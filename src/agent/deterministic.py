"""Faza B — intenții deterministe PRE-loop (NX-143). Early-exit ÎNAINTE de bucla LLM, cost $0 de
inferență: cererea se poate servi din setul deja afișat (`ctx.state.displayed_products` / sesiunea
activă), fără să chemăm modelul.

Trei intenții:
  • `link`    — „trimite-mi linkul" pe produs afișat → `Offer(open_url)` + card (NX-131).
  • `compare` — „compară primele două" → tabel structurat determinist (IZI-parity G2).
  • `show_more` — „mai arată-mi" pe o sesiune activă → paginare (predicatul e aici; paginarea
    propriu-zisă rămâne cablată în `stage.py`/GENERATE via `continue_search_session`, NX-119b).

Contract: `try_pre_intents(ctx, deps) -> bool` (True = tratat, `stage.py` face return) + predicatul
pur `is_show_more(ctx)`. Gating pur (predicat pe `ctx` + regex) + handler care setează `ctx.reply`
(P5). `_CHEAPER_RE` trăiește AICI și e partajat cu shaping-ul post-loop (planner, NX-144) — un
follow-up de preț («mai ieftin») NU e link/compare/paginare, ci re-căutare (cheaper_intent).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from src.agent.fallbacks import (
    _card_products,
    _compare_chips,
    _link_lead,
    _no_link_msg,
    _view_label,
)
from src.config import get_settings
from src.db.queries.catalog import get_products_by_ids, product_category_roots
from src.models import Offer, RetrievalResult, Route, TurnContext
from src.safety.policy import SafetyPolicy
from src.worker import compose

if TYPE_CHECKING:
    from src.worker.runner import PipelineDeps

# P1 (ARCH-product-retrieval): follow-up de PREȚ pe un set deja afișat → re-căutare DETERMINISTĂ a
# produselor strict mai ieftine (search_cheaper_than), NU re-rank pe setul afișat (R3). Precizie
# mare RO/HU/EN (comparativ/superlativ de preț). Un miss cade grațios pe comportamentul vechi (R3).
# Partajat cu planner-ul (NX-144): gating-ul link/compare/show_more exclude «mai ieftin».
_CHEAPER_RE = re.compile(
    r"\bmai\s+ieftin\w*|\bcea\s+mai\s+ieftin\w*|\bmai\s+accesibil\w*"
    r"|\bpre[țt]\s+mai\s+mic|\bmai\s+mic\s+la\s+pre[țt]|\bbuget\s+mai\s+mic"
    r"|\bprea\s+scump\w*|\bcam\s+scump\w*"
    r"|\bcheaper\b|\bcheapest\b|\bolcs[óo]bb\w*|\blegolcs[óo]bb\w*",
    re.IGNORECASE,
)

# IZI-parity (Tier 1, G2): intenție de COMPARAȚIE pe un set deja afișat → tabel DETERMINIST (ca
# cheaper/show_more/link), fără să depindem de modelul care cheamă `compare_products`. RO/EN/HU,
# agnostic de vertical. ÎNALTĂ PRECIZIE deliberat (gate-ul n-are recurs la model pe fals-pozitiv):
# DOAR verbul „a compara" + „versus/vs" + verbul HU. `compar[aăie]\w*` prinde compara/compară/
# comparați/comparație ȘI EN compare/comparison/comparing, dar NU „compartiment" (compar+t). Frazele
# laxe („ce diferență e între ele") cad pe calea model-driven (modelul cheamă compare_products) — nu
# le prindem determinist ca să nu confundăm „diferența dintre garanție și retur" cu o comparație.
_COMPARE_RE = re.compile(
    r"\bcompar[aăie]\w*|\bversus\b|\bvs\.?\b|\b[öo]sszehasonl\w*|\bhasonl[íi]ts\w*",
    re.IGNORECASE,
)
# „trei/three/3/három" în cerere → comparăm 3; altfel 2 (perechea = cazul dominant „primele două").
_THREE_RE = re.compile(r"\btrei\b|\bthree\b|\b3\b|\bh[áa]rom\b", re.IGNORECASE)

# NX-119b: „mai arată-mi"/„show more" = INTENȚIE de PAGINARE (NU un tool nou). Pe o sesiune activă
# → pagina următoare DETERMINIST, fără bucla LLM. NU prinde „mai ieftin" (= cheaper_intent). Ancorat
# pe sensul de paginare: „mai multe" PLURAL bare/terminal sau + obiect de listă — NU „mai multă/mult
# X" (rafinare „mai multă hidratare") și nu „și alte INGREDIENTE" (rafinare). Rafinările cu cuvânt
# „more" sunt prinse oricum de gate-ul `not route.filters` (cad pe bucla LLM → sesiune nouă).
_MORE_RE = re.compile(
    r"\bmai\s+arat\w*"  # „mai arată-mi"
    r"|\bmai\s+multe\b(?!\s+\w)"  # „mai multe" bare/terminal (NU „mai multă/mult X")
    r"|\bmai\s+multe\s+(?:produse|op[țt]iuni|variante|rezultate|exemple)\b"
    r"|\balte\s+(?:op[țt]iuni|variante|produse)\b|\b[șs]i\s+alte\s+(?:op[țt]iuni|variante|produse)\b"
    r"|\baltele\b|\bmai\s+vreau\b"
    r"|\bshow\s+more\b|\bmore\s+(?:options|products|results)\b|\bother\s+(?:options|ones)\b"
    r"|\bt[öo]bbet\b",  # HU: többet (mai mult)
    re.IGNORECASE,
)

# NX-131: cerere de LINK la un produs DEJA arătat („trimite-mi linkul / dă-mi link direct / unde-l
# cumpăr"). Intenție DETERMINISTĂ (ca _CHEAPER_RE/_MORE_RE): calea rich INTERZICE structural
# modelului linkurile (regulile rich) → o cerere de link cădea în re-randarea bogată cu coaching
# repetat (bug live: „partea asta e foarte repetitiva"). Ancorat pe „link" (link/linkul/linkuri,
# RO/EN) + fraze de cumpărare. `\blink\w*` NU prinde „hyperlink"/„blink" (fără boundary înainte).
# Gated în `try_pre_intents` pe displayed_products + SALES + fără filtre noi (cu filtre = fresh).
_LINK_RE = re.compile(
    r"\blink\w*"
    r"|\bunde\s+(?:o\s+|[îi]l\s+|le\s+)?(?:pot\s+)?(?:cump[ăa]r|comand|g[ăa]sesc)\w*"
    r"|\bwhere\s+(?:can\s+i\s+|to\s+)?(?:buy|get|find)\b"
    r"|\bhol\s+(?:tudom\s+)?(?:veszem|vehetem|megvenni|megveszem)\b",
    re.IGNORECASE,
)


async def _handle_link_intent(ctx: TurnContext, deps: PipelineDeps) -> None:
    """Servește o cerere de LINK pe produsele DEJA arătate, FĂRĂ bucla LLM (NX-131) — ca
    show_more/cheaper. State ține doar ref-uri (P8) → fetch `product_url` PROASPĂT din catalog
    (sursa de adevăr). Link real → Offer(open_url) + card(uri); `product_url` NULL (gaură de date
    demo) → mesaj ONEST, NU link inventat (PP-F4). Mereu setează un reply (P6, niciodată tăcere)."""
    ids = [p.product_id for p in ctx.state.displayed_products]
    products = await get_products_by_ids(deps.conn, ctx.business.id, ids, limit=6)
    # NX-173 (P0): `displayed_products` e STATE VECHI — poate conține produse afișate ÎNAINTE ca
    # clientul să declare contextul („arată-mi seruri" → „sunt însărcinată, dă-mi linkul"). Calea
    # asta iese înainte de tool loop, deci backstop-ul din executor n-o vede: gate aici, ori deloc.
    products = SafetyPolicy.for_turn(ctx).gate(ctx, products, purpose="link")[0]
    ctx.retrieval = RetrievalResult(products=products, source="link_intent")
    with_url = [p for p in products if p.get("url")]
    if not with_url:
        # Fără product_url → onest + pasul care există. NU re-afișăm cardul (ar repeta exact ce a
        # frustrat clientul în bucla veche); doar mesajul onest. cacheable=False (context-specific).
        ctx.emit("link_intent", served=0, in_context=len(products))
        ctx.set_reply(_no_link_msg(ctx.language), cacheable=False)
        return
    ctx.emit("link_intent", served=len(with_url))
    cards = _card_products(with_url, n=6)
    if len(with_url) == 1:
        # Un singur produs țintă → buton CTA (open_url). set_reply ÎNTÂI (creează reply-ul), apoi
        # set_offer (îl mută pe el — ordinea contează: set_offer cere un reply deja setat).
        ctx.set_reply(_link_lead(ctx.language, many=False), products=cards, cacheable=False)
        ctx.set_offer(
            Offer(kind="open_url", label=_view_label(ctx.language), url=with_url[0]["url"])
        )
    else:
        # Mai multe → cardurile SUNT linkurile (fiecare cu url-ul lui); fără un buton unic arbitrar.
        ctx.set_reply(_link_lead(ctx.language, many=True), products=cards, cacheable=False)


def _comparison_facets(ctx: TurnContext) -> tuple:
    """Tier 2 (IZI-parity): fațetele de DOMENIU din DomainPack pentru tabelul de comparație
    (finish/acoperire/potrivit-pentru/..., din products.attributes), gated de kill-switch. OFF /
    fără pack → () → tabelul are doar rândurile generice (preț/rating/avantaje/brand), ca azi."""
    if not get_settings().comparison_facets_enabled:
        return ()
    pack = getattr(ctx.business, "domain_pack", None)
    return pack.comparison_facets if pack else ()


async def _handle_compare_intent(ctx: TurnContext, deps: PipelineDeps, query: str) -> bool:
    """Servește o COMPARAȚIE pe produsele DEJA afișate, FĂRĂ bucla LLM (G2, IZI-parity) — ca
    link/show_more/cheaper. State ține doar ref-uri (P8) → re-fetch detaliile proaspete (preț/
    rating/avantaje/minusuri/disponibilitate/brand) → tabel structurat DETERMINIST (build_comparison
    → zero proză LLM în celule). Implicit primele 2 (perechea = cazul dominant „compară primele
    două"); „trei/3/három" → 3. `get_products_by_ids` păstrează ORDINEA afișată (deixis ordinal
    corect). <2 valide → False → cade pe bucla LLM (caută/compară fresh). True = a servit turul."""
    n = 3 if _THREE_RE.search(query) else 2
    ids = [p.product_id for p in ctx.state.displayed_products][:n]
    products = await get_products_by_ids(deps.conn, ctx.business.id, ids, limit=n)
    # NX-173 (P0): ca la link — set vechi din state, cale care ocolește tool loop-ul. Dacă
    # gate-ul taie sub 2, întoarcem False → cade pe bucla LLM, care caută FRESH (deja filtrat de
    # policy) — nu comparăm tăcut ce a mai rămas și nu prezentăm produsul exclus.
    products = SafetyPolicy.for_turn(ctx).gate(ctx, products, purpose="compare_intent")[0]
    if len(products) < 2:
        return False
    # NX-167 (C): nu compara produse din ramuri INCOERENTE (root-branch diferit din
    # `categories.path`, ex. machiaj vs. par) — tabelul „fond vs. accesoriu de păr" e absurd.
    # Fail-open la `path` lipsă (produse fără root nu contează). ≥2 root-uri distincte → `return
    # False` → cade pe bucla LLM (poate re-căuta coerent), nu un mesaj-perete. Kill-switch OFF →
    # comportamentul vechi (compară orice 2 afișate).
    if get_settings().compare_coherence_guard_enabled and len(products) >= 2:
        roots = await product_category_roots(
            deps.conn, ctx.business.id, [p["id"] for p in products]
        )
        distinct = {r for r in roots.values() if r}
        if len(distinct) >= 2:
            ctx.emit("compare_incoherent_blocked", n=len(products), root_branches=len(distinct))
            return False
    comparison = compose.build_comparison(products, ctx.language, _comparison_facets(ctx))
    if comparison is None:
        return False
    ctx.retrieval = RetrievalResult(products=products, source="compare_intent")
    ctx.set_comparison_reply(
        comparison,
        text=compose.flatten_comparison(comparison, ctx.language),
        products=compose.comparison_cards(comparison),
        chips=_compare_chips(comparison.columns, ctx.language),
    )
    ctx.emit("agent_compared", n=len(comparison.columns), deterministic=True)
    return True


async def try_pre_intents(ctx: TurnContext, deps: PipelineDeps) -> bool:
    """Faza B: intenții deterministe PRE-loop (link + compare). True = tratat (early-exit din
    `stage.py`); False = lasă bucla LLM. Doar SALES; toate exclud «mai ieftin» (cheaper_intent) și
    o căutare nouă (`route.filters`)."""
    route = ctx.route
    if route is None or route.route != Route.SALES:
        return False
    query = (ctx.message.body or "").strip()
    if not query:
        return False

    # NX-131: cerere de LINK pe un produs deja arătat = intenție DETERMINISTĂ, NU re-recomandare.
    # Calea rich interzice modelului linkurile → cererea de link cădea în re-randarea bogată cu
    # coaching repetat. O servim direct din state → product_url proaspăt → Offer(open_url) + card.
    link_intent = (
        get_settings().link_intent_enabled
        and bool(ctx.state.displayed_products)
        and not route.filters
        and _LINK_RE.search(query) is not None
        and _CHEAPER_RE.search(query) is None
    )
    if link_intent:
        await _handle_link_intent(ctx, deps)
        return True

    # IZI-parity G2: COMPARAȚIE pe setul afișat → tabel structurat determinist, fără să depindem de
    # model. ≥2 afișate; fără filtre noi; NU «mai ieftin». <2 valide la fetch → handler False.
    compare_intent = (
        get_settings().compare_intent_enabled
        and len(ctx.state.displayed_products) >= 2
        and not route.filters
        and _COMPARE_RE.search(query) is not None
        and _CHEAPER_RE.search(query) is None
    )
    if compare_intent and await _handle_compare_intent(ctx, deps, query):
        return True
    return False


def is_show_more(ctx: TurnContext) -> bool:
    """Faza B: predicat de PAGINARE („mai arată-mi" pe o sesiune activă). Paginarea propriu-zisă
    (`continue_search_session`) rămâne în `stage.py`/GENERATE. `not route.filters`: constrângeri NOI
    = RAFINARE (cade pe bucla LLM), nu paginare pură. NU pe «mai ieftin» (cheaper_intent)."""
    route = ctx.route
    if route is None or route.route != Route.SALES:
        return False
    query = (ctx.message.body or "").strip()
    return (
        get_settings().search_sessions_enabled
        and bool(ctx.state.active_search)
        and not route.filters
        and _MORE_RE.search(query) is not None
        and _CHEAPER_RE.search(query) is None
    )
