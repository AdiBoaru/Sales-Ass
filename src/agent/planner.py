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

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from src.agent import prompt_builder
from src.agent.deterministic import _CHEAPER_RE
from src.agent.fallbacks import (
    _cart_confirm_msg,
    _cheapest_already_msg,
    _cross_sell_query,
    _dedupe,
    _thin_path_chips,
)
from src.agent.finalize import _finalize_rich
from src.agent.validator import _valid
from src.config import get_settings
from src.db.queries.catalog import (
    get_complementary_products,
    get_products_by_ids,
    search_cheaper_than,
)
from src.models import RetrievalResult, TurnContext
from src.safety.policy import SafetyPolicy
from src.worker import compose
from src.worker.order_gate import login_required_for_ctx, web_unidentified

if TYPE_CHECKING:
    from src.agent.prompt_builder import PromptInputs
    from src.agent.tool_executor import ToolRun
    from src.worker.runner import PipelineDeps

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
    # NX-181: forma de răspuns (hint determinist pt Prompt vNext), owner unic = planner (P3).
    # Vocabular UNIC `response_shape`: recommendation | direct_followup | detail. Consumat de
    # `render` DOAR când `prompt_vnext_enabled` (altfel ignorat → comportament byte-identic).
    response_shape: str = "recommendation"


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
        # NX-173 (P0): cross-sell-ul e un set NOU, adus direct din DB, în afara `ToolRun` → nu-l
        # vede niciun backstop de tool. Un `cart_add` perfect sigur putea trage un complement
        # contraindicat (review Codex).
        complementary = policy.gate(ctx, complementary, purpose="cross_sell")[0]
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
        hydrated = await get_products_by_ids(deps.conn, ctx.business.id, ids, limit=6)
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
        cheaper = await search_cheaper_than(deps.conn, ctx.business.id, ref_ids, baseline, limit=6)
        # NX-173 (P0): „ceva mai ieftin" e o CĂUTARE NOUĂ în DB, în afara `ToolRun` → gate propriu.
        cheaper = policy.gate(ctx, cheaper, purpose="cheaper")[0]
        ctx.emit("cheaper_followup", baseline=round(baseline, 2), found=len(cheaper))
        if cheaper:
            products = _dedupe(cheaper)
        else:
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
        products = await get_products_by_ids(deps.conn, ctx.business.id, ids, limit=6)
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
    ctx.retrieval = RetrievalResult(products=products, source="tools", relevance=relevance)

    # NX-181: forma de răspuns din semnalele DEJA calculate (fără LLM nou). detail = deep-dive pe 1
    # produs afișat; direct_followup = follow-up pe setul afișat (superlativ/mai-ieftin/rehidratat);
    # altfel recommendation (căutare proaspătă). Hint pt Prompt vNext, consumat doar când flag ON.
    followup = attr_query or cheaper_intent or rehydrated
    if followup and len(products) == 1:
        response_shape = "detail"
    elif followup:
        response_shape = "direct_followup"
    else:
        response_shape = "recommendation"

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
        response_shape=response_shape,
    )
