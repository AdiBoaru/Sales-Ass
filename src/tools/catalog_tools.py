"""Tool-uri de catalog (G7 Faza 1) — read-only, grounded pe catalog real.

Trei tool-uri pe care agentul le poate chema (max 3/tur): `search_products` (caută),
`get_product_details` (detalii + recenzii D3), `compare_products` (compară 2-3). Toate scoped
pe `ctx.business.id` (modelul NU primește business_id). Argumentele modelului sunt validate
Pydantic ÎNAINTE de execuție. `llm_view` = reprezentare COMPACTĂ (fără PII).
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from src.analytics.demand import product_ids_from_dicts
from src.config import get_settings
from src.db.queries.catalog import (
    get_products_by_ids,
    has_embeddings,
    search_products_lexical,
    search_products_semantic,
)
from src.db.queries.fusion import fuse_candidates
from src.domain.normalize import normalize
from src.models import MAX_SEARCH_POOL, Relevance
from src.tools.base import ToolResult, register
from src.tools.taxonomy import map_concerns

# Candidați per retriever înainte de fuziune (P4: pool intern mare, dar tool result rămâne 6×8
# spre model). ~50 = standardul de product-RAG; recall bun fără să umfle latența.
_FUSION_POOL = 50

# Pool epuizat: semnal pt agent (NX-119b randează mesajul determinist cacheable=False per-locale).
_NO_MORE_VIEW = (
    "(sesiune de căutare epuizată — nu mai sunt alte produse pe filtrele curente. "
    "Spune-i clientului că asta e tot ce ai pe aceste criterii; nu inventa produse.)"
)

if TYPE_CHECKING:
    from src.models import TurnContext
    from src.worker.runner import PipelineDeps


# --- argumente (validare strictă a inputului de la model) --------------------


class SearchArgs(BaseModel):
    query: str = Field(min_length=1)
    price_max: float | None = Field(default=None, ge=0)
    category: str | None = None
    brand: str | None = None
    concerns: list[str] | None = None
    # Tier 2b p2: ingrediente/caracteristici cerute EXPLICIT („cu niacinamidă") → filtru pe
    # DomainPack.searchable_facets (key_ingredients), match normalizat. Altfel [] (fără filtru).
    features: list[str] | None = None
    sort_mode: str = "relevance"  # relevance | price_asc | price_desc | rating_desc (clamp în SQL)
    in_stock_only: bool = False
    limit: int = Field(default=6, ge=1, le=6)
    # A1 (Val1): numele EXACT al unui produs ANUME cerut de client (ex. „Hidra Boost Ultra"). DOAR
    # când clientul numește un produs specific, nu o nevoie. Dacă search NU întoarce un produs care
    # să-l conțină → disclosure „nu există ca atare" (anti-bait-and-switch, ca brand-not-found).
    product_name: str | None = None
    # NX-135: eticheta EXACTĂ de variantă cerută de client (nuanță/mărime, ex. „Warm Beige", „03").
    # Filtru DUR → doar produse care AU o variantă cu eticheta asta (fallback gradat, nivelul 3:
    # „alte game care chiar au Warm Beige"). Cap de lungime = o etichetă, nu o frază.
    variant_label: str | None = Field(default=None, max_length=80)


class DetailArgs(BaseModel):
    product_id: str = Field(min_length=1)


class CompareArgs(BaseModel):
    product_ids: list[str] = Field(min_length=2, max_length=3)


# --- vederi compacte pentru model (≤6×8, fără PII) ---------------------------


def _normname(s: str) -> str:
    """Lowercase + fără diacritice → match robust de nume produs (A1)."""
    d = unicodedata.normalize("NFKD", (s or "").lower())
    return "".join(c for c in d if not unicodedata.combining(c))


def _named_product_found(name: str, products: list[dict[str, Any]]) -> bool:
    """A1: vreun produs ÎNTORS chiar e produsul NUMIT de client? Heuristică deterministă, fără
    wordlist: cele mai LUNGI ≤2 tokenuri distinctive (≥4 litere — brand/model, nu „crema"/„ser")
    trebuie să apară TOATE în numele unui produs. Conservator spre „găsit" (evită disclosure fals):
    declară lipsă DOAR când tokenurile distinctive nu apar în niciun produs (zero incluse)."""
    toks = sorted(
        {t for t in re.findall(r"[a-z0-9]+", _normname(name)) if len(t) >= 4},
        key=len,
        reverse=True,
    )
    key = toks[:2]
    if not key:
        return True  # nimic distinctiv de verificat → nu disclose
    return any(all(t in _normname(p.get("name") or "") for t in key) for p in products)


def _brief(products: list[dict[str, Any]]) -> str:
    if not products:
        return "Niciun produs găsit."
    lines = []
    for p in products:
        rating = f" | {float(p['rating']):.1f}★" if p.get("rating") else ""
        # NX-118: tokenul de stoc → modelul nu mai scrie „este pe stoc" fără bază pe ruta de search.
        avail = f" | stoc: {p['availability']}" if p.get("availability") else ""
        # NX-135: produsul are varianta cerută (search pe variant_label) → fit grounded.
        vmatch = " | are varianta cerută" if p.get("variant_match") else ""
        variants = _variant_view(p.get("variants"), limit=4)
        vline = f" | variante: {variants}" if variants else ""
        summ = (p.get("ai_summary") or "")[:120]
        lines.append(
            f"[{p['id']}] {p['name']} | {p.get('brand') or '-'} | "
            f"{float(p['price']):.2f} lei{rating}{avail}{vmatch}{vline} | {summ}"
        )
    return "\n".join(lines)


def _variant_view(raw_variants: Any, *, limit: int) -> str:
    """Compact variant labels for the model, including per-variant stock when present."""
    labels: list[str] = []
    for v in (raw_variants or [])[:limit]:
        if not isinstance(v, dict):
            continue
        lbl = v.get("label")
        vid = v.get("variant_id") or v.get("id")
        if not lbl or not vid:
            continue
        pr = v.get("price")
        price_str = f", {float(pr):.2f} lei" if pr is not None else ""
        stock = v.get("stock")
        stock_str = f", stoc {int(stock)}" if stock is not None else ""
        attrs = v.get("attributes") if isinstance(v.get("attributes"), dict) else {}
        bits = [
            str(attrs.get(k) or v.get(k))
            for k in ("shade", "undertone", "depth")
            if attrs.get(k) or v.get(k)
        ]
        attrs_str = f", {'/'.join(bits)}" if bits else ""
        labels.append(f"[{vid}] {lbl}{attrs_str}{price_str}{stock_str}")
    return ", ".join(labels)


def _detail_view(p: dict[str, Any]) -> str:
    parts = [
        f"[{p['id']}] {p['name']} ({p.get('brand') or '-'}) — {float(p['price']):.2f} lei",
        f"stoc: {p.get('availability') or '-'}",
    ]
    if p.get("rating"):
        parts.append(f"rating: {float(p['rating']):.1f}★")
    if p.get("ai_summary"):
        parts.append(f"descriere: {p['ai_summary'][:200]}")
    if p.get("review_summary"):
        parts.append(f"recenzii: {p['review_summary'][:200]}")
    if p.get("top_pros"):
        parts.append("plusuri: " + ", ".join(list(p["top_pros"])[:3]))
    if p.get("top_cons"):
        parts.append("minusuri: " + ", ".join(list(p["top_cons"])[:2]))
    # NX-118: variante (nuanțe/mărimi) cu id + PREȚ real → modelul răspunde grounded la „aveți
    # nuanța 03?", recomandă un preț per-variantă acceptat de validator, și poate trimite un
    # `variant_id` REAL la cart_add (membership-ul rămâne plasa). Format `[id] etichetă (preț)`.
    if variants := _variant_view(p.get("variants"), limit=8):
        parts.append("variante: " + variants)
    return " | ".join(parts)


def _compare_view(products: list[dict[str, Any]]) -> str:
    return "\n".join(_detail_view(p) for p in products)


# --- tool-uri ----------------------------------------------------------------


def _relax_ladder(
    *,
    price_max: float | None,
    concerns: list[str] | None,
    category: str | None,
    in_stock_only: bool,
    features: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Trepte de filtre dure, relaxate CUMULATIV ca să iasă ceva relevant înainte de listă goală
    (P6). Brand-ul NU se relaxează niciodată.

    Cu `SEARCH_SORT_MODE_ENABLED` (ARCH-product-retrieval): prețul + disponibilitatea sunt
    constrângeri DURE, NU se relaxează — relaxăm doar SOFTUL (concerns → category). Altfel un
    „sub 80" supra-constrâns ar scoate bound-ul de preț și ar întoarce un 149.99 (bug-ul de preț).
    Fără flag (kill-switch OFF): comportamentul vechi (price → concerns → category)."""
    base = {
        "price_max": price_max,
        "concerns": concerns,
        "category": category,
        "in_stock_only": in_stock_only,
        "features": features,  # Tier 2b p2: relaxat ULTIMUL (hard requirement „cu niacinamidă")
    }
    steps: list[dict[str, Any]] = [base]
    if get_settings().search_sort_mode_enabled:
        # prețul + stocul rămân fixate; relaxăm softul
        if concerns:
            steps.append({**steps[-1], "concerns": None})
        if category:
            steps.append({**steps[-1], "category": None})
    else:
        if price_max is not None:
            steps.append({**steps[-1], "price_max": None})
        if concerns:
            steps.append({**steps[-1], "concerns": None})
        if category:
            steps.append({**steps[-1], "category": None})
    if features:  # feature relaxat DUPĂ category (păstrat cât mai mult; P6 la epuizare)
        steps.append({**steps[-1], "features": None})
    return steps


