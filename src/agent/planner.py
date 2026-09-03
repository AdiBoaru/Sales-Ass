"""Faza E — planner (NX-144 felia 1b). Extras 1:1 din `agent_stage`.

Descoperirea din `docs/AGENT-ARCHITECTURE.md` §2: shaping-ul DETERMINIST post-loop
(`checkout-fallback`/`cross-sell`/`attr_query`/`cheaper`/rehidratare) împletit cu decizia de
render ESTE planner-ul implicit. Aici e făcut explicit: `build_plan(ctx, deps, run, ...)` ia
rezultatul buclei de tool-uri (`ToolRun`) și produce un `ResponsePlan` — setul FINAL de produse,
nota de comerț, linkul de checkout și `mode`-ul de render (sau un reply direct pentru ramurile
care răspund singure: login / cross-sell / „deja cel mai ieftin").

Comportament BYTE-IDENTIC cu vechiul bloc post-loop din `agent_stage` (felia 1). Grounding-ul
rămâne la `validator`/`finalize` (P2); planner-ul DOAR decide, `render` (faza F) randează.
`ctx.retrieval` are un singur owner: acest modul (P3).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from src.agent import prompt_builder
from src.agent.deterministic import _CHEAPER_RE
from src.agent.fallbacks import (
    _cart_confirm_msg,
    _cheapest_already_msg,
    _dedupe,
    _relation_chain_query,
    _thin_path_chips,
)
from src.agent.finalize import _finalize_rich
from src.agent.match_gate import build_match_set
from src.agent.query_rewrite import build_query_spec
from src.agent.relevance_gate import apply_mask
from src.agent.validator import _valid
from src.analytics.demand import clean_ids
from src.catalog.relation_chain import walk_chain
from src.config import get_settings
from src.conversation.state_v2 import active_needs
from src.db.queries.catalog import (
    get_complementary_products,
    get_products_by_ids,
    search_cheaper_than,
    traverse_relation_chain,
)
from src.models import RetrievalResult, TurnContext
from src.safety.policy import SafetyPolicy
from src.worker import compose
from src.worker.order_gate import login_required_for_ctx, web_unidentified

if TYPE_CHECKING:
    from src.agent.prompt_builder import PromptInputs
    from src.agent.tool_executor import ToolRun
    from src.worker.runner import PipelineDeps

log = logging.getLogger(__name__)


# NX-263: sub cât nu e o secvență. Un „lanț" de un pas e un vecin direct cu alt nume, iar a-l
# anunța drept „pașii următori" ar fi o promisiune mai mare decât conținutul.
_MIN_CHAIN_STEPS = 2


async def _cart_followup_products(
    ctx: TurnContext, deps: PipelineDeps, added_id: str, exclude_ids: list[str]
) -> tuple[list[dict[str, Any]], str | None]:
    """Ce propunem după add-to-cart: PAȘII unei secvențe dacă tenantul o declară și ancora o are,
    altfel complementarele de azi. Întoarce `(produse, eticheta secvenței)`; eticheta e `None`
    pentru complemente, iar apelantul o folosește ca să știe ce fel de răspuns compune.

    Declanșarea e DETERMINISTĂ (D2): decide codul, pe baza `DomainPack.relation_kinds` și a datelor,
    nu modelul. Nu există tool nou și niciun nume de tip de muchie nu ajunge în prompt ca vocabular
    de ales — modelul primește doar produsele, în ordine, și eticheta localizată.

    Fail-safe în toate ramurile: flag stins, pack absent, niciun tip `ordered`, lanț prea scurt sau
    pași necumpărabili ⇒ exact comportamentul de azi. UN singur checkout (NX-231): traversarea și
    hidratarea sunt amândouă muncă de DB, fără niciun await extern între ele."""
    pack = getattr(ctx.business, "domain_pack", None)
    sequences = pack.relation_kinds.sequences() if pack is not None else ()
    excluded = {str(pid) for pid in exclude_ids}
    async with deps.db("cross_sell_complementary") as conn:
        if get_settings().relation_traversal_enabled:
            for spec in sequences:
                hops = await traverse_relation_chain(
                    conn,
                    ctx.business.id,
                    anchor_id=added_id,
                    kind=spec.kind,
                    max_depth=spec.max_depth,
                )
                steps = [h for h in walk_chain(hops, added_id, spec.max_depth)]
                ids = [h["id"] for h in steps if h["id"] not in excluded]
                if len(ids) < _MIN_CHAIN_STEPS:
                    continue
                # `get_products_by_ids` PĂSTREAZĂ ordinea cerută (`array_position`) → pașii rămân
                # pași. `respect_content_status=True` fiindcă ăsta e un set NOU de descoperire, nu
                # o rehidratare a ceva deja arătat.
                found = await get_products_by_ids(
                    conn, ctx.business.id, ids, limit=6, respect_content_status=True
                )
                # Un pas indisponibil LIPSEȘTE din prezentare, nu rupe structura (vezi contractul
                # lui `traverse_relation_chain`). Filtrăm aici, nu în traversare.
                buyable = [p for p in found if p.get("availability") in ("in_stock", "low_stock")]
                if len(buyable) >= _MIN_CHAIN_STEPS:
                    ctx.emit(
                        "relation_chain",
                        kind=spec.kind,
                        steps=len(buyable),
                        depth=spec.max_depth,
                    )
                    return buyable, spec.label(ctx.language or "ro")
        complementary = await get_complementary_products(
            conn, ctx.business.id, added_id, exclude_ids=exclude_ids, limit=4
        )
    return complementary, None


async def _current_cart_lines(
    ctx: TurnContext, deps: PipelineDeps, run: ToolRun, *, fetch: bool
) -> list[dict[str, Any]]:
    """NX-237: liniile coșului, dintr-o SINGURĂ autoritate.

    Flag OFF → merge-ul legacy (`state_patch['cart']` are întâietate peste `state.cart`).
    Flag ON → snapshotul canonic al turului (`run.cart_snapshot`, dacă un tool de coș a rulat),
    altfel — DOAR când `fetch=True` — o citire scurtă prin serviciu. `state.cart` legacy nu mai
    e autoritate cu flagul ON (liniile vechi nu se importă cu preț stale, decizia cardului)."""
    if not get_settings().conversation_cart_enabled:
        return list(ctx.state_patch.get("cart") or ctx.state.cart or [])
    snap = run.cart_snapshot
    if snap is None and fetch:
        from src.commerce.cart_service import CartService  # noqa: PLC0415 — evită ciclul

        snap = await CartService.for_turn(ctx, deps).get_snapshot(ctx.conversation_id)
    return snap.command_lines() if snap is not None else []


def _match_gate_shadow(ctx: TurnContext, products: list[dict[str, Any]], query: str) -> None:
    """NX-187: MatchSet în SHADOW post-retrieval (kill-switch `match_gate_shadow_enabled`, default
    OFF). Construiește constrângerile din query (contractul NX-208) + registrul tipizat (NX-186),
    clasează candidații și emite telemetrie FĂRĂ PII (clase + numere + status per fațetă). NU atinge
    `reply` (shadow). Best-effort — orice eroare e înghițită (nu blochează turul, P6)."""
    s = get_settings()
    # NX-257: masca de potrivire are nevoie de ACELEAȘI verdicte, deci calculul pornește și când
    # doar ea e aprinsă. Shadow-ul rămâne ce era: observabilitate, fără efect asupra reply-ului.
    if not (s.match_gate_shadow_enabled or s.relevance_mask_enabled):
        return
    dp = ctx.business.domain_pack
    facets = {f.key: f for f in (dp.facets if dp else ())}
    if not facets:
        return
    try:
        spec = build_query_spec(query or "", dp, locale=ctx.language, needs=active_needs(ctx))
        ms = build_match_set(products, spec.constraints, facets)
        ctx.match_set = ms
        if not s.match_gate_shadow_enabled:
            return  # doar masca e aprinsă: verdictele există, telemetria de shadow nu se emite
        ctx.emit(
            "match_gate_shadow",
            n_candidates=len(products),
            n_exact=len(ms.exact),
            n_alternative=len(ms.alternatives),
            n_rejected=len(ms.rejected),
            n_hard=sum(1 for c in spec.constraints if c.strength == "hard"),
        )
        for r in ms.coverage:  # distribuție per fațetă hard — cheie canonică + numere (fără PII)
            ctx.emit(
                "match_gate_outcome",
                facet=r.facet,
                match=r.match,
                mismatch=r.mismatch,
                unknown=r.unknown,
            )
    except Exception:  # noqa: BLE001 — shadow pur observabil; nu blochează niciodată turul
        log.warning("match_gate_shadow failed", exc_info=True)


def _apply_relevance_mask(ctx: TurnContext, products: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """NX-257 — scoate produsele care CONTRAZIC ce a cerut clientul (kill-switch, default OFF).

    Poarta lipsă: tot restul sistemului verifică dacă răspunsul e ADEVĂRAT, nimic nu verifica dacă
    e POTRIVIT. Decizia e a `relevance_gate` (pură); aici doar o aplicăm și o raportăm.

    Setul GOL după mască nu se repopulează: dacă toate produsele contrazic cererea, „n-am găsit
    ceva potrivit" e răspunsul onest, iar căile de no-results există deja. A da înapoi produsele
    respinse ar însemna să preferăm un răspuns plin și greșit unuia gol și corect — exact eroarea
    pe care poarta o repară. Best-effort: orice excepție lasă setul neatins (P6)."""
    if not get_settings().relevance_mask_enabled or ctx.match_set is None:
        return products
    dp = ctx.business.domain_pack
    facets = {f.key: f for f in (dp.facets if dp else ())}
    try:
        # NX-271: lista ACTIVĂ, o fațetă pe rând. Goală ⇒ poarta rulează dar nu exclude
        # nimic — flagul singur nu ajunge, exact ca la promovarea retrievalului (NX-238).
        outcome = apply_mask(
            products, ctx.match_set, facets, get_settings().active_relevance_facets
        )
    except Exception:  # noqa: BLE001 — o poartă de calitate nu are voie să dărâme turul
        log.warning("relevance_mask failed", exc_info=True)
        return products
    if outcome.skipped_low_coverage:
        # Fațetă partiționantă căreia catalogul nu-i dă destule valori ca să distingem
        # „contrazice" de „neetichetat". Se RAPORTEAZĂ: e un defect de date, nu unul de cod.
        ctx.emit("relevance_mask_skipped", facets=list(outcome.skipped_low_coverage))
    if not outcome.changed:
        return products
    ctx.emit(
        "relevance_mask_applied",
        n_before=len(products),
        n_after=len(outcome.kept),
        n_excluded=len(outcome.excluded_ids),
        facets=list(outcome.enforced_facets),
        emptied=not outcome.kept,
    )
    ctx.trace["relevance_mask_excluded"] = list(outcome.excluded_ids)
    return list(outcome.kept)


# MOD SUPERLATIV (IZI): întrebare despre setul AFIȘAT de tip „care dintre ele e cea mai X". ÎNALTĂ
# precizie (ca _COMPARE_RE): „care" + „cea/cel/cele mai" în aceeași frază, SAU „care dintre ele/
# acestea", SAU „cea mai <atribut> dintre ele". Prinde „care e cea mai ușoară/ieftină" (superlativ
# pe setul afișat), NU o căutare nouă („arată-mi ceva mai ieftin" = cheaper). RO/EN/HU. Intenție
# POST-loop (faza E) → trăiește aici (NX-144), nu în `deterministic.py` (intenții PRE-loop).
_ATTR_QUERY_RE = re.compile(
    r"\bcare\b[^?]{0,40}\b(cea|cel|cele|cei)\s+mai\b"
    r"|\bcare\s+dintre\s+(ele|acestea|astea|aceste)\b"
    r"|\b(cea|cel|cele|cei)\s+mai\s+\w+\s+dintre\s+(ele|acestea|astea)\b"
    r"|\bwhich\s+of\s+(these|them)\b|\bwhich\b[^?]{0,40}\b(most|best|cheapest|lightest)\b"
    r"|\bmelyik\b[^?]{0,40}\bleg\w+",
    re.IGNORECASE,
)


@dataclass
class ResponsePlan:
    """Rezultatul fazei E: ce trebuie randat de faza F (`render`). Value-object — planner-ul îl
    umple, `render` îl consumă. `handled=True` = ramura a răspuns DEJA direct (`ctx.reply` setat de
    build_plan: login / cross-sell / „deja cel mai ieftin") → `render` sare peste tur.

    Când `handled=False`, câmpurile de mai jos sunt inputul complet de render (P3: `render` nu
    citește `ToolRun`, ci doar planul). `mode` e derivat DETERMINIST pentru observabilitate/teste;
    dispatch-ul real din `render` păstrează fall-through-urile (comparație→produse, rich→proză)."""

    handled: bool = False
    mode: str = "fallback"  # comparison | rich | prose | order | fallback
    products: list[dict[str, Any]] = field(default_factory=list)
    final: str = ""
    is_order: bool = False
    query: str = ""
    history: str = ""
    commerce_note: str = ""
    inp: PromptInputs | None = None
    # Ieșirile buclei de tool-uri de care are nevoie `render` (extrase din `ToolRun` → decuplare):
    compared: list[dict[str, Any]] = field(default_factory=list)
    generated_links: set[str] = field(default_factory=set)
    grounded_prices: set[float] = field(default_factory=set)
    order_views: list[str] = field(default_factory=list)
    checkout_url: str | None = None
    successful_action_ids: set[str] = field(default_factory=set)


def _plan_mode(
    ctx: TurnContext,
    *,
    compared: list[dict[str, Any]],
    products: list[dict[str, Any]],
    final: str,
    is_order: bool,
    generated_links: set[str],
    grounded_prices: set[float],
) -> str:
    """Derivă `mode`-ul de render din aceleași condiții pe care le dispecerizează `render` (faza F).
    Best-effort pentru observabilitate/teste: `comparison` poate cădea în `render` pe produse dacă
    `build_comparison` întoarce None; `rich` poate cădea pe proză la eșec structurat — dar pentru
    fixture-urile clare (compare / cheaper / gol) reflectă ramura terminală."""
    if compared and not is_order:
        return "comparison"
    if products:
        return "rich" if not is_order else "prose"
    if final:
        if is_order:
            return "order"
        if _valid(final, [], generated_links, grounded_prices):
            return "prose"
        return "fallback"
    if is_order and web_unidentified(ctx):
        return "order"
    return "fallback"


async def build_plan(
    ctx: TurnContext,
    deps: PipelineDeps,
    run: ToolRun,
    inp: PromptInputs,
    *,
    final: str,
    retrieved: list[dict[str, Any]],
    is_order: bool,
    show_more: bool,
    query: str,
    history: str,
    tool_names: list[str],
) -> ResponsePlan:
    """Faza E: shaping determinist post-loop → `ResponsePlan`. Byte-identic cu vechiul bloc din
    `agent_stage`. Ramurile care răspund direct setează `ctx.reply` și întorc `handled=True`."""
    route = ctx.route
    # NX-173 (P0): policy-ul turului, o dată. Faza asta aduce produse din DB pe PATRU căi care nu
    # trec prin `ToolRun` (cross-sell, superlativ pe setul afișat, „mai ieftin", rehidratare de
    # grounding) — fiecare e gate-uită mai jos cu ACEEAȘI decizie (P3: un singur proprietar).
    policy = SafetyPolicy.for_turn(ctx)

    # FAQ-first: dacă modelul a cerut chiar un lookup de comandă pe web anonim (check_order →
    # login_required), servim mesajul de login DETERMINIST. Apare DOAR acum (nu pe toată ruta), după
    # ce agentul a avut șansa să răspundă din FAQ/catalog. cacheable=False (context-relativ).
    if run.order_gated_login:
        ctx.set_reply(login_required_for_ctx(ctx), cacheable=False)
        return ResponsePlan(handled=True)

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
        # state_patch["cart"] = coșul COMPLET merged de cart_add în acest tur; altfel cel din
        # state. NX-237 (sub flag): snapshotul canonic al serviciului, nu starea (o autoritate).
        cart_lines = await _current_cart_lines(ctx, deps, run, fetch=True)
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
        # NX-237 (sub flag): excluderile vin din snapshotul canonic (cart_add tocmai a rulat →
        # `run.cart_snapshot` e setat; nu facem o citire DOAR pentru exclude).
        if get_settings().conversation_cart_enabled:
            cart_lines = await _current_cart_lines(ctx, deps, run, fetch=False)
        else:
            cart_lines = list(ctx.state.cart or []) + list(ctx.state_patch.get("cart") or [])
        exclude_ids = [str(line.get("product_id")) for line in cart_lines if line.get("product_id")]
        complementary, sequence_label = await _cart_followup_products(
            ctx, deps, str(added["id"]), exclude_ids
        )
        # NX-173 (P0): cross-sell-ul e un set NOU, adus direct din DB, în afara `ToolRun` → nu-l
        # vede niciun backstop de tool. Un `cart_add` perfect sigur putea trage un complement
        # contraindicat (review Codex).
        complementary = policy.gate(ctx, complementary, purpose="cross_sell")[0]
        if complementary:
            ctx.retrieval = RetrievalResult(
                products=complementary,
                source="relation_chain" if sequence_label else "cross_sell",
            )
            rich = await _finalize_rich(
                deps.llm,
                prompt_builder.build_rich_system(inp),
                _relation_chain_query(added, ctx.language, sequence_label),
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
                return ResponsePlan(handled=True)
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
        async with deps.db("attr_query_hydrate") as conn:
            hydrated = await get_products_by_ids(conn, ctx.business.id, ids, limit=6)
        # NX-173 (P0): superlativ pe setul AFIȘAT = state vechi (posibil de dinaintea declarației).
        # „care e cea mai bună?" nu are voie să reintroducă un retinoid afișat la turul 1.
        hydrated = policy.gate(ctx, hydrated, purpose="attr_query")[0]
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
        async with deps.db("search_cheaper_than") as conn:
            cheaper = await search_cheaper_than(conn, ctx.business.id, ref_ids, baseline, limit=6)
        # NX-173 (P0): „ceva mai ieftin" e o CĂUTARE NOUĂ în DB, în afara `ToolRun` → gate propriu.
        cheaper = policy.gate(ctx, cheaper, purpose="cheaper")[0]
        ctx.emit("cheaper_followup", baseline=round(baseline, 2), found=len(cheaper))
        if cheaper:
            products = _dedupe(cheaper)
        else:
            # NX-163b: „ceva mai ieftin" + zero rezultate = GOL DE PREȚ în categoria setului
            # afișat — cerere reală pe care catalogul n-o acoperă. Marcat LA SURSĂ (aici se știe
            # că turul a fost o intenție de preț), NU inferat post-hoc din `no_result`: din
            # rollup n-ai cum să distingi „n-am găsit nimic" de „n-am găsit nimic mai ieftin".
            # Dimensiunea = categoria (acțiunea derivată e „adaugă opțiune entry-level"), pragul
            # e preț rotunjit — atribute, nu text de user (P12).
            # `product_ids` = setul AFIȘAT (ref-uri deja în mână, zero query în plus): rollup-ul
            # derivă categoria prin join pe `products`. Dimensiunea se calculează unde se agregă,
            # nu în calea de răspuns a clientului — care n-are voie să crape pentru o etichetă.
            ctx.emit(
                "unmet_query",
                reason="price_gap",
                product_ids=clean_ids(ref_ids),
                price_below=round(baseline, 2),
                locale=ctx.language,
            )
            # Nimic mai ieftin → mesaj sigur (NU cacheabil: e relativ la setul afișat al ACESTUI
            # client; un cache hit l-ar servi altui context — clasa de cache-poisoning știută).
            ctx.set_reply(_cheapest_already_msg(ctx.language), cacheable=False)
            # NX-159 felia 2: mesajul are deja o întrebare, dar atașăm chips de continuare
            # (popular / alt buget / altă categorie) → opțiuni clickabile, nu doar text.
            if get_settings().cheapest_alternatives_enabled:
                ctx.reply.suggestions = _thin_path_chips(ctx.language)
            return ResponsePlan(handled=True)
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
        async with deps.db("rehydrate_displayed") as conn:
            products = await get_products_by_ids(conn, ctx.business.id, ids, limit=6)
        # NX-173 (P0): plasa de grounding rehidratează state vechi → gate ca pe orice altă cale.
        products = policy.gate(ctx, products, purpose="rehydrate")[0]
        rehydrated = True
    # izi-parity hardening: relevanța off-category NUMAI pe calea de căutare PROASPĂTĂ. „Mai ieftin"
    # (set determinist), paginarea și re-hidratarea din state (produse deja arătate, on-topic) NU
    # setează semnalul → compose tratează ca potrivire exactă (fail-open, fără suprimare falsă).
    relevance = None if (cheaper_intent or rehydrated) else run.search_relevance
    # NX-173 (P0) — ENFORCEMENT FINAL: orice ar fi produs căile de mai sus (inclusiv una viitoare
    # care uită gate-ul), aici e ultimul punct înainte ca `ctx.retrieval` să alimenteze validatorul,
    # cardurile și `displayed_products`. Idempotent: pe un set deja gate-uit nu taie nimic.
    products = policy.gate(ctx, products, purpose="retrieval_final")[0]
    # NX-187/NX-257: verdictele se calculează ÎNAINTE de `ctx.retrieval`, fiindcă masca de
    # potrivire are voie să schimbe setul. Shadow-ul rămâne pur observabil (vezi funcția).
    _match_gate_shadow(ctx, products, query)
    products = _apply_relevance_mask(ctx, products)
    ctx.retrieval = RetrievalResult(products=products, source="tools", relevance=relevance)

    return ResponsePlan(
        handled=False,
        mode=_plan_mode(
            ctx,
            compared=run.compared,
            products=products,
            final=final,
            is_order=is_order,
            generated_links=run.generated_links,
            grounded_prices=run.grounded_prices,
        ),
        products=products,
        final=final,
        is_order=is_order,
        query=query,
        history=history,
        commerce_note=commerce_note,
        inp=inp,
        compared=run.compared,
        generated_links=run.generated_links,
        grounded_prices=run.grounded_prices,
        order_views=run.order_views,
        checkout_url=run.checkout_url,
        successful_action_ids=set(run.successful_action_ids),
    )
