"""NX-237 — query-urile coșului canonic: `conversation_carts` + items + receipts.

Principiul 7: FIECARE query cu `business_id = $1` (mecanismul primar; RLS = plasa). Serializarea
mutațiilor e lockul de rând pe coș (`lock_active_cart` → FOR UPDATE), luat de `cart_service`
într-o tranzacție SCURTĂ (`db_tx`) — niciun await extern înăuntru (contract NX-231).

Hidratarea faptelor comerciale (`load_cart_facts_rows`) refolosește DELIBERAT expresiile de preț
din `catalog.py` (`_EFFECTIVE_PRICE` / `_SALE_ACTIVE` / `_VARIANTS_AGG`): prețul pe care îl vede
coșul TREBUIE să fie exact prețul pe care îl vede validatorul și clientul pe orice altă cale —
două formule de preț ar fi două adevăruri (aceeași decizie ca `load_context_entities`, NX-234).
"""

from __future__ import annotations

import json
from typing import Any

import asyncpg

# Import de sibling (același pachet): sursa UNICĂ a semanticii de preț/variante. Private prin
# convenție, dar partajate intenționat — o copie locală ar diverge tăcut la primul fix de preț.
from src.db.queries.catalog import (  # noqa: PLC2701
    _EFFECTIVE_PRICE,
    _SALE_ACTIVE,
    _SALE_WINDOW_OK,
    _VARIANTS_AGG,
    _row_to_product,
)

# ── Hidratarea faptelor comerciale (UN query per batch, indiferent de linii) ────────────────

_CART_FACTS_SELECT = f"""
    select
        p.id::text                  as id,
        p.name                      as name,
        b.name                      as brand,
        {_EFFECTIVE_PRICE}::float8  as price,
        (case when {_SALE_ACTIVE} then p.price end)::float8
                                    as list_price,
        p.currency                  as currency,
        p.availability              as availability,
        p.stock_total               as stock,
        p.rating::float8            as rating,
        p.review_count              as review_count,
        prs.summary                 as review_summary,
        p.attributes                as attributes,
        p.product_url               as url,
        p.delivery_class            as delivery_class,
        p.restock_date              as restock_date,
        p.synced_at                 as synced_at,
        p.updated_at                as updated_at,
        ing.names                   as ingredients_db,
        vr.variants                 as variants
    from products p
    left join brands b on b.id = p.brand_id
    left join product_review_summaries prs on prs.product_id = p.id
    left join lateral (
        select min(case when {_SALE_WINDOW_OK} and v.sale_price is not null
                         and v.sale_price < v.price then v.sale_price else v.price end) as price
        from product_variants v
        where v.product_id = p.id and v.business_id = p.business_id
    ) vp on true
    left join lateral (
        select array_agg(i.name order by pin.position) as names
        from product_ingredients pin
        join ingredients i on i.id = pin.ingredient_id
        where pin.product_id = p.id and pin.is_key
    ) ing on true
{_VARIANTS_AGG}
"""


async def load_cart_facts_rows(
    conn: asyncpg.Connection, business_id: str, product_ids: list[str], *, limit: int = 12
) -> list[dict[str, Any]]:
    """Rândurile de catalog pentru TOATE liniile coșului + ținta comenzii, într-UN round-trip.

    `status='active'` e filtrul onest: un produs arhivat între add și citire NU se întoarce →
    linia lui devine UNKNOWN în snapshot (vizibilă, dar fără preț susținut), nu „ultimul preț
    cunoscut" servit drept fapt. `business_id = $1` (P7)."""
    if not product_ids:
        return []
    rows = await conn.fetch(
        _CART_FACTS_SELECT
        + " where p.business_id = $1 and p.status = 'active' and p.id = any($2::uuid[])"
        + " limit $3",
        business_id,
        product_ids[:limit],
        limit,
    )
    return [_row_to_product(r) for r in rows]


# ── Coșul (rândul-părinte) ──────────────────────────────────────────────────────────────────


async def create_cart_if_absent(
    conn: asyncpg.Connection, business_id: str, conversation_id: str
) -> None:
    """Creează coșul ACTIV al conversației dacă nu există (idempotent pe partial unique).
    Rândul nou are `version=0`; prima mutație reușită îl duce la 1."""
    await conn.execute(
        """
        insert into conversation_carts (business_id, conversation_id)
        values ($1, $2)
        on conflict (business_id, conversation_id) where status = 'active' do nothing
        """,
        business_id,
        conversation_id,
    )