def _rank_weights(ctx: TurnContext) -> dict[str, float] | None:
    """Ponderile scorului de ranking BLENDED (ARCH-2026 P0) pentru `fuse_candidates`: din
    `DomainPack.rank_weights` (override per-vertical, parțial), altfel `{}` → default-urile din
    fusion.py (`RANK_WEIGHTS`, merge acolo). `None` când kill-switch-ul e OFF → fuziunea cade pe
    `deterministic_rerank` (RRF pur, rating doar pe tie — byte-identic). Determinist, fără I/O."""
    if not get_settings().search_blended_rank_enabled:
        return None
    pack = getattr(ctx.business, "domain_pack", None)
    return (pack.rank_weights if pack else None) or {}


# NX-134: diversificare sortiment. Max produse per brand în prima pagină + acoperirea terțelor de
# preț → sortiment ca un arbore de decizie (ieftin/mediu/scump, mărci diferite), nu top-N clone.
_MAX_PER_BRAND = 2


def _price_tertile(price: float, lo: float, hi: float) -> int:
    """Terța de preț (0=ieftin, 1=mediu, 2=scump) a unui preț în intervalul [lo, hi] al setului.
    Interval degenerat (hi<=lo) → 0 (o singură terță)."""
    if hi <= lo:
        return 0
    frac = (price - lo) / (hi - lo)
    return 0 if frac < 1 / 3 else (1 if frac < 2 / 3 else 2)


