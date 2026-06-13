"""Persistarea evenimentelor de observabilitate în `analytics_events`.

Append-only: botul are DOAR INSERT pe acest tabel (003) — niciun UPDATE/DELETE,
nici SELECT din runtime. Runner-ul acumulează evenimente în `ctx.events` fără să
știe că sunt măsurate (principiul 10); processor-ul le scrie aici la finalul
turului, best-effort (observabilitatea nu blochează răspunsul către client).

`conn` trebuie să fie tenant-scoped (tenant_conn).
"""

import json
from collections.abc import Sequence

import asyncpg

from src.models import Event


async def insert_events(
    conn: asyncpg.Connection,
    business_id: str,
    events: Sequence[Event],
    *,
    conversation_id: str | None = None,
    contact_id: str | None = None,
) -> int:
    """Scrie un lot de evenimente. Întoarce câte au fost inserate.

    `tokens_in/out` și `cost_usd` se extrag din `properties` dacă există (le pun
    acolo stagiile LLM) → coloane dedicate pentru agregare ieftină; restul rămâne
    în `properties` jsonb."""
    if not events:
        return 0

    rows = [
        (
            business_id,
            conversation_id,
            contact_id,
            e.type,
            json.dumps(e.properties),
            e.properties.get("tokens_in"),
            e.properties.get("tokens_out"),
            e.properties.get("cost_usd"),
        )
        for e in events
    ]
    await conn.executemany(
        """
        insert into analytics_events
            (business_id, conversation_id, contact_id, event_type, properties,
             tokens_in, tokens_out, cost_usd)
        values ($1, $2, $3, $4, $5::jsonb, $6, $7, $8)
        """,
        rows,
    )
    return len(rows)
