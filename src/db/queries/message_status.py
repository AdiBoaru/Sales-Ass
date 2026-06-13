"""Statusuri de livrare de la Meta (delivered/read/failed) → DB.

Două scrieri per eveniment:
  1. `message_status_events` — log append-only (tot ce raportează Meta, brut).
  2. `messages.status` — statusul curent al mesajului outbound, pe provider_msg_id.

Update-ul e RANK-GUARDED: statusurile pot sosi out-of-order (Meta nu garantează
ordinea), deci nu „retrogradăm" read → delivered. `failed` câștigă mereu.

`conn` trebuie să fie tenant-scoped (tenant_conn).
"""

import json
from typing import Any

import asyncpg

# Ordinea de avansare a statusului unui mesaj outbound. `failed` = rang maxim
# (un eșec raportat după delivered tot trebuie să se vadă). `received` = inbound.
_RANK_CASE = (
    "case %s when 'received' then 0 when 'queued' then 1 when 'sent' then 2 "
    "when 'delivered' then 3 when 'read' then 4 when 'failed' then 5 else 0 end"
)


async def record_status_event(
    conn: asyncpg.Connection,
    business_id: str,
    provider_msg_id: str,
    status: str,
    *,
    payload: dict[str, Any] | None = None,
) -> bool:
    """Loghează un eveniment de status și avansează `messages.status` dacă e cazul.

    Întoarce True dacă rândul din `messages` a fost actualizat (False dacă statusul
    nu outranking-uia pe cel curent, sau mesajul nu e încă în DB)."""
    await conn.execute(
        """
        insert into message_status_events (business_id, provider_msg_id, status, payload)
        values ($1, $2, $3, coalesce($4::jsonb, '{}'::jsonb))
        """,
        business_id,
        provider_msg_id,
        status,
        json.dumps(payload) if payload is not None else None,
    )

    updated = await conn.fetchval(
        f"""
        update messages
           set status = $3
         where business_id = $1
           and provider_msg_id = $2
           and ({_RANK_CASE % "status"}) < ({_RANK_CASE % "$3"})
        returning 1
        """,
        business_id,
        provider_msg_id,
        status,
    )
    return updated is not None