def diversify_pool(
    candidates: list[dict[str, Any]], limit: int, *, max_per_brand: int = _MAX_PER_BRAND
) -> list[dict[str, Any]]:
    """Reordonează candidații (DEJA ordonați pe relevanță) ca PRIMELE `limit` să fie DIVERSE — scară
    de preț (terțe) + max `max_per_brand` per brand — păstrând top-1 primul și ordinea de relevanță
    ÎN INTERIORUL selecției. Restul pool-ului urmează în ordinea de relevanță (pt paginare). Greedy
    DETERMINIST (fără random). `len <= limit` → neschimbat (nimic de diversificat).

    Fază 1: acoperă terțele de preț prezente (câte una întâi), respectând cota de brand. Fază 2:
    umple sloturile rămase pe relevanță, relaxând cota când brandurile nu ajung (ex. toate produsele
    de la un singur brand → tot `limit` rezultate, nu 2)."""
    n = len(candidates)
    if limit <= 0 or n <= limit:
        return list(candidates)

    prices = [p["price"] for p in candidates if p.get("price") is not None]
    lo, hi = (min(prices), max(prices)) if prices else (0.0, 0.0)
    tert = [
        None if p.get("price") is None else _price_tertile(float(p["price"]), lo, hi)
        for p in candidates
    ]
    present = {t for t in tert if t is not None}

    selected: list[int] = [0]
    brand_count: dict[Any, int] = {}
    covered: set[int] = set()
    if candidates[0].get("brand"):
        brand_count[candidates[0]["brand"]] = 1
    if tert[0] is not None:
        covered.add(tert[0])

    # Fază 1: greedy pe acoperirea terțelor de preț, sub cota de brand.
    for i in range(1, n):
        if len(selected) >= limit:
            break
        brand = candidates[i].get("brand")
        if brand and brand_count.get(brand, 0) >= max_per_brand:
            continue
        all_covered = present <= covered  # toate terțele prezente deja acoperite
        if tert[i] is None or tert[i] not in covered or all_covered:
            selected.append(i)
            if brand:
                brand_count[brand] = brand_count.get(brand, 0) + 1
            if tert[i] is not None:
                covered.add(tert[i])

    # Fază 2: umple pe relevanță (relaxează cota) → niciodată < limit când există candidați.
    if len(selected) < limit:
        chosen = set(selected)
        for i in range(1, n):
            if len(selected) >= limit:
                break
            if i not in chosen:
                selected.append(i)
                chosen.add(i)

    selected_set = set(selected)
    front = [candidates[i] for i in sorted(selected_set)]  # ordinea de relevanță (top-1 primul)
    rest = [candidates[i] for i in range(n) if i not in selected_set]
    return front + rest


