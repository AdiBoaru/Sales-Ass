"""Query-uri pe `messages` (partiționat lunar) — insert + istoric (max 8).

În schema reală textul e `body`, rolul e `direction` (inbound|outbound|internal)
+ `author` (contact|bot|human_agent|system) — NU `role`/`content`.

Dedupe la nivel de provider (retry-ul agresiv al Meta) NU se rezolvă aici:
unique-ul de pe `messages` include cheia de partiționare (`created_at`), deci
un retry cu alt `created_at` nu se prinde prin ON CONFLICT. Garanția exact-once
e în stratul de dedupe dedicat (NX-51: Redis + tabel ne-partiționat), upstream
de acest insert. Aici facem inserturi simple.

`conn` trebuie să fie deja tenant-scoped (tenant_conn).
"""

import json
from datetime import datetime
from typing import Any

import asyncpg

from src.models import Author, Direction, Message

# Bugetul de istoric din arhitectură: max 8 mesaje, cel mai recent ultimul.
HISTORY_LIMIT = 8


async def insert_message(
    conn: asyncpg.Connection,
    business_id: str,
    conversation_id: str,
    contact_id: str,
    direction: Direction | str,
    author: Author | str,
    *,
    body: str | None = None,
    content_type: str = "text",
    provider_msg_id: str | None = None,
    payload: dict[str, Any] | None = None,
    media_ref: str | None = None,
    status: str | None = None,
    model_route: str | None = None,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    cost_usd: float | None = None,
    latency_ms: int | None = None,
) -> str:
    """Inserează un mesaj (inbound/outbound/internal) și întoarce id-ul lui.

    Câmpurile de observabilitate (tokens/cost/latency/model_route) sunt opționale
    — runner-ul le completează pentru mesajele bot. Status default-ul DB e
    'received'; pentru outbound trece prin status='queued' (setat de Sender).
    """
    direction = Direction(direction).value if isinstance(direction, Direction) else direction
    author = Author(author).value if isinstance(author, Author) else author

    row = await conn.fetchrow(
        """
        insert into messages (
            business_id, conversation_id, contact_id, direction, author,
            body, content_type, provider_msg_id, payload, media_ref,
            status, model_route, tokens_in, tokens_out, cost_usd, latency_ms
        )
        values (
            $1, $2, $3, $4, $5,
            $6, $7, $8, coalesce($9::jsonb, '{}'::jsonb), $10,
            coalesce($11, 'received'), $12, $13, $14, $15, $16
        )
        returning id::text as id
        """,
        business_id,
        conversation_id,
        contact_id,
        direction,
        author,
        body,
        content_type,
        provider_msg_id,
        json.dumps(payload) if payload is not None else None,
        media_ref,
        status,
        model_route,
        tokens_in,
        tokens_out,
        cost_usd,
        latency_ms,
    )
    return row["id"]


async def get_recent_messages(
    conn: asyncpg.Connection,
    business_id: str,
    conversation_id: str,
    limit: int = HISTORY_LIMIT,
) -> list[Message]:
    """Ultimele `limit` mesaje ale conversației, ordonate cronologic crescător
    (cel mai recent ultimul — exact ce așteaptă context builder-ul / agentul).

    Hard cap la HISTORY_LIMIT (8): chiar dacă cineva cere mai mult, bugetul de
    context e impus în cod (principiul 4)."""
    limit = min(limit, HISTORY_LIMIT)
    rows = await conn.fetch(
        """
        select direction, author, body, content_type, created_at
        from (
            select direction, author, body, content_type, created_at
            from messages
            where business_id = $1 and conversation_id = $2
            order by created_at desc
            limit $3
        ) recent
        order by created_at asc
        """,
        business_id,
        conversation_id,
        limit,
    )
    return [
        Message(
            direction=Direction(r["direction"]),
            author=Author(r["author"]),
            body=r["body"],
            content_type=r["content_type"],
            created_at=r["created_at"],
        )
        for r in rows
    ]


