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
from src.tools.base import ToolResult, register
from src.tools.taxonomy import map_concerns

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


@register("search_products")
async def search_products_tool(
    ctx: TurnContext, deps: PipelineDeps, args: dict[str, Any]
) -> ToolResult:
    """Caută în catalog cu filtre dure (preț, categorie, brand, concerns). Întoarce până la
    6 produse REALE — niciodată „indisponibil".

    Calea bună = semantic (embedding query × `product_embeddings`, filtrat pe categorie/concerns).
    Plasa (NX-98) = SQL-only (`name ilike '%q%'`, ZERO halucinație) când tenantul n-are embeddings,
    n-avem LLM pentru vectorul de query, sau semantic întoarce gol. Filtrele dure care golesc tot
    se relaxează progresiv (price → concerns → category) ÎNAINTE de a întoarce gol (P6). Singurul
    apel extern rămâne `embed([query])` pe calea semantică (P2); SQL-only n-are LLM deloc.
    """
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

    products: list[dict[str, Any]] = []
    mode = "lexical"  # default = plasa lexicală (FTS+trgm); urcă la "semantic" dacă avem vector
    relaxed = False

    # 1. cale semantică doar dacă avem LLM (vector de query) ȘI tenantul are embeddings
    use_semantic = deps.llm is not None and await has_embeddings(deps.conn, ctx.business.id)
    if use_semantic:
        try:
            query_vec = (await deps.llm.embed([a.query]))[0]
            for i, f in enumerate(ladder):
                products = await search_products_semantic(
                    deps.conn,
                    ctx.business.id,
                    query_vec,
                    price_max=f["price_max"],
                    concerns=f["concerns"],
                    category=f["category"],
                    brand=a.brand,  # brand = filtru DUR și pe calea semantică (nu se relaxează)
                    sort_mode=a.sort_mode,
                    in_stock_only=f["in_stock_only"],
                    limit=a.limit,
                )
                if products:
                    mode, relaxed = "semantic", i > 0
                    break
        except Exception:  # noqa: BLE001 — embed/rețea pică → NU tăcem, cădem pe SQL-only (P6)
            products = []
    # 2. plasă LEXICALĂ (NX-113a): fără embeddings/LLM, semantic gol, sau embed a aruncat. Acum
    #    lexical REAL (FTS `websearch_to_tsquery` + `pg_trgm`), nu `name ILIKE '%q%'` spart — prinde
    #    fraze naturale ȘI SKU/typo. Filtrele dure (category/brand/concerns) se păstrează.
    if not products:
        mode = "lexical"
        for i, f in enumerate(ladder):
            products = await search_products_lexical(
                deps.conn,
                ctx.business.id,
                query_text=a.query,
                price_max=f["price_max"],
                concerns=f["concerns"],
                category=f["category"],
                brand=a.brand,
                sort_mode=a.sort_mode,
                in_stock_only=f["in_stock_only"],
                pool=a.limit,
            )
            if products:
                relaxed = i > 0
                break
    # `mode=lexical` în analytics = semnal că jobul de embed trebuie rulat pe tenant (fără vector).
    # FĂRĂ `query`/`concerns` text în properties (P12 — pot conține formulări PII); doar flag-uri.
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
    # Brand cerut explicit + zero rezultate = brandul nu e în catalog. Semnal EXPLICIT pentru agent
    # (raportează DOAR ce întoarce tool-ul) ca să spună „nu lucrăm cu brandul X", nu să prezinte
    # produse de la alt brand ca și cum ar fi al lui (fix CAT-001 / brand-availability).
    if not products and a.brand:
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