def _searchable_facets(ctx: TurnContext) -> tuple[str, ...]:
    """Tier 2b p2: cheile de attributes filtrabile de search (DomainPack.searchable_facets), gated
    de kill-switch. OFF / fără pack → () → fără filtru de feature."""
    if not get_settings().facet_search_enabled:
        return ()
    pack = getattr(ctx.business, "domain_pack", None)
    return pack.searchable_facets if pack else ()


def _displayed_ids(ctx: TurnContext) -> set[str]:
    """Id-urile produselor deja afișate (din `state.displayed_products`, ref-uri P8) — pentru
    dedup la „arată-mi altele". State gol / lipsă → set gol (fără efect)."""
    state = getattr(ctx, "state", None)
    if state is None:
        return set()
    return {str(p.product_id) for p in getattr(state, "displayed_products", [])}


def _session_filters(
    a: SearchArgs, concern_keys: list[str] | None, features: list[str] | None = None
) -> dict[str, Any]:
    """Setul canonic de filtre care DEFINEȘTE o sesiune de căutare (baza fp-ului). Rafinarea
    oricăruia (preț, concerns, features, brand...) schimbă fp → sesiune nouă (NX-119)."""
    return {
        "query": a.query,
        "category": a.category,
        "brand": a.brand,
        "concerns": concern_keys,
        "features": features,
        "price_max": a.price_max,
        "sort_mode": a.sort_mode,
        "in_stock_only": a.in_stock_only,
    }


def _fp(filters: dict[str, Any]) -> str:
    """Fingerprint determinist al filtrelor → invalidează sesiunea când se schimbă (rafinare)."""
    canon = json.dumps(filters, sort_keys=True, default=str)
    return hashlib.sha1(canon.encode()).hexdigest()[:16]


def _next_page(pool: list[str], cursor: int, seen: set[str], limit: int) -> tuple[list[str], int]:
    """Următoarea pagină de ≤`limit` id-uri NEVĂZUTE din `pool[cursor:]` → (ids, cursor_nou);
    cursor_nou trece peste tot ce s-a consumat (inclusiv id-urile sărite ca deja-văzute)."""
    page: list[str] = []
    i = cursor
    while i < len(pool) and len(page) < limit:
        if pool[i] not in seen:
            page.append(pool[i])
        i += 1
    return page, i


async def continue_search_session(
    ctx: TurnContext, deps: PipelineDeps, sess: dict[str, Any], limit: int
) -> ToolResult:
    """Servește pagina URMĂTOARE dintr-o sesiune activă (NX-119): paginare din `pool[cursor:]` FĂRĂ
    re-fetch/embed, unseen-dedup vs displayed, cursor monotonic. Folosit de `search_products_tool`
    (fp identic) ȘI de ramura deterministă „mai arată-mi" din agent (NX-119b). Pool epuizat / toate
    inactive → semnal determinist `_NO_MORE_VIEW` (P6). Scrie `ctx.state_patch` (persistă proc)."""
    pool: list[str] = sess.get("pool") or []
    cursor = int(sess.get("cursor") or 0)
    seen = _displayed_ids(ctx)
    page_ids, new_cursor = _next_page(pool, cursor, seen, limit)
    page = int(sess.get("page") or 0) + 1
    ctx.state_patch["active_search"] = {**sess, "cursor": new_cursor, "page": page}
    products = (
        await get_products_by_ids(deps.conn, ctx.business.id, page_ids, limit=limit)
        if page_ids
        else []
    )
    if products:
        ctx.emit(
            "search_session",
            action="page",
            page_index=page,
            pool_size=len(pool),
            served=len(products),
            unseen=len(page_ids),
        )
        return ToolResult(ok=True, products=products, llm_view=_brief(products))
    ctx.emit(
        "search_session",
        action="exhausted",
        page_index=page,
        pool_size=len(pool),
        served=0,
        unseen=len(page_ids),
    )
    return ToolResult(ok=True, products=[], llm_view=_NO_MORE_VIEW)


