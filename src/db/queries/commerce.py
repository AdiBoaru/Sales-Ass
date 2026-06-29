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
    external_customer_ref: str | None = None,
) -> dict[str, Any]:
    """Inserează (idempotent pe `business_id+external_id`) o comandă. La re-livrare
    actualizează DOAR status/total/updated_at — atribuirea + contactul NU se downgradează.
    Întoarce `{id, inserted}` (`inserted` = a fost insert nou, via `xmax = 0`).

    `external_customer_ref` (NX-130): id-ul OPAC de client din eshop (sau hash de email — NU PII,
    P12), pe care login passthrough-ul web (NX-129) îl folosește la lookup. No-downgrade la conflict
    (`coalesce(existent, nou)`): o re-livrare poate BACKFILL-a un NULL, dar nu șterge o valoare."""
    row = await conn.fetchrow(
        """
        insert into orders
            (business_id, contact_id, external_id, status, total, currency,
             attributed_checkout_link_id, attribution, payload, placed_at, external_customer_ref)
        values ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10, $11)
        on conflict (business_id, external_id) do update
            set status = excluded.status, total = excluded.total, updated_at = now(),
                external_customer_ref = coalesce(
                    orders.external_customer_ref, excluded.external_customer_ref
                )
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
        external_customer_ref,
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


# --- back-in-stock (NX-80, tool subscribe_back_in_stock) ---------------------


async def has_back_in_stock_sub(
    conn: asyncpg.Connection,
    business_id: str,
    contact_id: str,
    product_id: str,
    variant_id: str | None,
) -> bool:
    """True dacă există deja un abonament pe (business, contact, product, variant). Guard pentru
    cazul `variant_id IS NULL`: în Postgres NULL e DISTINCT în UNIQUE → `ON CONFLICT` NU prinde,
    deci verificăm întâi cu `is not distinct from` (NULL = NULL). `business_id = $1` (izolare)."""
    row = await conn.fetchrow(
        """
        select 1 from back_in_stock_subscriptions
        where business_id = $1 and contact_id = $2 and product_id = $3
          and variant_id is not distinct from $4::uuid
        limit 1
        """,
        business_id,
        contact_id,
        product_id,
        variant_id,
    )
    return row is not None


async def subscribe_back_in_stock(
    conn: asyncpg.Connection,
    business_id: str,
    contact_id: str,
    product_id: str,
    variant_id: str | None = None,
) -> dict[str, Any]:
    """INSERT idempotent în `back_in_stock_subscriptions`. Re-subscribe pe aceeași
    (product, variant) = `ON CONFLICT` re-armează notificarea (`notified_at` → NULL: vrea iar
    notificare la următorul restock). `business_id = $1` (P7). Întoarce `{id, created}`
    (`created` = insert nou, via `xmax = 0`). NB: pe `variant_id IS NULL` ON CONFLICT nu prinde
    (NULL distinct) → caller-ul cheamă `has_back_in_stock_sub` întâi (NX-80)."""
    row = await conn.fetchrow(
        """
        insert into back_in_stock_subscriptions
            (business_id, contact_id, product_id, variant_id)
        values ($1, $2, $3, $4::uuid)
        on conflict (business_id, contact_id, product_id, variant_id)
            do update set notified_at = null
        returning id::text as id, (xmax = 0) as created
        """,
        business_id,
        contact_id,
        product_id,
        variant_id,
    )
    return dict(row)


# --- citire status comandă (G7-3, tool check_order) --------------------------


async def get_orders_status(
    conn: asyncpg.Connection,
    business_id: str,
    *,
    external_id: str | None = None,
    contact_id: str | None = None,
    external_customer_ref: str | None = None,
    limit: int = 3,
) -> list[dict[str, Any]]:
    """Comenzile (status + tracking) pentru tool-ul `check_order`. UN singur SELECT cu lateral
    `order_items` (jsonb_agg, fără N+1) + ultimul `shipment` (după updated_at). Filtrare DURĂ pe
    `business_id` ȘI (opțional) `external_id`/`contact_id`/`external_customer_ref` (NX-130: login
    passthrough caută pe customer_ref, nu pe contactul throwaway) — izolarea se face ÎN SQL
    (defense-in-depth), nu doar în cod. `order_items` n-are `business_id` → izolare tranzitivă prin
    rândul părinte `orders` (deja tenant-scoped). Ordine: cele mai recente comenzi întâi."""
    rows = await conn.fetch(
        """
        select o.id::text as id, o.contact_id::text as contact_id, o.external_id,
               o.status, o.total::float8 as total, o.currency, o.placed_at,
               s.carrier, s.awb, s.status as shipment_status, s.eta,
               coalesce(items.items, '[]'::jsonb) as items
        from orders o
        left join lateral (
            select jsonb_agg(jsonb_build_object(
                'name', oi.name, 'quantity', oi.quantity, 'unit_price', oi.unit_price
            )) as items
            from order_items oi where oi.order_id = o.id
        ) items on true
        left join lateral (
            select carrier, awb, status, eta
            from shipments sh
            where sh.business_id = $1 and sh.order_id = o.id
            order by sh.updated_at desc
            limit 1
        ) s on true
        where o.business_id = $1
          and ($2::text is null or o.external_id = $2)
          and ($3::uuid is null or o.contact_id = $3)
          and ($4::text is null or o.external_customer_ref = $4)
        order by o.placed_at desc
        limit $5
        """,
        business_id,
        external_id,
        contact_id,
        external_customer_ref,
        min(limit, 6),
    )
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        if isinstance(d.get("items"), str):  # jsonb vine ca text fără codec
            d["items"] = json.loads(d["items"])
        out.append(d)
    return out
