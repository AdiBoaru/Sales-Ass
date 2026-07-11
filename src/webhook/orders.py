"""Webhook comenzi → atribuire (F2-2). Închide bucla de bani.

Două părți, separate de marginea de proces:
  • parser NEUTRU (`OrderIn`) — forma comună de comandă (adaptorul de platformă Shopify/Woo →
    neutru e out of scope; v1 ingestă forma asta).
  • `process_order` — engine-ul de atribuire, rulat în WORKER (DB writes), NU în webhook.
    Endpointul (app.py) doar pune un envelope pe stream (margine subțire, fără DB).

Atribuirea: comanda poartă `?ref=<ref_code>` din linkul botului (F2-1) → match pe `checkout_links`
→ `orders.attribution='assisted'` + `attributed_checkout_link_id` + `converted_order_id`. Fără ref
sau ref necunoscut → `attribution='none'`. Idempotent pe `(business_id, external_id)`.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from src.db.queries.analytics import insert_events
from src.db.queries.commerce import (
    get_checkout_link_by_ref,
    insert_order_items,
    mark_checkout_converted,
    upsert_order,
)
from src.models import Event

if TYPE_CHECKING:
    import asyncpg

log = logging.getLogger(__name__)


class OrderItemIn(BaseModel):
    """O linie de comandă (neutru de platformă). `product_id`/`variant_id` = uuid catalog dacă-s."""

    product_id: str | None = None
    variant_id: str | None = None
    name: str = Field(min_length=1)
    sku: str | None = None
    quantity: int = Field(default=1, ge=1)
    unit_price: float = Field(ge=0)


class OrderIn(BaseModel):
    """Comandă neutră de la platforma magazinului. `ref` = `?ref=` din linkul botului (F2-1)."""

    external_id: str = Field(min_length=1)
    status: str = Field(min_length=1)
    total: float = Field(ge=0)
    currency: str = "RON"
    ref: str | None = None
    placed_at: datetime
    items: list[OrderItemIn] = Field(default_factory=list)
    # NX-130: id-ul STABIL de client din eshop (opac, sau hash de email — NU PII, P12). Login
    # passthrough-ul web (NX-129) caută comenzile pe el. Trebuie să fie ACEEAȘI cheie pe care
    # backend-ul gazdei o pune în `sub`-ul JWT-ului. Absent → comandă fără identitate de client.
    customer_ref: str | None = Field(default=None, max_length=128)


async def process_order(
    conn: asyncpg.Connection, business_id: str, order: dict[str, Any]
) -> dict[str, Any]:
    """Inserează comanda + rezolvă atribuirea. `conn` deja tenant-scoped.

    Întoarce `{order_id, attribution, attributed}`. Ridică `ValidationError` la payload invalid
    (caller-ul = consumer prinde și loghează — un order rău nu blochează coada)."""
    o = OrderIn(**order)

    attribution = "none"
    checkout_link_id: str | None = None
    contact_id: str | None = None
    conversation_id: str | None = None
    if o.ref:
        link = await get_checkout_link_by_ref(conn, business_id, o.ref)
        if link is not None:
            attribution = "assisted"
            checkout_link_id = link["id"]
            contact_id = link["contact_id"]
            conversation_id = link["conversation_id"]

    row = await upsert_order(
        conn,
        business_id,
        external_id=o.external_id,
        status=o.status,
        total=o.total,
        currency=o.currency,
        placed_at=o.placed_at,
        contact_id=contact_id,
        attribution=attribution,
        attributed_checkout_link_id=checkout_link_id,
        payload=order,
        external_customer_ref=o.customer_ref,
    )
    order_id = row["id"]
    inserted = bool(row["inserted"])

    # items DOAR la insert nou → re-livrarea aceleiași comenzi nu le dublează.
    if inserted and o.items:
        await insert_order_items(conn, order_id, [it.model_dump() for it in o.items])

    if checkout_link_id is not None:
        await mark_checkout_converted(conn, business_id, checkout_link_id, order_id)

    # Analytics (append-only, fără PII): order_received mereu; order_attributed la match.
    events = [Event("order_received", {"total": o.total, "has_ref": o.ref is not None})]
    if attribution != "none":
        events.append(Event("order_attributed", {"attribution": attribution, "total": o.total}))
    # NX-162 (Funnel Truth): pasul de CONVERSIE al funnel-ului checkout (created→clicked→converted).
    # Gated pe `inserted` → redelivery-ul aceleiași comenzi NU dublează evenimentul (mark_checkout
    # e deja idempotent). Fără PII: ref_code (uuid), order_id (uuid), attribution (enum).
    if checkout_link_id is not None and inserted:
        events.append(
            Event(
                "checkout_link_converted",
                {"ref_code": o.ref, "order_id": order_id, "attribution": attribution},
            )
        )
    try:
        await insert_events(
            conn, business_id, events, conversation_id=conversation_id, contact_id=contact_id
        )
    except Exception:  # noqa: BLE001 — analytics best-effort, atribuirea e deja persistată
        log.warning("orders: persistarea analytics a eșuat (atribuirea rămâne)")

    log.info(
        "order procesat: business=%s order=%s attribution=%s inserted=%s",
        business_id,
        order_id,
        attribution,
        inserted,
    )
    return {"order_id": order_id, "attribution": attribution, "attributed": attribution != "none"}