@register("search_products")
async def search_products_tool(
    ctx: TurnContext, deps: PipelineDeps, args: dict[str, Any]
) -> ToolResult:
    """Caută în catalog cu filtre dure (preț, categorie, brand, concerns). Întoarce până la
    6 produse REALE — niciodată „indisponibil".

    HIBRID (NX-113b): rulează AMÂNDOUĂ retrieverele pe pool (~50) — lexical REAL (FTS+pg_trgm,
    NX-113a) ȘI vector (când avem LLM + embeddings) — fuzionate prin RRF (`relevance`) sau
    re-sortate determinist (preț/rating). Filtrele dure care golesc tot se relaxează progresiv
    ÎNAINTE de a întoarce gol (P6). Înainte de trunchierea la 6: dedup vs `displayed_products`
    (paritate „arată altele", P8). Degradare grațioasă la lexical-only fără LLM/embeddings sau
    dacă `embed` pică. Singurul apel extern rămâne `embed([query])` (P2)."""
    a = SearchArgs(**args)
    # Termenii liberi ai clientului („ten gras") → cheile reale din attributes->'concerns' („oily").
    # NX-124: maparea vine din DomainPack (config DB per-vertical), nu hardcodat beauty → generic.
    # Determinist (P2); necunoscutele/pack lipsă → fără filtru fals care golește (P6).
    concern_keys = map_concerns(ctx.business.domain_pack, a.concerns) or None
    # Tier 2b p2: features („cu niacinamidă") → filtru pe searchable_facets, NORMALIZAT (lower+strip
    # diacritice, ca SQL) → „niacinamida"/„niacinamidă" se potrivesc. Fără searchable_facets → None.
    searchable_facets = _searchable_facets(ctx)
    norm_features: list[str] | None = None
    if searchable_facets and a.features:
        norm_features = [
            normalize(f) for f in a.features if isinstance(f, str) and f.strip()
        ] or None
    sessions_on = (
        get_settings().search_sessions_enabled
    )  # kill-switch (OFF → fiecare căutare fresh)
    # IZI-anti-drift: rafinare ÎN sesiune activă, fără categorie/concerns NOI → moștenește-le pe ale
    # sesiunii (ține „raftul" curent). Bug „mai ieftin → mască/ser/toner": user scrie „mai ifetin"
    # (typo) → `cheaper_intent` (regex) ratează → modelul re-caută `price_asc` fără categorie →
    # drift pe alt raft. Moștenirea repară fără wordlist (model+context); DOAR când câmpul NU e
    # re-specificat (schimbare de subiect = modelul setează explicit category → fără moștenire).
    # Observabil prin `search_filter_inherited`.
    sess_filters = (ctx.state.active_search or {}).get("filters") or {}
    if sessions_on and sess_filters:
        inherited: list[str] = []
        if a.category is None and sess_filters.get("category"):
            a.category = sess_filters["category"]
            inherited.append("category")
        if concern_keys is None and sess_filters.get("concerns"):
            concern_keys = [str(x) for x in sess_filters["concerns"]] or None
            inherited.append("concerns")
        if inherited:
            ctx.emit("search_filter_inherited", fields=inherited)
    seen = _displayed_ids(ctx)
    filters = _session_filters(a, concern_keys, norm_features)
    fp = _fp(filters)
    sess = ctx.state.active_search or {}

    # === CONTINUARE sesiune (NX-119): aceleași filtre (fp) + pool stocat → pagina URMĂTOARE,
    # FĂRĂ re-fetch/embed. Paginare deterministă (pool stabil, tie-break p.id) + unseen-dedup.
    if sessions_on and sess.get("fp") == fp and sess.get("pool"):
        return await continue_search_session(ctx, deps, sess, a.limit)

    # === SESIUNE NOUĂ: retrieval hibrid → pool stabil (top MAX_SEARCH_POOL) + prima pagină ===
    ladder = _relax_ladder(
        price_max=a.price_max,
        concerns=concern_keys,
        category=a.category,
        in_stock_only=a.in_stock_only,
        features=norm_features,
    )

    # Vector de query: O SINGURĂ DATĂ (P2), doar cu LLM + embeddings. Dacă `embed` pică → None →
    # degradare grațioasă la lexical-only (P6), fără tăcere.
    query_vec: list[float] | None = None
    if deps.llm is not None and await has_embeddings(deps.conn, ctx.business.id):
        try:
            query_vec = (await deps.llm.embed([a.query]))[0]
        except Exception:  # noqa: BLE001 — embed/rețea pică → cădem pe lexical-only (P6)
            query_vec = None

    # ARCH-2026 P0: ponderile scorului blended (din DomainPack / defaults); None = kill-switch OFF
    # (RRF pur). Calculate O DATĂ (nu se schimbă între treptele de relaxare).
    rank_weights = _rank_weights(ctx)
    ranked_final: list[dict[str, Any]] = []  # ordinea fuzionată+re-rankată la treapta care a produs
    vector_final: list[dict[str, Any]] = []
    relaxed = False
    winning_step: dict[str, Any] | None = None  # treapta din ladder care a produs rezultate
    had_any_match = False  # vreun retriever a întors ceva (semnal brand-not-found)
    relax_depth = 0  # treapta de relaxare la care s-a oprit (0 = filtre stricte)
    lexical_pool_n = vector_pool_n = 0  # mărimea pool-urilor la treapta finală
    top_cosine = None  # cea mai mică distanță cosine (cel mai apropiat vector) — semnal de calitate
    for i, f in enumerate(ladder):
        lexical = await search_products_lexical(
            deps.conn,
            ctx.business.id,
            query_text=a.query,
            price_max=f["price_max"],
            concerns=f["concerns"],
            features=f["features"],
            searchable_facets=searchable_facets,
            variant_label=a.variant_label,  # NX-135: filtru DUR (nu se relaxează), ca brand
            category=f["category"],
            brand=a.brand,
            sort_mode=a.sort_mode,
            in_stock_only=f["in_stock_only"],
            pool=_FUSION_POOL,
        )
        vector: list[dict[str, Any]] = []
        if query_vec is not None:
            try:
                vector = await search_products_semantic(
                    deps.conn,
                    ctx.business.id,
                    query_vec,
                    price_max=f["price_max"],
                    concerns=f["concerns"],
                    features=f["features"],
                    searchable_facets=searchable_facets,
                    variant_label=a.variant_label,  # NX-135: filtru DUR pe variantă
                    category=f["category"],
                    brand=a.brand,  # brand = filtru DUR și pe vector (nu se relaxează)
                    sort_mode=a.sort_mode,
                    in_stock_only=f["in_stock_only"],
                    pool=_FUSION_POOL,
                )
            except Exception:  # noqa: BLE001 — semantic pică în tur → lexical rămâne (P6)
                vector = []
        relax_depth, lexical_pool_n, vector_pool_n = i, len(lexical), len(vector)
        cosines = [p["cosine_distance"] for p in vector if p.get("cosine_distance") is not None]
        if cosines:
            top_cosine = min(cosines)
        ranked = fuse_candidates(
            lexical, vector, sort_mode=a.sort_mode, concerns=concern_keys, weights=rank_weights
        )
        had_any_match = had_any_match or bool(ranked)
        if ranked:
            ranked_final = ranked
            vector_final = vector
            relaxed = i > 0
            winning_step = f
            break

    # NX-134: diversificare sortiment — reordonează pool-ul ca prima pagină să acopere scara de preț
    # + branduri (nu top-N clone). DOAR pe `relevance` (sort explicit = ordinea cerută de client,
    # neatinsă) și NU pe produs numit (A1: căutăm exact acel produs). Top-1/pick-ul nu se mișcă.
    diversified = False
    if (
        get_settings().search_diversify_enabled
        and a.sort_mode == "relevance"
        and a.product_name is None
        and len(ranked_final) > a.limit
    ):
        ranked_final = diversify_pool(ranked_final, a.limit)
        diversified = True

    # Pool-ul sesiunii = ordinea fuzionată COMPLETĂ (top MAX_SEARCH_POOL), NU dedup-uită: dacă l-am
    # semăna din setul minus-displayed, produsele deja afișate ar fi excluse PERMANENT din sesiune +
    # epuizare falsă (review #1). Prima pagină se servește prin ACELAȘI `_next_page` ca paginarea
    # (unseen-dedup vs displayed, P8) → cursorul reflectă poziția în pool, paritate cu paginarea.
    pool_ids = [str(p["id"]) for p in ranked_final][:MAX_SEARCH_POOL]
    by_id = {str(p["id"]): p for p in ranked_final}
    page_ids, cursor = _next_page(pool_ids, 0, seen, a.limit)
    products = [by_id[i] for i in page_ids]
    # NX-135: search filtrat pe variant_label → TOATE rezultatele au varianta cerută (construcție).
    # Marcăm fiecare produs → `_brief` îl semnalează → modelul scrie fit grounded, nu inventat.
    if a.variant_label:
        for p in products:
            p["variant_match"] = True
    # mode=semantic DOAR dacă un produs din vector a SUPRAVIEȚUIT în pagina întoarsă (nu doar
    # „vectorul a întors ceva"): dedup/RRF pot elimina toate hiturile vector → altfel minte.
    vector_ids = {str(v["id"]) for v in vector_final}
    vector_contributed = any(i in vector_ids for i in page_ids)

    # mode=lexical = semnal că jobul de embed trebuie rulat pe tenant (fără vector); =semantic când
    # vectorul a contribuit la setul ÎNTORS. `fused` = ambele retrievere au întors candidați la
    # treapta finală. FĂRĂ `query`/`concerns` text (P12 — doar flag-uri/counts/distanță numerică).
    mode = "semantic" if vector_contributed else "lexical"
    # NX-163: produs NUMIT cerut dar absent din setul întors — precomputat aici (o dată) fiindcă e
    # și semnalul de unmet «named_not_found» (mai jos) și condiția de disclosure (nota de mai jos).
    named_miss = bool(a.product_name) and not _named_product_found(a.product_name, products)
    ctx.emit(
        "product_search",
        mode=mode,
        count=len(products),
        had_price_filter=a.price_max is not None,
        had_category=a.category is not None,
        had_brand=a.brand is not None,
        n_concerns=len(concern_keys or []),
        relaxed=relaxed,
        fused=bool(lexical_pool_n) and bool(vector_pool_n),
        lexical_pool=lexical_pool_n,
        vector_pool=vector_pool_n,
        relax_depth=relax_depth,
        zero_result=not products,
        top_cosine_distance=top_cosine,
        had_variant_label=a.variant_label
        is not None,  # NX-135: căutare de variantă (nuanță/mărime)
        diversified=diversified,  # NX-134: prima pagină a fost re-compusă divers
        brands_in_result=len({p.get("brand") for p in products if p.get("brand")}),
        # NX-163 Demand Capture: ce s-a cerut, ca ref-uri/atribute NORMALIZATE (P8/P12) →
        # raportul de cerere (NX-164). `category_key`/`brand` = filtrele cerute (structurate de
        # triaj, nu text de user); `top_product_ids` = ce a întors search-ul. FĂRĂ query brut/PII.
        top_product_ids=product_ids_from_dicts(products),
        category_key=a.category,
        brand=a.brand,
    )
    # NX-163: cerere neîmplinită = gap de catalog, capturat determinist la sursă (nu inferență LLM).
    # Exclusiv, ca o singură căutare să nu se dubleze: «named_not_found» (produs numit, absent) e
    # mai specific decât «no_result» (nimic nu s-a potrivit — `had_any_match` False peste toate
    # treptele de relaxare). Emis ÎNAINTE de early-return-ul de brand-absent, ca „brand X, 0
    # rezultate" (cel mai valoros semnal) să NU fie pierdut. Doar atribute normalizate + locale.
    if named_miss:
        ctx.emit(
            "unmet_query",
            reason="named_not_found",
            category_key=a.category,
            brand=a.brand,
            locale=ctx.language,
        )
    elif not had_any_match:
        ctx.emit(
            "unmet_query",
            reason="no_result",
            category_key=a.category,
            brand=a.brand,
            locale=ctx.language,
        )
    # NX-119: semează sesiunea de căutare — DOAR id-uri (pool, cap MAX_SEARCH_POOL) + cursor + fp +
    # filtre mici (P8). Următorul „mai arată-mi" (fp identic) paginează din pool fără re-fetch. Doar
    # dacă avem rezultate (zero → nicio sesiune de paginat). Owner scriere: processor (state_patch).
    if products and sessions_on:
        ctx.state_patch["active_search"] = {
            "filters": filters,
            "pool": pool_ids,
            "cursor": cursor,
            "fp": fp,
            "page": 0,
        }
        ctx.emit(
            "search_session",
            action="new",
            page_index=0,
            pool_size=len(pool_ids),
            served=len(products),
            unseen=len(page_ids),
        )
    # Brand cerut + ZERO match real (nu doar zero după dedup) = brandul nu e în catalog. Semnal
    # EXPLICIT pentru agent („nu lucrăm cu brandul X"), nu prezenta alt brand ca al lui (CAT-001).
    # `had_any_match` separă „brand absent" de „brand prezent dar tot ce avea e deja afișat" — în al
    # doilea caz cădem pe răspunsul gol normal (P6), NU pe negarea falsă a brandului (NX-113b).
    if not products and a.brand and not had_any_match:
        return ToolResult(
            ok=True,
            products=[],
            llm_view=(
                f"Nu am găsit niciun produs de la brandul «{a.brand}» în catalog. "
                f"Nu prezenta alt brand ca fiind «{a.brand}». Poți oferi alternative din alte "
                f"branduri, dar spune explicit că sunt alt brand."
            ),
        )

    # A1: produs NUMIT inexistent (nu doar brand) → disclosure anti-bait-and-switch. Produsele
    # întoarse (dacă există) rămân ALTERNATIVE, dar agentul spune clar că nu e cel cerut.
    notes: list[str] = []
    if (
        named_miss
    ):  # NX-163: precomputat mai sus (= același predicat, reuse — și driver de unmet_query)
        ctx.emit("named_product_not_found", alternatives=len(products))
        notes.append(
            f"(produsul «{a.product_name}» nu există ca atare în catalog — NU prezenta alt produs "
            f"ca fiind «{a.product_name}»; cele de mai jos sunt ALTERNATIVE similare, spune clar)"
        )
    # Relaxare cu disclosure: search a renunțat la o constrângere SOFT (nevoie/categorie) ca să iasă
    # ceva → agentul trebuie să fie sincer că nu e potrivire exactă pe ce a cerut (P6, nu tăcere).
    if relaxed:
        notes.append(
            "(relaxat: n-am găsit potrivire exactă pe nevoia/categoria cerută; cele de mai jos "
            "sunt cele mai apropiate — spune sincer clientului că nu e match exact)"
        )
    view = _brief(products)
    if notes:
        view = "\n".join(notes) + "\n" + view
    # izi-parity hardening: semnal de RELEVANȚĂ pentru compose (suprimă „Recomandarea mea"
    # off-category). `category_dropped` = filtrul de categorie cerut a fost renunțat ca să iasă ceva
    # (categorie inexistentă). `top_cosine` = cât de departe e cel mai apropiat vector (prinde
    # free-text fără categorie). Determinist, fără LLM. Fail-open la consumator (None ⇒ exact).
    category_dropped = bool(a.category) and (
        winning_step is not None and winning_step.get("category") is None
    )
    # NX-167 (B): cerere CLARĂ de categorie, dar potrivirea a picat pe ALTĂ ramură (categoria cerută
    # a fost renunțată în relaxare — nici pe arbore nu s-a găsit nimic pe ea) → NU prezenta produse
    # off-category ca match. Suprimă cardurile + semnal de clarificare (P6: nu tăcere — agentul
    # întreabă / oferă o subcategorie, nu minte că e ce a cerut). Curăță și sesiunea, ca
    # „arată-mi altele" (fp identic) să NU pagineze gunoiul off-category suprimat.
    if get_settings().search_offcategory_guard_enabled and category_dropped and products:
        ctx.emit(
            "offcategory_suppressed",
            category_key=a.category,
            relax_depth=relax_depth,
            pool_size=len(products),
        )
        ctx.state_patch.pop("active_search", None)
        return ToolResult(
            ok=True,
            products=[],
            llm_view=(
                f"Nu am găsit produse pe categoria «{a.category}» în catalog. NU prezenta produse "
                f"din altă categorie ca fiind «{a.category}». Întreabă clientul ce anume caută sau "
                f"propune-i o categorie înrudită — nu inventa o potrivire."
            ),
        )
    relevance = Relevance(relaxed=relaxed, category_dropped=category_dropped, top_cosine=top_cosine)
    return ToolResult(ok=True, products=products, llm_view=view, relevance=relevance)


@register("get_product_details")
async def get_product_details_tool(
    ctx: TurnContext, deps: PipelineDeps, args: dict[str, Any]
) -> ToolResult:
    """Detalii complete + rezumat de recenzii (D3) pentru un produs."""
    a = DetailArgs(**args)
    products = await get_products_by_ids(deps.conn, ctx.business.id, [a.product_id], limit=1)
    if not products:
        return ToolResult(ok=False, error="not_found", llm_view="Produsul nu există în catalog.")
    return ToolResult(ok=True, products=products, llm_view=_detail_view(products[0]))


@register("compare_products")
async def compare_products_tool(
    ctx: TurnContext, deps: PipelineDeps, args: dict[str, Any]
) -> ToolResult:
    """Compară 2-3 produse (preț, rating, plusuri/minusuri din recenzii)."""
    a = CompareArgs(**args)
    products = await get_products_by_ids(deps.conn, ctx.business.id, a.product_ids, limit=3)
    if len(products) < 2:
        return ToolResult(
            ok=False,
            products=products,
            error="need_2",
            llm_view="Am nevoie de cel puțin 2 produse existente pentru comparație.",
        )
    return ToolResult(ok=True, products=products, llm_view=_compare_view(products))
