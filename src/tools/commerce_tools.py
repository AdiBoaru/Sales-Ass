"""Tool-uri de comerț (F2, bucla de bani) — primul WRITE tool al agentului.

`checkout_link(cart_items)` construiește un link de cumpărare cu `?ref=<ref_code>` și scrie
un rând în `checkout_links` (ancora de atribuire). Determinist, scoped pe `ctx.business.id`
(modelul NU primește business_id). Validează produsele contra catalogului — nu generăm link
pentru produse inexistente. Linkul generat e returnat în `ToolResult.links` → validatorul
(stagiul 8) îl acceptă (grounded prin construcție).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from src.config import get_settings
from src.db.queries.catalog import get_products_by_ids
from src.db.queries.commerce import create_checkout_link
from src.tools.base import ToolResult, register

if TYPE_CHECKING:
    from src.models import TurnContext
    from src.worker.runner import PipelineDeps


# --- argumente (validare strictă a inputului de la model) --------------------


class CartItem(BaseModel):
    product_id: str = Field(min_length=1)
    variant_id: str | None = None
    quantity: int = Field(default=1, ge=1, le=99)


class CheckoutArgs(BaseModel):
    cart_items: list[CartItem] = Field(min_length=1, max_length=10)


def _checkout_base(ctx: TurnContext) -> str:
    """Base URL de checkout: settings-ul businessului are prioritate, apoi config global.
    Gol → checkout indisponibil (NU inventăm domeniu)."""
    per_business = (ctx.business.settings or {}).get("checkout_url")
    return (per_business or get_settings().checkout_base_url or "").strip()


@register("checkout_link")
async def checkout_link_tool(
    ctx: TurnContext, deps: PipelineDeps, args: dict[str, Any]
) -> ToolResult:
    """Creează un link de cumpărare atribuibil (`?ref=`) pentru coșul cerut."""
    a = CheckoutArgs(**args)

    base = _checkout_base(ctx)
    if not base:
        return ToolResult(
            ok=False, error="no_checkout_url", llm_view="Checkout indisponibil momentan."
        )

    # Validăm produsele contra catalogului (scoped pe business) — nu linkuim ce nu există.
    ids = list(dict.fromkeys(it.product_id for it in a.cart_items))
    products = await get_products_by_ids(deps.conn, ctx.business.id, ids, limit=6)
    by_id = {p["id"]: p for p in products}

    cart: list[dict[str, Any]] = []
    total = 0.0
    for it in a.cart_items:
        p = by_id.get(it.product_id)
        if p is None or p.get("price") is None:
            continue
        price = round(float(p["price"]), 2)
        cart.append(
            {
                "product_id": it.product_id,
                "variant_id": it.variant_id,
                "name": p["name"],
                "price": price,
                "quantity": it.quantity,
            }
        )
        total += price * it.quantity

    if not cart:
        return ToolResult(
            ok=False,
            error="no_valid_products",
            llm_view="Produsele cerute nu mai sunt în catalog.",
        )

    sep = "&" if "?" in base else "?"
    url = f"{base}{sep}ref={ctx.turn_id}"
    expires_at = datetime.now(UTC) + timedelta(days=get_settings().checkout_link_ttl_days)
    await create_checkout_link(
        deps.conn,
        ctx.business.id,
        ctx.conversation_id,
        ctx.contact.id,
        ctx.turn_id,  # ref_code = turn_id → idempotent per tur
        cart,
        url,
        expires_at,
    )
    total = round(total, 2)
    ctx.emit("checkout_link_created", items=len(cart), value=total)

    lines = ", ".join(f"{c['name']} ×{c['quantity']} ({c['price']:.2f} lei)" for c in cart)
    llm_view = f"Link de checkout creat: {url}\nCoș: {lines} | total {total:.2f} lei"
    # `products` (cart) → prețurile produselor sunt grounded; `links` → linkul permis;
    # `prices=[total]` → TOTALUL coșului e grounded (altfel validatorul l-ar respinge).
    return ToolResult(
        ok=True,
        products=[by_id[c["product_id"]] for c in cart],
        links=[url],
        prices=[total],
        llm_view=llm_view,
    )