async def get_turn_messages(
    conn: asyncpg.Connection,
    business_id: str,
    conversation_id: str,
    turn_id: str,
) -> list[Message]:
    """Mesajele (inbound + outbound) unui SINGUR tur, pentru Turn Replay (NX-146 felia 2 fix).

    `turn_id` e stampat în `payload` la insert (processor.py) — precis, spre deosebire de
    euristica anterioară „ultimele N mesaje ale conversației" (greșită dacă discuția a continuat
    după turul investigat). Tool de suport/ops (`admin_conn`), nu runtime; `business_id = $1`
    (P7). Mesaje mai vechi (insertate înainte de acest fix, fără turn_id în payload) → gol,
    replay-ul cade pe fallback (fără inbound/reply, restul traiectoriei rămâne intact)."""
    rows = await conn.fetch(
        """
        select direction, author, body, content_type, created_at
        from messages
        where business_id = $1 and conversation_id = $2 and payload->>'turn_id' = $3
        order by created_at asc
        """,
        business_id,
        conversation_id,
        turn_id,
    )
    return [
        Message(
            direction=Direction(r["direction"]),
            author=Author(r["author"]),
            body=r["body"],
            content_type=r["content_type"],
            created_at=r["created_at"],
        )
        for r in rows
    ]


async def count_messages(conn: asyncpg.Connection, business_id: str, conversation_id: str) -> int:
    """Nr. TOTAL de mesaje pe conversație — declanșatorul de prag al summarizer-ului (G6-2).
    `business_id = $1` (P7). Conversațiile au zeci de mesaje, nu milioane → count(*) acceptabil."""
    return await conn.fetchval(
        "select count(*) from messages where business_id = $1 and conversation_id = $2",
        business_id,
        conversation_id,
    )


async def get_messages_for_summary(
    conn: asyncpg.Connection,
    business_id: str,
    conversation_id: str,
    *,
    after: datetime | None,
    tail: int = HISTORY_LIMIT,
) -> list[Message]:
    """Fereastra de SUMARIZAT: mesajele mai VECHI decât ultimele `tail` (care rămân în
    transcriptul live) ȘI mai noi decât `after` (watermark-ul rezumatului anterior; None = de la
    început). Ordine cronologică crescătoare. Asta evită bug-ul „sumarizezi aceleași 8 din
    transcript": rezumatul acoperă fix mesajele care ies din fereastra de 8, fără pierderi.
    `business_id = $1` (P7)."""
    rows = await conn.fetch(
        """
        with ranked as (
            select direction, author, body, content_type, created_at,
                   row_number() over (order by created_at desc) as rn
            from messages
            where business_id = $1 and conversation_id = $2
        )
        select direction, author, body, content_type, created_at
        from ranked
        where rn > $3 and ($4::timestamptz is null or created_at > $4)
        order by created_at asc
        """,
        business_id,
        conversation_id,
        tail,
        after,
    )
    return [
        Message(
            direction=Direction(r["direction"]),
            author=Author(r["author"]),
            body=r["body"],
            content_type=r["content_type"],
            created_at=r["created_at"],
        )
        for r in rows
    ]


async def set_message_provider_id(
    conn: asyncpg.Connection,
    business_id: str,
    message_id: str,
    provider_msg_id: str,
    *,
    status: str = "sent",
) -> None:
    """Leagă wamid-ul Meta de mesajul outbound, după trimitere (dispatcher).

    UPDATE pe (business_id, id) — pe tabela partiționată nu putem prune fără
    created_at, dar volumul per dispatch e mic, acceptabil. Statusul devine 'sent';
    delivered/read vin ulterior din webhook-ul de status pe provider_msg_id."""
    await conn.execute(
        """
        update messages
           set provider_msg_id = $3, status = $4
         where business_id = $1 and id = $2
        """,
        business_id,
        message_id,
        provider_msg_id,
        status,
    )
