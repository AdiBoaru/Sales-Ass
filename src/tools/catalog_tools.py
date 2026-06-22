"""Tool-uri de catalog (G7 Faza 1) — read-only, grounded pe catalog real.

Trei tool-uri pe care agentul le poate chema (max 3/tur): `search_products` (caută),
`get_product_details` (detalii + recenzii D3), `compare_products` (compară 2-3). Toate scoped
pe `ctx.business.id` (modelul NU primește business_id). Argumentele modelului sunt validate
Pydantic ÎNAINTE de execuție. `llm_view` = reprezentare COMPACTĂ (fără PII).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from src.config import get_settings
from src.db.queries.catalog import (
    get_products_by_ids,
    has_embeddings,
    search_products_lexical,
    search_products_semantic,
)
from src.db.queries.fusion import fuse_candidates
from src.tools.base import ToolResult, register
from src.tools.taxonomy import map_concerns

# Candidați per retriever înainte de fuziune (P4: pool intern mare, dar tool result rămâne 6×8
# spre model). ~50 = standardul de product-RAG; recall bun fără să umfle latența.
_FUSION_POOL = 50

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
    sort_mode: str = "relevance"  # relevance | price_asc | price_desc | rating_desc (clamp în SQL)
    in_stock_only: bool = False
    limit: int = Field(default=6, ge=1, le=6)


class DetailArgs(BaseModel):
    product_id: str = Field(min_length=1)


class CompareArgs(BaseModel):
    product_ids: list[str] = Field(min_length=2, max_length=3)


# --- vederi compacte pentru model (≤6×8, fără PII) ---------------------------


def _brief(products: list[dict[str, Any]]) -> str:
    if not products:
        return "Niciun produs găsit."
    lines = []
    for p in products:
        rating = f" | {float(p['rating']):.1f}★" if p.get("rating") else ""
        lines.append(
            f"[{p['id']}] {p['name']} | {p.get('brand') or '-'} | "
            f"{float(p['price']):.2f} lei{rating} | {(p.get('ai_summary') or '')[:120]}"
        )
    return "\n".join(lines)


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
    return steps


def _displayed_ids(ctx: TurnContext) -> set[str]:
    """Id-urile produselor deja afișate (din `state.displayed_products`, ref-uri P8) — pentru
    dedup la „arată-mi altele". State gol / lipsă → set gol (fără efect)."""
    state = getattr(ctx, "state", None)
    if state is None:
        return set()
    return {str(p.product_id) for p in getattr(state, "displayed_products", [])}


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
    # Determinist (P2), per vertical; necunoscutele se ignoră (fără filtru fals care golește).
    concern_keys = map_concerns(ctx.business.vertical, a.concerns) or None
    ladder = _relax_ladder(
        price_max=a.price_max,
        concerns=concern_keys,
        category=a.category,
        in_stock_only=a.in_stock_only,
    )
    seen = _displayed_ids(ctx)

    # Vector de query: O SINGURĂ DATĂ (P2), doar cu LLM + embeddings. Dacă `embed` pică → None →
    # degradare grațioasă la lexical-only (P6), fără tăcere.
    query_vec: list[float] | None = None
    if deps.llm is not None and await has_embeddings(deps.conn, ctx.business.id):
        try:
            query_vec = (await deps.llm.embed([a.query]))[0]
        except Exception:  # noqa: BLE001 — embed/rețea pică → cădem pe lexical-only (P6)
            query_vec = None

    products: list[dict[str, Any]] = []
    relaxed = False
    vector_contributed = False
    had_any_match = False  # vreun retriever a întors ceva ÎNAINTE de dedup (semnal brand-not-found)
    for i, f in enumerate(ladder):
        lexical = await search_products_lexical(
            deps.conn,
            ctx.business.id,
            query_text=a.query,
            price_max=f["price_max"],
            concerns=f["concerns"],
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
                    category=f["category"],
                    brand=a.brand,  # brand = filtru DUR și pe vector (nu se relaxează)
                    sort_mode=a.sort_mode,
                    in_stock_only=f["in_stock_only"],
                    pool=_FUSION_POOL,
                )
            except Exception:  # noqa: BLE001 — semantic pică în tur → lexical rămâne (P6)
                vector = []
        fused = fuse_candidates(lexical, vector, sort_mode=a.sort_mode)
        had_any_match = had_any_match or bool(fused)
        # Dedup vs produsele deja afișate ÎNAINTE de trunchiere (paritate „arată altele", P8).
        deduped = [p for p in fused if str(p["id"]) not in seen]
        if deduped:
            products = deduped[: a.limit]
            relaxed = i > 0
            # mode=semantic DOAR dacă un produs din vector a SUPRAVIEȚUIT în setul întors (nu doar
            # „vectorul a întors ceva"): dedup/RRF pot elimina toate hiturile vector → altfel minte.
            vector_ids = {str(v["id"]) for v in vector}
            vector_contributed = any(str(p["id"]) in vector_ids for p in products)
            break

    # mode=lexical = semnal că jobul de embed trebuie rulat pe tenant (fără vector); =semantic când
    # vectorul a contribuit la setul întors. FĂRĂ `query`/`concerns` text (P12). (NX-113c extinde
    # emit-ul cu `fused`/pool-counts/`top_cosine_distance`/`relax_depth`/`zero_result`.)
    mode = "semantic" if vector_contributed else "lexical"
    ctx.emit(
        "product_search",
        mode=mode,
        count=len(products),
        had_price_filter=a.price_max is not None,
        had_category=a.category is not None,
        had_brand=a.brand is not None,
        n_concerns=len(concern_keys or []),
        relaxed=relaxed,
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
    return ToolResult(ok=True, products=products, llm_view=_brief(products))


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
