"""Query-uri pe comerț (bucla de bani) — `checkout_links`.

Principiul 7: fiecare query cu `business_id = $1` (mecanism primar; RLS = plasa).
`checkout_links` e capătul din care PLEACĂ atribuirea: botul scrie un `ref_code` în URL,
iar webhookul de comenzi (F2-2) face match pe el → `orders.attribution`.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import asyncpg


async def create_checkout_link(
    conn: asyncpg.Connection,
    business_id: str,
    conversation_id: str,
    contact_id: str,
    ref_code: str,
    cart: list[dict[str, Any]],
    url: str,
    expires_at: datetime,
) -> dict[str, Any]:
    """Creează (idempotent) un link de checkout cu `ref_code` unic per business.

    `ref_code = turn_id` → re-run pe același tur cade pe `ON CONFLICT` (update cart/url/
    expires, NU duplică). Întoarce `{id, ref_code, url}`. `conn` deja tenant-scoped."""
    row = await conn.fetchrow(
        """
        insert into checkout_links
            (business_id, conversation_id, contact_id, ref_code, cart, url, expires_at)
        values ($1, $2, $3, $4, $5::jsonb, $6, $7)
        on conflict (business_id, ref_code) do update
            set cart = excluded.cart, url = excluded.url, expires_at = excluded.expires_at
        returning id::text as id, ref_code, url
        """,
        business_id,
        conversation_id,
        contact_id,
        ref_code,
        json.dumps(cart),
        url,
        expires_at,
    )
    return dict(row)


# --- atribuire comenzi (F2-2) ------------------------------------------------


async def get_checkout_link_by_ref(
    conn: asyncpg.Connection, business_id: str, ref_code: str
) -> dict[str, Any] | None:
    """Linkul de checkout după `ref_code` (ancora de atribuire). None dacă nu există.
    `business_id = $1` (izolare; match-ul de atribuire e per tenant)."""
    row = await conn.fetchrow(
        """
        select id::text as id, contact_id::text as contact_id,
               conversation_id::text as conversation_id,
               converted_order_id::text as converted_order_id
        from checkout_links
        where business_id = $1 and ref_code = $2
        """,
        business_id,
        ref_code,
    )
    return dict(row) if row else None


async def upsert_order(
    conn: asyncpg.Connection,
    business_id: str,
    *,
    external_id: str,
    status: str,
    total: float,
    currency: str,
    placed_at: datetime,
    contact_id: str | None,
    attribution: str,
    attributed_checkout_link_id: str | None,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Inserează (idempotent pe `business_id+external_id`) o comandă. La re-livrare
    actualizează DOAR status/total/updated_at — atribuirea + contactul NU se downgradează.
    Întoarce `{id, inserted}` (`inserted` = a fost insert nou, via `xmax = 0`)."""
    row = await conn.fetchrow(
        """
        insert into orders
            (business_id, contact_id, external_id, status, total, currency,
             attributed_checkout_link_id, attribution, payload, placed_at)
        values ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10)
        on conflict (business_id, external_id) do update
            set status = excluded.status, total = excluded.total, updated_at = now()
        returning id::text as id, (xmax = 0) as inserted
        """,
        business_id,
        contact_id,
        external_id,
        status,
        total,
        currency,
        attributed_checkout_link_id,
        attribution,
        json.dumps(payload),
        placed_at,
    )
    return dict(row)


async def insert_order_items(
    conn: asyncpg.Connection, order_id: str, items: list[dict[str, Any]]
) -> int:
    """Inserează liniile comenzii. `product_id`/`variant_id` = uuid din catalog dacă vin,
    altfel null (maparea SKU extern → produs e refinement). Întoarce câte au fost inserate."""
    if not items:
        return 0
    rows = [
        (
            order_id,
            it.get("product_id"),
            it.get("variant_id"),
            it["name"],
            it.get("sku"),
            it.get("quantity", 1),
            it["unit_price"],
        )
        for it in items
    ]
    await conn.executemany(
        """
        insert into order_items
            (order_id, product_id, variant_id, name, sku, quantity, unit_price)
        values ($1, $2::uuid, $3::uuid, $4, $5, $6, $7)
        """,
        rows,
    )
    return len(rows)


async def mark_checkout_converted(
    conn: asyncpg.Connection, business_id: str, checkout_link_id: str, order_id: str
) -> None:
    """Marchează linkul drept convertit (set `converted_order_id` DOAR dacă e null —
    idempotent: prima comandă atribuită câștigă). `business_id = $1` (izolare)."""
    await conn.execute(
        """
        update checkout_links
        set converted_order_id = $3
        where business_id = $1 and id = $2 and converted_order_id is null
        """,
        business_id,
        checkout_link_id,
        order_id,
    )