async def lock_active_cart(
    conn: asyncpg.Connection, business_id: str, conversation_id: str
) -> dict[str, Any] | None:
    """Coșul activ, cu LOCK DE RÂND (FOR UPDATE) — frâna de concurență a mutațiilor: două
    comenzi simultane pe același coș se serializează aici, nu se împletesc."""
    row = await conn.fetchrow(
        """
        select id::text as id, version, status, currency
        from conversation_carts
        where business_id = $1 and conversation_id = $2 and status = 'active'
        for update
        """,
        business_id,
        conversation_id,
    )
    return dict(row) if row else None


async def get_active_cart(
    conn: asyncpg.Connection, business_id: str, conversation_id: str
) -> dict[str, Any] | None:
    """Citire FĂRĂ lock (snapshot read-only)."""
    row = await conn.fetchrow(
        """
        select id::text as id, version, status, currency
        from conversation_carts
        where business_id = $1 and conversation_id = $2 and status = 'active'
        """,
        business_id,
        conversation_id,
    )
    return dict(row) if row else None


async def bump_cart_version(conn: asyncpg.Connection, business_id: str, cart_id: str) -> int:
    """Versiune nouă, MONOTONĂ — se apelează o singură dată per mutație reușită."""
    return await conn.fetchval(
        """
        update conversation_carts
        set version = version + 1, updated_at = now()
        where business_id = $1 and id = $2
        returning version
        """,
        business_id,
        cart_id,
    )


async def set_cart_status(
    conn: asyncpg.Connection, business_id: str, cart_id: str, status: str
) -> None:
    await conn.execute(
        """
        update conversation_carts
        set status = $3, updated_at = now()
        where business_id = $1 and id = $2
        """,
        business_id,
        cart_id,
        status,
    )


# ── Liniile coșului ─────────────────────────────────────────────────────────────────────────


async def get_cart_items(
    conn: asyncpg.Connection, business_id: str, cart_id: str
) -> list[dict[str, Any]]:
    """Liniile, în ordinea adăugării — refs + cantități, NU cache de name/price (P8)."""
    rows = await conn.fetch(
        """
        select id::text as id, product_id::text as product_id,
               variant_id::text as variant_id, quantity
        from conversation_cart_items
        where business_id = $1 and cart_id = $2
        order by created_at, id
        """,
        business_id,
        cart_id,
    )
    return [dict(r) for r in rows]


async def insert_cart_item(
    conn: asyncpg.Connection,
    business_id: str,
    cart_id: str,
    product_id: str,
    variant_id: str | None,
    quantity: int,
    turn_id: str,
) -> None:
    """INSERT simplu — apelantul deține lockul de coș, deci linia nu poate apărea concurent;
    UNIQUE NULLS NOT DISTINCT rămâne plasa structurală."""
    await conn.execute(
        """
        insert into conversation_cart_items
            (business_id, cart_id, product_id, variant_id, quantity,
             added_turn_id, updated_turn_id)
        values ($1, $2, $3::uuid, $4::uuid, $5, $6, $6)
        """,
        business_id,
        cart_id,
        product_id,
        variant_id,
        quantity,
        turn_id,
    )


async def set_cart_item_quantity(
    conn: asyncpg.Connection,
    business_id: str,
    cart_id: str,
    product_id: str,
    variant_id: str | None,
    quantity: int,
    turn_id: str,
) -> bool:
    """True dacă linia exista și a fost actualizată. `is not distinct from` — NULL = NULL."""
    row = await conn.fetchrow(
        """
        update conversation_cart_items
        set quantity = $5, updated_turn_id = $6, updated_at = now()
        where business_id = $1 and cart_id = $2 and product_id = $3::uuid
          and variant_id is not distinct from $4::uuid
        returning id
        """,
        business_id,
        cart_id,
        product_id,
        variant_id,
        quantity,
        turn_id,
    )
    return row is not None


async def delete_cart_item(
    conn: asyncpg.Connection,
    business_id: str,
    cart_id: str,
    product_id: str,
    variant_id: str | None,
) -> bool:
    row = await conn.fetchrow(
        """
        delete from conversation_cart_items
        where business_id = $1 and cart_id = $2 and product_id = $3::uuid
          and variant_id is not distinct from $4::uuid
        returning id
        """,
        business_id,
        cart_id,
        product_id,
        variant_id,
    )
    return row is not None


