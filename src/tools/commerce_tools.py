"""Tool-uri de comerț (F2 + NX-79/80) — WRITE tools ale agentului, cod determinist.

`checkout_link(cart_items)` construiește un link de cumpărare cu `?ref=<ref_code>` și scrie
un rând în `checkout_links` (ancora de atribuire). `cart_add` acumulează coșul în
`conversations.state` (ref-uri, pas intermediar înainte de checkout). `reorder` propune
re-comanda ultimei comenzi a contactului. `subscribe_back_in_stock` (NX-80) abonează la
notificare la restock (citit de proactiv, NX-70). Toate scoped pe `ctx.business.id` (modelul
NU primește business_id) și validate contra catalogului — nu acumulăm/linkuim produse
inexistente. Prețuri/linkuri grounded în `ToolResult` → validatorul (stagiul 8) le acceptă.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from src.config import get_settings
from src.db.queries.catalog import get_products_by_ids
from src.db.queries.commerce import (
    create_checkout_link,
    get_orders_status,
    has_back_in_stock_sub,
    subscribe_back_in_stock,
)
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


# --- cart_add (NX-79): acumulează coșul în state (ref-uri, P8) ----------------


class CartAddArgs(BaseModel):
    product_id: str = Field(min_length=1)
    variant_id: str | None = None
    quantity: int = Field(default=1, ge=1, le=99)


_CART_MAX_LINES = 10  # cap dur (aliniat cu CheckoutArgs.max_length)


@register("cart_add")
async def cart_add_tool(ctx: TurnContext, deps: PipelineDeps, args: dict[str, Any]) -> ToolResult:
    """Adaugă un produs în coșul conversației (persistat în `state.cart` prin `state_patch`).
    Validează produsul contra catalogului (scoped pe business); merge pe (product_id, variant_id)
    → re-apel crește cantitatea, nu duplică linia. Întoarce totalul grounded (validator, P8)."""
    a = CartAddArgs(**args)
    products = await get_products_by_ids(deps.conn, ctx.business.id, [a.product_id], limit=1)
    p = products[0] if products else None
    if p is None or p.get("price") is None:
        return ToolResult(
            ok=False, error="product_not_found", llm_view="Produsul nu mai e în catalog."
        )

    # Coșul curent din state (ref-uri compacte, NU obiectul complet — P8). Copie → nu mutăm state.
    cart: list[dict[str, Any]] = [dict(line) for line in (ctx.state.cart or [])]
    key = (a.product_id, a.variant_id)
    for line in cart:
        if (line["product_id"], line.get("variant_id")) == key:
            line["quantity"] = min(line["quantity"] + a.quantity, 99)
            break
    else:
        cart.append(
            {
                "product_id": a.product_id,
                "variant_id": a.variant_id,
                "name": p["name"],
                "price": round(float(p["price"]), 2),
                "quantity": a.quantity,
            }
        )
    cart = cart[:_CART_MAX_LINES]
    total = round(sum(line["price"] * line["quantity"] for line in cart), 2)
    ctx.emit("cart_updated", lines=len(cart), value=total)

    summary = ", ".join(f"{line['name']} ×{line['quantity']}" for line in cart)
    return ToolResult(
        ok=True,
        products=[p],  # complet → ctx.retrieval + validator de preț
        prices=[total],  # totalul coșului grounded (P8) → validator
        state_patch={"cart": cart},  # ref-uri compacte → persistate de processor
        llm_view=f"Coș actualizat ({len(cart)} produse): {summary} | total {total:.2f} lei",
    )


# --- reorder (NX-79): re-comandă din istoricul contactului -------------------


@register("reorder")
async def reorder_tool(ctx: TurnContext, deps: PipelineDeps, args: dict[str, Any]) -> ToolResult:
    """Propune re-comanda ultimei comenzi a contactului. `contact_id` din `ctx` (NU din args, P7).
    Citire pură (NU scrie coș/link). Numele + prețurile vin din `orders` (date reale ale
    tenantului, ca `check_order`) → `prices` grounded; produsele istorice pot fi inactive, deci
    NU le validăm contra catalogului."""
    orders = await get_orders_status(deps.conn, ctx.business.id, contact_id=ctx.contact.id, limit=3)
    if not orders:
        return ToolResult(
            ok=False, error="no_orders", llm_view="Nu găsesc comenzi anterioare pe contul tău."
        )
    last = orders[0]  # cea mai recentă (order by placed_at desc)
    items = last.get("items") or []
    if not items:
        return ToolResult(ok=False, error="no_items", llm_view="Comanda anterioară n-are produse.")
    prices = [round(float(i["unit_price"]), 2) for i in items if i.get("unit_price") is not None]
    ctx.emit("reorder_suggested", order_id=last["id"], lines=len(items))
    summary = ", ".join(f"{i['name']} ×{i.get('quantity', 1)}" for i in items)
    return ToolResult(
        ok=True,
        prices=prices,
        llm_view=(f"Ultima comandă a clientului: {summary}. Sugerează re-comanda acestor produse."),
    )


# --- subscribe_back_in_stock (NX-80): notificare la restock (WRITE) ----------


class BackInStockArgs(BaseModel):
    product_id: str = Field(min_length=1)
    variant_id: str | None = None


@register("subscribe_back_in_stock")
async def subscribe_back_in_stock_tool(
    ctx: TurnContext, deps: PipelineDeps, args: dict[str, Any]
) -> ToolResult:
    """Abonează clientul la notificare când un produs fără stoc revine. `contact_id` din `ctx`
    (PII-ul nu trece prin model, P12). Idempotent: re-abonare = no-op (re-armează notificarea),
    cu guard pe `variant_id IS NULL` (NULL distinct în UNIQUE → ON CONFLICT nu prinde). NU
    trimite confirmarea de restock — aia e proactivul (NX-70), care citește rândul scris aici."""
    a = BackInStockArgs(**args)
    products = await get_products_by_ids(deps.conn, ctx.business.id, [a.product_id], limit=1)
    if not products:
        return ToolResult(ok=False, error="not_found", llm_view="Produsul nu există în catalog.")
    p = products[0]
    if p.get("availability") == "in_stock":
        return ToolResult(
            ok=True,
            products=[p],
            llm_view=f"{p['name']} este pe stoc acum — nu e nevoie de notificare.",
        )
    # Guard variant NULL: dacă deja abonat, nu mai inserăm (evită duplicatul pe NULL distinct).
    if a.variant_id is None and await has_back_in_stock_sub(
        deps.conn, ctx.business.id, ctx.contact.id, a.product_id, None
    ):
        ctx.emit("back_in_stock_subscribed", product_id=a.product_id, created=False)
        return ToolResult(
            ok=True, products=[p], llm_view=f"Ești deja pe lista de notificare pentru {p['name']}."
        )
    res = await subscribe_back_in_stock(
        deps.conn, ctx.business.id, ctx.contact.id, a.product_id, a.variant_id
    )
    ctx.emit("back_in_stock_subscribed", product_id=a.product_id, created=res["created"])
    return ToolResult(ok=True, products=[p], llm_view=f"Te anunț când {p['name']} revine pe stoc.")
