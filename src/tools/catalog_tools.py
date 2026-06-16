"""Tool-uri de catalog (G7 Faza 1) — read-only, grounded pe catalog real.

Trei tool-uri pe care agentul le poate chema (max 3/tur): `search_products` (caută),
`get_product_details` (detalii + recenzii D3), `compare_products` (compară 2-3). Toate scoped
pe `ctx.business.id` (modelul NU primește business_id). Argumentele modelului sunt validate
Pydantic ÎNAINTE de execuție. `llm_view` = reprezentare COMPACTĂ (fără PII).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from src.db.queries.catalog import (
    get_products_by_ids,
    has_embeddings,
    search_products,
    search_products_semantic,
)
from src.tools.base import ToolResult, register

if TYPE_CHECKING:
    from src.models import TurnContext
    from src.worker.runner import PipelineDeps


# --- argumente (validare strictă a inputului de la model) --------------------


class SearchArgs(BaseModel):
    query: str = Field(min_length=1)
    price_max: float | None = Field(default=None, ge=0)
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


@register("search_products")
async def search_products_tool(
    ctx: TurnContext, deps: PipelineDeps, args: dict[str, Any]
) -> ToolResult:
    """Caută în catalog. Întoarce până la 6 produse REALE — niciodată „indisponibil".

    Calea bună = semantic (embedding query × `product_embeddings`). Plasa (NX-98) =
    SQL-only (`name ilike '%q%'`, ZERO halucinație) când tenantul n-are embeddings,
    n-avem LLM pentru vectorul de query, sau semantic întoarce gol. Degradare
    deterministă (P6): tot iese ceva, nu listă goală structurală. Singurul apel extern
    rămâne `embed([query])` pe calea semantică (P2); SQL-only n-are LLM deloc.
    """
    a = SearchArgs(**args)
    # 1. cale semantică doar dacă avem LLM (vector de query) ȘI tenantul are embeddings
    use_semantic = deps.llm is not None and await has_embeddings(deps.conn, ctx.business.id)
    products: list[dict[str, Any]] = []
    mode = "sql_only"
    if use_semantic:
        try:
            query_vec = (await deps.llm.embed([a.query]))[0]
            products = await search_products_semantic(
                deps.conn, ctx.business.id, query_vec, price_max=a.price_max, limit=a.limit
            )
            if not products and a.price_max is not None:
                # bugetul a tăiat tot → reia fără filtru (ranking semantic decide)
                products = await search_products_semantic(
                    deps.conn, ctx.business.id, query_vec, limit=a.limit
                )
            if products:
                mode = "semantic"
        except Exception:  # noqa: BLE001 — embed/rețea pică → NU tăcem, cădem pe SQL-only (P6)
            products = []
    # 2. plasă SQL-only: fără embeddings/LLM, semantic gol, sau embed a aruncat
    if not products:
        products = await search_products(
            deps.conn, ctx.business.id, query_text=a.query, price_max=a.price_max, limit=a.limit
        )
        if not products and a.price_max is not None:
            products = await search_products(
                deps.conn, ctx.business.id, query_text=a.query, limit=a.limit
            )
    # `sql_only` în analytics = semnal că jobul de embed trebuie rulat pe tenant.
    # FĂRĂ `query` în properties (P12 — poate conține nume/adrese/PII).
    ctx.emit(
        "product_search", mode=mode, count=len(products), had_price_filter=a.price_max is not None
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
