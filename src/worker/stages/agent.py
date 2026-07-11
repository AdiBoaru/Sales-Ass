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
from typing import TYPE_CHECKING, Any

from src.agent import prompt_builder
from src.agent.deterministic import (
    _CHEAPER_RE,  # noqa: F401 — re-export (teste)
    _COMPARE_RE,  # noqa: F401 — re-export (teste)
    _LINK_RE,  # noqa: F401 — re-export (teste)
    _MORE_RE,  # noqa: F401 — re-export (teste)
    is_show_more,
    try_pre_intents,
)
from src.agent.fallbacks import (
    _cart_confirm_msg,  # noqa: F401 — re-export (teste)
    _cheapest_already_msg,  # noqa: F401 — re-export (teste)
    _no_more_msg,
)
from src.agent.finalize import (
    _rich_bundle,  # noqa: F401 — re-export (teste)
    render,
)
from src.agent.observability import agent_prompt_event
from src.agent.planner import build_plan
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
    _bad_bare_numbers,  # noqa: F401 — re-export (teste)
    _bare_numbers_ok,  # noqa: F401 — re-export (teste)
    _budget,  # noqa: F401 — re-export (teste)
    _claims_ok,  # noqa: F401 — re-export (teste)
    _links_ok,  # noqa: F401 — re-export (teste)
    _prices_ok,  # noqa: F401 — re-export (teste)
    _safety_ok,  # noqa: F401 — re-export (teste)
    _stock_available,  # noqa: F401 — re-export (teste)
    _stock_claim_ok,  # noqa: F401 — re-export (teste)
    _valid,  # noqa: F401 — re-export (teste; patch-uit în test_golden)
    validate_prose,  # noqa: F401 — re-export (consumatori/teste)
)
from src.config import get_settings
from src.db.queries.catalog import (
    list_category_names,
    list_routing_aliases,
)
from src.models import Route, RouteDecision, TurnContext
from src.tools import (  # noqa: F401 — importul înregistrează tool-urile
    catalog_tools,
    commerce_tools,
    faq_tools,
    handoff_tools,
    orders_tools,
)
from src.tools.base import enabled_tools
from src.worker.context import context_blocks, conversation_transcript

if TYPE_CHECKING:
    from src.worker.runner import PipelineDeps

log = logging.getLogger(__name__)

# Validatorul de proză (preț/link/număr/claim/stoc/safety) trăiește în `src/agent/validator.py`
# (NX-142); `render` (faza F, `src/agent/finalize.py`) orchestrează retry/fallback pe baza lui.
# Regexurile intențiilor deterministe PRE-loop (`_LINK_RE`/`_COMPARE_RE`/`_MORE_RE` + `_CHEAPER_RE`
# partajat) trăiesc în `deterministic.py` (NX-143); `_ATTR_QUERY_RE` + shaping-ul determinist
# post-loop (checkout-fallback/cross-sell/attr/cheaper/rehidratare) s-au mutat în
# `src/agent/planner.py` (NX-144, `build_plan`). Aici rămâne DOAR regia A→B→C→D → plan → render.


# System prompt-urile sunt GENERATE din DB per (business, locale) — vezi `prompt_builder`
# (NX-78, principiul 9). ZERO vertical hardcodat aici. `agent_stage` construiește `PromptInputs`
# o dată și pasează prompturile la run_tool_loop / build_plan / render.


async def _load_prompt_inputs(deps: PipelineDeps, ctx: TurnContext) -> PromptInputs:
    """Citește categoriile + aliasele aprobate (scoped pe business) și compune `PromptInputs`
    (NX-78). Determinist (query-uri `order by`) → prefix de cache stabil. Ridicarea unei
    excepții de DB se propagă în `try`-ul din `agent_stage` (→ echo fallback, P6)."""
    categories = await list_category_names(deps.conn, ctx.business.id)
    aliases = await list_routing_aliases(deps.conn, ctx.business.id)
    # NX-159 felia 3 / NX-165: profilul de STIL (DomainPack) intră în TOATE system-urile de
    # compunere (buclă/retry/rich). Gated; pack absent / OFF → None → prefix byte-identic.
    pack = getattr(ctx.business, "domain_pack", None)
    style = pack.response_style if (pack and get_settings().response_style_enabled) else None
    return PromptInputs.build(
        ctx.business.name,
        ctx.business.vertical,
        ctx.language,
        categories,
        aliases,
        response_style=style,
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

    system: str | None = None  # NX-146: promptul de sistem rendered (pt agent_prompt event)
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
            system = prompt_builder.build_agent_system(inp)
            final = await deps.llm.run_tool_loop(system, user, tools, run.execute)
            retrieved = run.retrieved  # produsele acumulate de tool executor în această buclă
    except Exception as e:  # noqa: BLE001 — bucla eșuată → lasă echo fallback
        log.warning("agent: tool loop eșuat (%s)", type(e).__name__)
        return

    # Faza E (NX-144): shaping determinist post-loop (checkout-fallback/cross-sell/attr_query/
    # cheaper/rehidratare) → `ResponsePlan`. Ramurile care răspund direct (login / cross-sell /
    # „deja cel mai ieftin") setează `ctx.reply` și întorc `handled=True` → sărim peste render.
    plan = await build_plan(
        ctx,
        deps,
        run,
        inp,
        final=final,
        retrieved=retrieved,
        is_order=is_order,
        show_more=show_more,
        query=query,
        history=history,
        tool_names=tool_names,
    )
    # Faza F (NX-144): render pe plan → răspuns final (comparație / rich / proză / order /
    # fallback), validat + retry + fallback. Singurul punct de ieșire e Sender, via `render`.
    # `handled=True` = build_plan a răspuns deja direct (login/cross-sell/„deja cel mai ieftin")
    # → render sare peste tur (fără ValidationResult de proză).
    validation: ValidationResult | None = None
    if not plan.handled:
        validation = await render(ctx, deps, plan)
    # NX-146 felia 2 (fix DoD): emis DUPĂ validare (nu înainte) — corelează per-tur prompt↔
    # grounding↔rezultatul validatorului pentru Turn Replay (P10 — observabilitate din runner;
    # corpul promptului NU se persistă, doar hash + retrieval IDs, P12).
    if system is not None:
        ctx.emit(
            "agent_prompt",
            **agent_prompt_event(
                system,
                user,
                retrieved,
                store_prompt=get_settings().replay_store_prompt_enabled,
                validator=validation,
            ),
        )
