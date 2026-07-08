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
    în `properties` jsonb. NX-122: `turn_id` la fel — extras în coloană dedicată
    (rămâne și în jsonb) pentru filtrare/replay ieftin al traiectoriei unui tur.
    Event fără `turn_id` (ex. emis în afara unui tur) → coloana primește NULL."""
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
            e.properties.get("turn_id"),
        )
        for e in events
    ]
    await conn.executemany(
        """
        insert into analytics_events
            (business_id, conversation_id, contact_id, event_type, properties,
             tokens_in, tokens_out, cost_usd, turn_id)
        values ($1, $2, $3, $4, $5::jsonb, $6, $7, $8, $9)
        """,
        rows,
    )
    return len(rows)


async def fetch_turn_events(conn: asyncpg.Connection, business_id: str, turn_id: str) -> list[dict]:
    """Citește evenimentele unui tur pentru REPLAY/suport (NX-146) — ordonate cronologic.

    Excepție documentată de la „append-only, niciun SELECT din runtime" (vezi docstring-ul
    modulului): ăsta NU e apel din pipeline, ci un tool de suport/ops. Tenant-scoped (P7:
    `WHERE business_id = $1 AND turn_id = $2` — indexul `(business_id, turn_id)` face
    filtrarea ieftină pe tabelul partiționat). Evenimentele sunt deja redactate la emitere
    (P12); scriptul de replay mai redactează defensiv corpurile de mesaje. `properties` e
    decodat din jsonb în dict."""
    rows = await conn.fetch(
        """
        select event_type, conversation_id, properties, tokens_in, tokens_out,
               cost_usd, created_at
        from analytics_events
        where business_id = $1 and turn_id = $2
        order by created_at, id
        """,
        business_id,
        turn_id,
    )
    out: list[dict] = []
    for r in rows:
        props = r["properties"]
        if isinstance(props, str):
            props = json.loads(props) if props else {}
        out.append(
            {
                "event_type": r["event_type"],
                "conversation_id": r["conversation_id"],
                "properties": props or {},
                "tokens_in": r["tokens_in"],
                "tokens_out": r["tokens_out"],
                "cost_usd": r["cost_usd"],
                "created_at": r["created_at"],
            }
        )
    return out