async def clear_cart_items(conn: asyncpg.Connection, business_id: str, cart_id: str) -> int:
    result = await conn.execute(
        "delete from conversation_cart_items where business_id = $1 and cart_id = $2",
        business_id,
        cart_id,
    )
    try:
        return int(result.split()[-1])
    except (ValueError, IndexError, AttributeError):
        return 0


# ── Receipts (dovada idempotentă a mutației) ────────────────────────────────────────────────

_RECEIPT_COLS = (
    "id::text as id, operation, status, idempotency_key, before_version, after_version,"
    " result_code, external_ref, url, action_id, turn_id"
)


async def get_receipt_by_key(
    conn: asyncpg.Connection, business_id: str, idempotency_key: str
) -> dict[str, Any] | None:
    """Receiptul unei chei — verificat SUB lockul de coș, ca doi retry simultani să nu treacă
    amândoi de verificare înainte ca vreunul să scrie."""
    row = await conn.fetchrow(
        f"select {_RECEIPT_COLS} from commerce_action_receipts"
        " where business_id = $1 and idempotency_key = $2",
        business_id,
        idempotency_key,
    )
    return dict(row) if row else None


async def insert_receipt(
    conn: asyncpg.Connection,
    business_id: str,
    *,
    conversation_id: str,
    cart_id: str | None,
    operation: str,
    idempotency_key: str,
    status: str,
    before_version: int,
    after_version: int | None,
    result_code: str | None,
    turn_id: str | None,
    action_id: str | None,
    external_ref: str | None = None,
    url: str | None = None,
) -> dict[str, Any] | None:
    """INSERT idempotent: cheia deja consumată → None (apelantul face replay pe rândul existent).
    Doar refs și coduri — ZERO PII, zero text liber (P12)."""
    row = await conn.fetchrow(
        f"""
        insert into commerce_action_receipts
            (business_id, conversation_id, cart_id, operation, idempotency_key, status,
             before_version, after_version, result_code, turn_id, action_id, external_ref, url)
        values ($1, $2, $3::uuid, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
        on conflict (business_id, idempotency_key) do nothing
        returning {_RECEIPT_COLS}
        """,
        business_id,
        conversation_id,
        cart_id,
        operation,
        idempotency_key,
        status,
        before_version,
        after_version,
        result_code,
        turn_id,
        action_id,
        external_ref,
        url,
    )
    return dict(row) if row else None


async def finalize_receipt(
    conn: asyncpg.Connection,
    business_id: str,
    receipt_id: str,
    *,
    status: str,
    result_code: str | None = None,
    after_version: int | None = None,
    external_ref: str | None = None,
    url: str | None = None,
) -> None:
    """Finalizează un receipt `pending` (calea cu adaptor extern). Nu re-deschide un terminal:
    predicatul pe status face UPDATE-ul unui rând deja finalizat un no-op vizibil (0 rânduri)."""
    await conn.execute(
        """
        update commerce_action_receipts
        set status = $3, result_code = coalesce($4, result_code),
            after_version = coalesce($5, after_version),
            external_ref = coalesce($6, external_ref), url = coalesce($7, url),
            updated_at = now()
        where business_id = $1 and id = $2
          and status in ('pending', 'unknown_reconcile')
        """,
        business_id,
        receipt_id,
        status,
        result_code,
        after_version,
        external_ref,
        url,
    )


async def pending_receipts(
    conn: asyncpg.Connection, business_id: str, *, older_than_s: int = 300, limit: int = 50
) -> list[dict[str, Any]]:
    """Receipts nefinalizate mai vechi de prag — inputul reconcilierii + al alarmelor
    (`cart_receipt_reconcile`). Bounded (limit), ordonate de la cel mai vechi."""
    rows = await conn.fetch(
        f"""
        select {_RECEIPT_COLS} from commerce_action_receipts
        where business_id = $1 and status in ('pending', 'unknown_reconcile')
          and created_at < now() - make_interval(secs => $2)
        order by created_at
        limit $3
        """,
        business_id,
        older_than_s,
        limit,
    )
    return [dict(r) for r in rows]


def decode_jsonb(value: Any) -> Any:
    """jsonb fără codec vine ca text — utilitar pentru cititorii de receipts/state."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return None
    return value
