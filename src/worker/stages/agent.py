"""Stagiul 7 — Agent (GPT-5.4-mini) cu TOOL-CALLING (G7). Recomandă produse pe rutele
de vânzare, chemând unelte deterministe în catalog (max 3/tur, cap dur în adaptor).

Agentul DECIDE ce tool cheamă (search_products / get_product_details / compare_products);
uneltele sunt cod determinist scoped pe `business_id`. Bucla de function-calling stă în
adaptor (`llm.run_tool_loop`); aici dăm callback-ul `execute` care rulează uneltele și
acumulează produsele retrievate. Validator inline (stagiul 8): preț + link din reply ∈
retrieval → 1 retry → fallback determinist. ZERO halucinații structural.

Degradare grațioasă: fără LLM / eroare de buclă → no-op (echo fallback). Vezi
docs/agent-tools-architecture.md.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from src.agent import prompt_builder
from src.agent.deterministic import (
    _CHEAPER_RE,
    _COMPARE_RE,  # noqa: F401 — re-export (teste)
    _LINK_RE,  # noqa: F401 — re-export (teste)
    _MORE_RE,  # noqa: F401 — re-export (teste)
    _comparison_facets,
    is_show_more,
    try_pre_intents,
)
from src.agent.fallbacks import (
    _card_products,
    _cart_confirm_msg,
    _cheapest_already_msg,
    _checkout_label,
    _compare_chips,
    _cross_sell_query,
    _dedupe,
    _no_more_msg,
)
from src.agent.finalize import (
    _finalize,
    _finalize_grounded,
    _finalize_rich,
    _no_result_msg,
    _rich_bundle,  # noqa: F401 — re-export (teste)
)
from src.agent.prompt_builder import PromptInputs
from src.agent.tool_definitions import tool_schemas
from src.agent.tool_executor import (
    ToolRun,
    _safe_tool_args,  # noqa: F401 — re-export (teste)
    _trunc,  # noqa: F401 — re-export (teste)
)
from src.agent.validator import (
    ValidationResult,  # noqa: F401 — re-export (consumatori/teste)
    _allowed_numbers,  # noqa: F401 — re-export (teste)
    _allowed_prices,  # noqa: F401 — re-export (teste; folosit de finalize)
    _bad_bare_numbers,
    _bare_numbers_ok,  # noqa: F401 — re-export (teste)
    _budget,  # noqa: F401 — re-export (teste)
    _claims_ok,
    _links_ok,  # noqa: F401 — re-export (teste)
    _prices_ok,  # noqa: F401 — re-export (teste)
    _safety_ok,  # noqa: F401 — re-export (teste)
    _stock_available,  # noqa: F401 — re-export (teste)
    _stock_claim_ok,
    _valid,
    validate_prose,  # noqa: F401 — re-export (consumatori/teste)
)
from src.config import get_settings
from src.db.queries.catalog import (
    get_complementary_products,
    get_products_by_ids,
    list_category_names,
    list_routing_aliases,
    search_cheaper_than,
)
from src.models import Offer, RetrievalResult, Route, RouteDecision, TurnContext
from src.tools import (  # noqa: F401 — importul înregistrează tool-urile
    catalog_tools,
    commerce_tools,
    faq_tools,
    handoff_tools,
    orders_tools,
)
from src.tools.base import enabled_tools
from src.worker import compose
from src.worker.context import context_blocks, conversation_transcript
from src.worker.order_gate import login_required_for_ctx, web_unidentified

if TYPE_CHECKING:
    from src.worker.runner import PipelineDeps

log = logging.getLogger(__name__)

# Validatorul de proză (preț/link/număr/claim/stoc/safety) + regexurile `_PRICE_RE`/`_BUDGET_RE`/
# `_URL_RE`/`_BARE_NUM_RE`/`_SAFE_BARE` trăiesc în `src/agent/validator.py` (NX-142) și sunt
# importate mai sus. `_finalize*` orchestrează retry/fallback pe baza lor (regia rămâne aici).

# Regexurile intențiilor deterministe PRE-loop (`_LINK_RE`/`_COMPARE_RE`/`_THREE_RE`/`_MORE_RE`) +
# `_CHEAPER_RE` (partajat) trăiesc în `src/agent/deterministic.py` (NX-143). `_ATTR_QUERY_RE` rămâne
# aici (intenție POST-loop, faza E → planner NX-144).

# MOD SUPERLATIV (IZI): întrebare despre setul AFIȘAT de tip „care dintre ele e cea mai X". ÎNALTĂ
# precizie (ca _COMPARE_RE): „care" + „cea/cel/cele mai" în aceeași frază, SAU „care dintre ele/
# acestea", SAU „cea mai <atribut> dintre ele". Prinde „care e cea mai ușoară/ieftină" (superlativ
# pe setul afișat), NU o căutare nouă („arată-mi ceva mai ieftin" = cheaper). RO/EN/HU.
_ATTR_QUERY_RE = re.compile(
    r"\bcare\b[^?]{0,40}\b(cea|cel|cele|cei)\s+mai\b"
    r"|\bcare\s+dintre\s+(ele|acestea|astea|aceste)\b"
    r"|\b(cea|cel|cele|cei)\s+mai\s+\w+\s+dintre\s+(ele|acestea|astea)\b"
    r"|\bwhich\s+of\s+(these|them)\b|\bwhich\b[^?]{0,40}\b(most|best|cheapest|lightest)\b"
    r"|\bmelyik\b[^?]{0,40}\bleg\w+",
    re.IGNORECASE,
)


def _attach_checkout_offer(ctx: TurnContext, url: str | None) -> None:
    """NX-137: linkul de checkout creat în ACEST tur ajunge GARANTAT la client, pe orice cale de
    compunere. Root cause (găsit live pe sim): pe calea RICH (web) modelul are INTERZIS structural
    să scrie linkuri (regulile rich) → linkul era creat în DB (`checkout_link_created`) și apoi
    murea tăcut — reply fără URL. Offer e neutru de canal (NX-114): marginile bogate randează
    buton/CTA; floor-ul din `set_offer` lipește URL-ul la text DOAR dacă nu e deja acolo (proza
    de WhatsApp îl poate conține deja — fără dublare)."""
    if not url or ctx.reply is None:
        return
    ctx.set_offer(Offer(kind="open_url", label=_checkout_label(ctx.language), url=url))
    ctx.emit("checkout_offer_attached")


# System prompt-urile sunt GENERATE din DB per (business, locale) — vezi `prompt_builder`
# (NX-78, principiul 9). ZERO vertical hardcodat aici. `agent_stage` construiește `PromptInputs`
# o dată și pasează prompturile la run_tool_loop / _finalize / _finalize_rich.


async def _load_prompt_inputs(deps: PipelineDeps, ctx: TurnContext) -> PromptInputs:
    """Citește categoriile + aliasele aprobate (scoped pe business) și compune `PromptInputs`
    (NX-78). Determinist (query-uri `order by`) → prefix de cache stabil. Ridicarea unei
    excepții de DB se propagă în `try`-ul din `agent_stage` (→ echo fallback, P6)."""
    categories = await list_category_names(deps.conn, ctx.business.id)
    aliases = await list_routing_aliases(deps.conn, ctx.business.id)
    return PromptInputs.build(
        ctx.business.name, ctx.business.vertical, ctx.language, categories, aliases
    )


# NX-133: stiva de constrângeri multi-tur. Cheile scalare peste care merge-ul suprascrie/păstrează;
# `concerns` are tratament de UNION separat. `category_key` = trigger de reset, nu constrângere de
# search (o vede _filters_hint dar o ignoră). Cap dur (P4): 5 termeni concerns, ≤6 chei total.
_CONSTRAINT_SCALAR_KEYS = ("budget_max", "suitable_for", "brand")
_MAX_CONCERNS = 5


def merge_constraints(
    stored: Any, filters: dict[str, Any] | None, category_key: str | None
) -> tuple[dict[str, Any], bool]:
    """Funcție PURĂ: împacă stiva stocată cu sloturile turului curent (`filters` din triaj), pt ca
    o RAFINARE („am tenul mixt") să NU piardă constrângerile deja spuse („ser cu vitamina C sub
    150"). Regulă (P5, runda 2):
    - slot scalar NOU (buget/brand/suitable_for) → SUPRASCRIE; absent → păstrează din stivă;
    - `concerns` → UNION (recent întâi), dedupe case-insensitive, cap 5;
    - RESET total când `category_key` e set ȘI diferă de cel stocat (subiect nou: alt tip produs).
      `category_key` null (follow-up neancorat) → stiva se PĂSTREAZĂ — exact cazul rafinării.
    Întoarce `(merged, reset)`. Robust la stored corupt (non-dict → {})."""
    stored = stored if isinstance(stored, dict) else {}
    filters = filters if isinstance(filters, dict) else {}
    prev_cat = stored.get("category_key")
    reset = bool(category_key) and bool(prev_cat) and category_key != prev_cat
    base = {} if reset else dict(stored)
    merged: dict[str, Any] = {}

    for k in _CONSTRAINT_SCALAR_KEYS:
        v = filters.get(k)
        if v is not None and v != "":
            merged[k] = v
        elif base.get(k) not in (None, ""):
            merged[k] = base[k]

    seen: set[str] = set()
    unioned: list[str] = []
    for c in [*(filters.get("concerns") or []), *(base.get("concerns") or [])]:
        if not isinstance(c, str):
            continue
        key = c.strip().lower()
        if key and key not in seen:
            seen.add(key)
            unioned.append(c.strip())
    if unioned:
        merged["concerns"] = unioned[:_MAX_CONCERNS]

    cat = category_key or (None if reset else prev_cat)
    if cat:
        merged["category_key"] = cat
    return merged, reset


def _filters_hint(filters: dict[str, Any]) -> str:
    """NX-116: constrângerile structurate din triaj (`RouteDecision.filters`) ca HINT determinist
    pentru primul `search_products` — agentul nu le reparsează din proză. Args rămân ale modelului
    (P3); hint-ul doar îl seedează cu ce a extras nano. NX-133: primește stiva MERGED (nu doar
    turul curent) → hint-ul cară constrângerile deja spuse."""
    if not filters:
        return ""
    parts: list[str] = []
    if filters.get("budget_max") is not None:
        parts.append(f"buget max {filters['budget_max']:g}")
    if filters.get("concerns"):
        parts.append("nevoi: " + ", ".join(filters["concerns"]))
    if filters.get("suitable_for"):
        parts.append(f"pentru: {filters['suitable_for']}")
    if filters.get("brand"):
        parts.append(f"brand: {filters['brand']}")
    if not parts:
        return ""
    return "Constrângeri detectate (folosește-le în search_products): " + "; ".join(parts) + "\n"


def _lead_score_hint(ctx: TurnContext) -> str:
    """Val3: lead_score (0..100, cross-tur, calculat post-tur NX-88) era câmp MORT — agentul nu-l
    citea. La scor RIDICAT (≥ prag, semnal de intenție acumulată) injectează un nudge per-tur spre
    finalizare (bias checkout), fără să forțeze. Hint în USER (nu în prefixul cached). Gated."""
    s = get_settings()
    if not s.lead_score_hint_enabled:
        return ""
    try:
        score = float(ctx.contact.lead_score)
    except (TypeError, ValueError):
        return ""
    if score < s.lead_score_high_threshold:
        return ""
    return (
        "Semnal: client cu intenție mare de cumpărare (din interacțiunile anterioare) — fii "
        "proactiv spre finalizare: când e firesc, oferă linkul de checkout sau adăugarea în coș, "
        "fără să forțezi.\n"
    )


# NX-122: whitelist de chei per tool pentru `tool_call` în analytics, ALINIATĂ la arg-urile
# REALE ale tool-urilor (SearchArgs/CartAddArgs/...). NICIUN fallback „pune tot ce e acolo" —
# tool necunoscut sau cheie ne-listată → omis (P12: analytics nu primește text liber / PII).
# `search_products.query` e EXCLUS deliberat (e textul de căutare al modelului, poate ecoua
# fraza userului) — păstrăm doar FILTRELE structurate (category/concerns/price_max...) care
# răspund la „de ce a căutat în categoria greșită". `check_order` e special (vezi _safe_tool_args).
# Tool-urile cu arg-uri PII (faq_lookup.query, reorder, request_human) NU-s în whitelist → `{}`.
async def agent_stage(ctx: TurnContext, deps: PipelineDeps) -> None:
    """Bucla de tool-calling cu toolset PER RUTĂ: `sales` → recomandare grounded; `order` →
    status comandă (G7-3). Ambele validate; alte rute → no-op (lasă fallback/echo)."""
    if deps.llm is None:
        return
    route: RouteDecision | None = ctx.route
    if route is None or route.route not in (Route.SALES, Route.ORDER):
        return
    query = (ctx.message.body or "").strip()
    if not query:
        return
    is_order = route.route == Route.ORDER

    # NX-128++ (FAQ-first, cererea Adi): zidul de login NU mai e scurtcircuit pe toată ruta ORDER.
    # Răspunde la FAQ chiar nelogat; cere login doar pentru ce ține de contul lui (status/retur).
    # O întrebare de proces/politică (cum comand, ce retur, cât e livrarea) trebuie răspunsă fără
    # cont — la stratul FAQ (înainte de poartă) sau de agent prin `faq_lookup`. Lăsăm agentul să
    # ruleze; zidul apare DOAR dacă modelul cheamă `check_order` pe web anonim (lookup care chiar
    # are nevoie de cont). Tool-ul semnalează `login_required`; servim mesajul determinist după
    # buclă (cacheable=False) — nu parafraza modelului. NX-129: web cu login verificat trece.

    # Faza B (NX-143): intenții deterministe PRE-loop (link/compare) → early-exit, $0 inferență.
    if await try_pre_intents(ctx, deps):
        return

    # NX-119b: „mai arată-mi" pe o sesiune activă = paginare DETERMINISTĂ (predicatul e în
    # deterministic.py; paginarea propriu-zisă e mai jos, în GENERATE, via continue_search_session).
    sess = ctx.state.active_search
    show_more = is_show_more(ctx)

    tool_names = enabled_tools(ctx.business, route.route.value)
    tools = tool_schemas(tool_names)
    # Faza D (NX-143): tool executor cu stare explicită. Acumulatorii (produse/linkuri/sume/…) sunt
    # câmpuri ale lui `run`, nu `nonlocal`; `run.execute` e callback-ul buclei; citim `run.X` după.
    run = ToolRun(ctx, deps)

    history = conversation_transcript(ctx.history)
    history_block = f"Conversație până acum:\n{history}\n\n" if history else ""
    context = context_blocks(ctx)
    context_block = f"{context}\n\n" if context else ""
    # `category_key` derivat + validat în triaj → HINT pentru agent (NX-72). NU-l forțăm în tool
    # args din cod (P3: args sunt ale modelului); modelul decide dacă se potrivește cererii.
    cat_hint = f"Categorie probabilă: {route.category_key}\n" if route.category_key else ""
    # NX-133: stiva de constrângeri multi-tur — DOAR pe SALES (order/handoff nu ating stiva).
    # Filters curente merged peste ce s-a spus deja → rafinarea nu resetează căutarea. Scriere pe
    # ctx.state (owner = agent); persistat de processor (merge canonic, ca `constraints`).
    if is_order:
        merged_constraints = route.filters
    else:
        merged_constraints, cons_reset = merge_constraints(
            ctx.state.search_constraints, route.filters, route.category_key
        )
        ctx.state.search_constraints = merged_constraints
        current = route.filters or {}
        carried = sum(1 for k in merged_constraints if k != "category_key" and k not in current)
        ctx.emit(
            "constraints_merged",
            keys=sorted(merged_constraints),
            reset=cons_reset,
            carried=carried,
        )
    filters_hint = _filters_hint(merged_constraints)  # NX-116/133: seed structurat (stiva merged)
    # A2 (Val1): semnal de CUMPĂRARE → onorează intenția (checkout_link + confirmă stocul), nu
    # re-recomanda. Hint per-tur (în USER, nu în prefixul cached). Leagă tool-urile existente de
    # intenția detectată de triaj (gap-ul „tool-uri există dar nelegate").
    purchase_hint = (
        "Semnal: clientul vrea să CUMPERE acum. Dacă produsul e deja arătat (din produsele "
        "discutate), cheamă checkout_link pe el și confirmă disponibilitatea/stocul; dacă nu e pe "
        "stoc, oferă subscribe_back_in_stock. Altfel caută-l întâi, apoi oferă linkul de checkout. "
        "NU re-recomanda inutil.\n"
        if route.purchase_intent
        else ""
    )
    lead_hint = _lead_score_hint(ctx)  # Val3: nudge la lead_score ridicat (câmp altfel mort)
    user = (
        f"Limba clientului: {ctx.language}\n{cat_hint}{filters_hint}{purchase_hint}{lead_hint}"
        f"{context_block}{history_block}Mesaj client: {query}"
    )

    try:
        inp = await _load_prompt_inputs(deps, ctx)  # prompt generat din DB (NX-78, P9)
        if show_more:
            # Pagina următoare din pool-ul sesiunii — determinist, fără inferență LLM (NX-119b).
            page = await catalog_tools.continue_search_session(ctx, deps, sess, 6)
            if not page.products:
                # Pool epuizat → mesaj determinist per-locale, fără tăcere (P6, cacheable=False).
                ctx.emit("show_more", served=0)
                ctx.set_reply(_no_more_msg(ctx.language), cacheable=False)
                return
            retrieved = page.products
            ctx.emit("show_more", served=len(retrieved))
            final = ""  # fără text de model — compose-ul rich phrasează din produse mai jos
        else:
            final = await deps.llm.run_tool_loop(
                prompt_builder.build_agent_system(inp), user, tools, run.execute
            )
            retrieved = run.retrieved  # produsele acumulate de tool executor în această buclă
    except Exception as e:  # noqa: BLE001 — bucla eșuată → lasă echo fallback
        log.warning("agent: tool loop eșuat (%s)", type(e).__name__)
        return

    # FAQ-first: dacă modelul a cerut chiar un lookup de comandă pe web anonim (check_order →
    # login_required), servim mesajul de login DETERMINIST. Apare DOAR acum (nu pe toată ruta), după
    # ce agentul a avut șansa să răspundă din FAQ/catalog. cacheable=False (context-relativ).
    if run.order_gated_login:
        ctx.set_reply(login_required_for_ctx(ctx), cacheable=False)
        return

    # NX-137: nota de comerț pentru compunere — un cart_add/checkout_link eșuat în acest tur
    # interzice chips-urile care promit exact acțiunea refuzată (contradicția din runda 2 iZi).
    commerce_note = (
        "coșul/linkul de plată au EȘUAT în acest tur — în `suggestions` NU propune mesaje de tip "
        "«adaugă în coș» sau «dă-mi link de plată»; oferă alternative (detalii, comparație, "
        "similare)."
        if run.failed_commerce
        else ""
    )

    # NX-137: purchase_intent onorat DETERMINIST (CALM — codul decide). Observat live pe sim:
    # clientul cere EXPLICIT „adaugă în coș și dă-mi link de plată", modelul cheamă doar cart_add,
    # iar turul e deturnat de cross-sell — fără link. Dacă intenția de cumpărare e detectată, coșul
    # are linii și modelul n-a creat linkul, îl creează codul, prin ACELAȘI `execute` (analytics,
    # run.generated_links → cross-sell sare, checkout_offer → CTA pe reply; bookkeeping identic).
    if (
        not is_order
        and not show_more
        and route.purchase_intent
        and run.checkout_url is None
        and "checkout_link" not in run.failed_commerce
        and "checkout_link" in tool_names
        and get_settings().checkout_intent_fallback_enabled
    ):
        # state_patch["cart"] = coșul COMPLET merged de cart_add în acest tur; altfel cel din state.
        cart_lines = list(ctx.state_patch.get("cart") or ctx.state.cart or [])
        items = [
            {
                "product_id": str(line["product_id"]),
                "variant_id": line.get("variant_id"),
                "quantity": int(line.get("quantity") or 1),
            }
            for line in cart_lines
            if line.get("product_id")
        ][:10]
        if items:
            await run.execute("checkout_link", {"cart_items": items})
            ctx.emit("checkout_intent_fallback", items=len(items))

    # #7b — cross-sell „merge bine cu" (model iZi): clientul tocmai a adăugat un produs în coș →
    # sugerăm produse COMPLEMENTARE (rutină/accesorii) ca CARDURI, prin calea rich existentă.
    # Retrieval DETERMINIST (brand/concern, categorie DIFERITĂ = complement, nu substitut); copy
    # de la model (fit per produs, scrubuit); intro = confirmare DETERMINISTĂ a coșului (robustă la
    # scrub pe nume cu cifre) + pick scos (n-are sens un „pick" între complementare). Gated de
    # kill-switch; fără complementare / rich eșuat → cade în flux normal (confirmarea de coș).
    added = run.added_product
    if (
        not is_order
        and added is not None
        and not run.generated_links  # checkout link creat în acest tur → linkul, nu cross-sell
        and get_settings().cross_sell_enabled
    ):
        cart_lines = list(ctx.state.cart or []) + list(ctx.state_patch.get("cart") or [])
        exclude_ids = [str(line.get("product_id")) for line in cart_lines if line.get("product_id")]
        complementary = await get_complementary_products(
            deps.conn, ctx.business.id, str(added["id"]), exclude_ids=exclude_ids, limit=4
        )
        if complementary:
            ctx.retrieval = RetrievalResult(products=complementary, source="cross_sell")
            rich = await _finalize_rich(
                deps.llm,
                prompt_builder.build_rich_system(inp),
                _cross_sell_query(added, ctx.language),
                complementary,
                ctx,
                history,
                notes=commerce_note,
            )
            if rich is not None and rich.items:
                rich.intro = _cart_confirm_msg(added, ctx.language)  # confirmare robustă (no scrub)
                rich.pick = None  # fără „Recomandarea mea" între complementare
                ctx.set_rich_reply(
                    rich,
                    text=compose.flatten(rich, ctx.language),
                    products=compose.card_products(rich.items),
                )
                ctx.emit("cross_sell", added=str(added["id"]), n=len(rich.items))
                return
        ctx.emit("cross_sell", added=str(added["id"]), n=0)
        # niciun complement / rich eșuat → cade în fluxul normal (confirmarea de coș a agentului)

    products = _dedupe(retrieved)
    # MOD SUPERLATIV (IZI): întrebare „care dintre ele e cea mai X" pe setul AFIȘAT → re-hidratează
    # ÎNTREGUL set afișat (nu o căutare nouă, nu 1 produs) ca modelul să RĂSPUNDĂ la superlativ
    # peste toate candidatele reale (fațete/descriere în bundle). Precede cheaper: „care dintre
    # ACESTEA e cea mai ieftină" = min-ul setului afișat, NU „ceva mai ieftin" (căutare nouă).
    # ≥2 afișate, fără filtre noi (cu filtre = căutare/rafinare → bucla LLM). Kill-switch propriu.
    attr_query = (
        not is_order
        and get_settings().attr_query_enabled
        and len(ctx.state.displayed_products) >= 2
        and not route.filters
        and _ATTR_QUERY_RE.search(query) is not None
    )
    if attr_query:
        ids = [p.product_id for p in ctx.state.displayed_products]
        hydrated = await get_products_by_ids(deps.conn, ctx.business.id, ids, limit=6)
        if hydrated:
            products = _dedupe(hydrated)
        ctx.emit("attr_query", n=len(products))
    # P1 (ARCH-product-retrieval): follow-up „mai ieftin" pe un set deja afișat → re-căutare
    # DETERMINISTĂ a produselor strict mai ieftine decât cel mai ieftin afișat, în aceeași categorie
    # (search_cheaper_than) — NU re-rank pe setul afișat (bug-ul „cea mai ieftină 80.99 când există
    # 18.99"). Arată DOAR ce e mai ieftin (1 dacă e 1, zero padding); nimic mai ieftin → mesaj
    # determinist (niciodată tăcere/padding, P6). Sare peste R3 pentru această intenție.
    # NU pe attr_query („care dintre acestea e cea mai ieftină" = superlativ pe set, nu căutare).
    cheaper_intent = (
        not is_order
        and not show_more  # „mai arată-mi" deja paginat determinist mai sus
        and not attr_query
        and get_settings().cheaper_intent_enabled
        and bool(ctx.state.displayed_products)
        and _CHEAPER_RE.search(query) is not None
    )
    if cheaper_intent:
        baseline = min(p.price for p in ctx.state.displayed_products)
        ref_ids = [p.product_id for p in ctx.state.displayed_products]
        cheaper = await search_cheaper_than(deps.conn, ctx.business.id, ref_ids, baseline, limit=6)
        ctx.emit("cheaper_followup", baseline=round(baseline, 2), found=len(cheaper))
        if cheaper:
            products = _dedupe(cheaper)
        else:
            # Nimic mai ieftin → mesaj sigur (NU cacheabil: e relativ la setul afișat al ACESTUI
            # client; un cache hit l-ar servi altui context — clasa de cache-poisoning știută).
            ctx.set_reply(_cheapest_already_msg(ctx.language), cacheable=False)
            return
    # R3: follow-up pe produse DEJA arătate („care e cea mai bună?") la care modelul n-a rechemat
    # un tool → re-hidratează produsele afișate (după id, din state) ca set de retrieval, ca să
    # răspundem GROUNDED pe ele în loc de „n-am găsit". Doar SALES, NU pe intenția de preț (aia o
    # tratează cheaper_intent mai sus), doar cu id-uri în state, și DOAR când textul singur ar pica
    # (gol sau preț negroundat). Rămâne plasa de grounding pentru follow-up-urile neclasificate.
    rehydrated = False
    if (
        not products
        and not is_order
        and not cheaper_intent
        and not show_more
        and ctx.state.displayed_products
        and not (final and _valid(final, [], run.generated_links, run.grounded_prices))
    ):
        ids = [p.product_id for p in ctx.state.displayed_products]
        products = await get_products_by_ids(deps.conn, ctx.business.id, ids, limit=6)
        rehydrated = True
    # izi-parity hardening: relevanța off-category NUMAI pe calea de căutare PROASPĂTĂ. „Mai ieftin"
    # (set determinist), paginarea și re-hidratarea din state (produse deja arătate, on-topic) NU
    # setează semnalul → compose tratează ca potrivire exactă (fail-open, fără suprimare falsă).
    relevance = None if (cheaper_intent or rehydrated) else run.search_relevance
    ctx.retrieval = RetrievalResult(products=products, source="tools", relevance=relevance)

    # IZI-compare: modelul a chemat compare_products → turul e o COMPARAȚIE, nu o recomandare.
    # Tabel structurat DETERMINIST din setul comparat (ordinea cerută păstrată) — fapte din
    # retrieval, lead determinist (cel mai ieftin / cel mai bine cotat), ZERO proză LLM în celule →
    # zero halucinație. Web randează tabelul; canalele text primesc floor-ul aplatizat. Precede
    # calea rich de recomandare (altfel ar re-RECOMANDA în loc să compare — bug-ul „Compară primele
    # două" care doar re-lista produsele). Sare peste rich/proză pentru acest tur.
    if run.compared and not is_order:
        comparison = compose.build_comparison(run.compared, ctx.language, _comparison_facets(ctx))
        if comparison is not None:
            ctx.set_comparison_reply(
                comparison,
                text=compose.flatten_comparison(comparison, ctx.language),
                products=compose.comparison_cards(comparison),
                chips=_compare_chips(comparison.columns, ctx.language),
            )
            ctx.emit("agent_compared", n=len(comparison.columns))
            return

    if products:
        # Calea BOGATĂ (model iZi): recomandare structurată → compose. Doar pe SALES.
        # Orice eșec (apel structurat, zero items după membership) → fallback pe proză.
        if not is_order:
            rich = await _finalize_rich(
                deps.llm,
                prompt_builder.build_rich_system(inp),
                query,
                products,
                ctx,
                history,
                notes=commerce_note,
            )
            if rich is not None and rich.items:
                ctx.set_rich_reply(
                    rich,
                    text=compose.flatten(rich, ctx.language),
                    products=compose.card_products(rich.items),
                )
                # NX-137: regulile rich INTERZIC linkuri în proza modelului → fără atașarea asta,
                # linkul de checkout creat în acest tur nu ajungea NICIODATĂ la client pe web.
                _attach_checkout_offer(ctx, run.checkout_url)
                ctx.emit("agent_recommended", n=len(rich.items), rich=True)
                return
            # NX-122: downgrade tăcut rich → proză, acum vizibil. `rich is None` = apelul
            # structurat a eșuat/excepție; `rich.items == []` = toate produsele au picat la
            # grounding-ul de apartenență. Pur observabilitate (downgrade-ul exista deja, P6).
            reason = (
                "all-items-dropped-by-membership" if rich is not None else "structured-call-failed"
            )
            ctx.emit("rich_downgraded", reason=reason)
        # NX-91: dacă textul brut al modelului are cifre bare negroundate, semnalează (P12: doar
        # contorul, NU corpul). _finalize declanșează retry-ul/fallback-ul pe baza lui _valid.
        bare = _bad_bare_numbers(final, products, run.grounded_prices) if final else []
        if bare:
            ctx.emit("validator_rejected", kind="bare_number", n=len(bare))
        # NX-117: claim ne-numeric neverificabil pe proză → semnalează (P12: doar contorul).
        if final and not _claims_ok(final):
            ctx.emit("validator_rejected", kind="claim")
        # NX-118: claim de stoc nefondat (niciun produs pe stoc) → semnalează (P12: doar contorul).
        if final and not _stock_claim_ok(final, products):
            ctx.emit("validator_rejected", kind="stock_claim")
        reply = await _finalize(
            deps.llm,
            prompt_builder.build_reco_system(inp),
            query,
            final,
            products,
            ctx.language,
            history,
            run.generated_links,
            run.grounded_prices,
        )
        ctx.set_reply(reply, products=_card_products(products))
        # NX-137: pe proză modelul POATE scrie linkul (validat prin run.generated_links), dar dacă
        # l-a omis, Offer-ul îl garantează (floor-ul din set_offer nu dublează un URL deja în text).
        _attach_checkout_offer(ctx, run.checkout_url)
        ctx.emit("agent_recommended", n=len(products))
    elif final:
        # Fără produse, dar avem text: îl VALIDĂM (nu servire oarbă). Forma de recuperare diferă
        # pe rută — nu trecem o întrebare de vânzare prin fallback-ul de status comandă.
        if is_order:
            # ORDER: fără bare-check (numere DB legitime: dată/AWB/cantitate) — vezi _valid.
            reply = await _finalize_grounded(
                deps.llm,
                final,
                "\n".join(run.order_views),
                ctx.language,
                run.generated_links,
                run.grounded_prices,
            )
            ctx.set_reply(reply)
        elif _valid(final, [], run.generated_links, run.grounded_prices):
            # SALES: text fără produse și fără sumă inventată (clarificare) → servim
            ctx.set_reply(final)
        else:
            # SALES: preț negroundat fără produse care să-l susțină → mesaj sigur de vânzare.
            # NU cacheabil: altfel „n-am găsit" otrăvește semantic_cache și se re-servește la
            # fiecare query similar, sărind agentul (bug găsit live: hit_count=9 pe demo).
            ctx.set_reply(_no_result_msg(is_order=False), cacheable=False)
    elif is_order and web_unidentified(ctx):
        # ORDER pe web anonim, fără rezultat (modelul n-a chemat un tool) → login, NU „dă-mi numărul
        # comenzii" (ar relua bucla NX-128 pe un canal unde lookup-ul nu poate reuși).
        ctx.set_reply(login_required_for_ctx(ctx), cacheable=False)
    else:
        ctx.set_reply(_no_result_msg(is_order), cacheable=False)
