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
