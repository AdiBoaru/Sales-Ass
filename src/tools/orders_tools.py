"""Tool de comenzi (G7-3, Faza 3) — `check_order`: status + tracking, read-only.

Răspunde la „unde e comanda mea?" / „ce status are ORD-123?" din `orders` + `shipments`.
Izolare DURĂ: lookup-ul e scoped pe `ctx.contact.id` (din channel_identities, NU din args) —
un client nu poate vedea comanda altcuiva nici ghicind un `external_id` (răspuns `not_found`
identic cu inexistent, fără a divulga existența). `business_id` din `ctx` (P7). Fără PII în
`llm_view` (status/AWB/ETA/total — `orders` n-are telefon/adresă, P12).

Totalurile comenzii se întorc în `ToolResult.prices` (grounded din DB) → validatorul de preț le
acceptă fără să fie slăbit (oglindește `links` de la checkout_link).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from src.db.queries.commerce import get_orders_status
from src.tools.base import ToolResult, register

if TYPE_CHECKING:
    from src.models import TurnContext
    from src.worker.runner import PipelineDeps


class CheckOrderArgs(BaseModel):
    # Numărul/identificatorul comenzii dat de client. Opțional: lipsă → ultimele comenzi ale lui.
    order_ref: str | None = Field(default=None, min_length=1, max_length=64)


def _order_totals(orders: list[dict[str, Any]]) -> list[float]:
    """Sumele grounded de oferit validatorului: totalul fiecărei comenzi (+ unit_price-urile)."""
    out: list[float] = []
    for o in orders:
        if o.get("total") is not None:
            out.append(round(float(o["total"]), 2))
        for it in o.get("items") or []:
            if it.get("unit_price") is not None:
                out.append(round(float(it["unit_price"]), 2))
    return out


def _orders_view(orders: list[dict[str, Any]]) -> str:
    """Vedere compactă pt model (fără PII): nr comandă, status, total, AWB/curier/ETA dacă-s."""
    lines: list[str] = []
    for o in orders:
        parts = [f"Comanda {o['external_id']}", f"status: {o['status']}"]
        if o.get("total") is not None:
            parts.append(f"total: {float(o['total']):.2f} {o.get('currency') or 'RON'}")
        if o.get("awb"):
            carrier = o.get("carrier") or "curier"
            parts.append(f"AWB {o['awb']} ({carrier}, {o.get('shipment_status') or '-'})")
        if o.get("eta"):
            parts.append(f"ETA: {o['eta']}")
        lines.append(" | ".join(parts))
    return "\n".join(lines)


@register("check_order")
async def check_order_tool(
    ctx: TurnContext, deps: PipelineDeps, args: dict[str, Any]
) -> ToolResult:
    """Status + tracking pentru comanda cerută (după nr) sau ultimele comenzi ale contactului."""
    a = CheckOrderArgs(**args)
    # Izolare: ÎNTOTDEAUNA scoped pe contactul curent (în SQL). `order_ref` doar îngustează.
    orders = await get_orders_status(
        deps.conn,
        ctx.business.id,
        external_id=a.order_ref,
        contact_id=ctx.contact.id,
        limit=1 if a.order_ref else 3,
    )
    if not orders:
        return ToolResult(
            ok=False, error="not_found", llm_view="Nu am găsit nicio comandă pe acest cont."
        )
    return ToolResult(
        ok=True,
        products=[],  # nu-s produse de catalog → nu poluează validatorul de preț
        prices=_order_totals(orders),
        llm_view=_orders_view(orders),
    )
