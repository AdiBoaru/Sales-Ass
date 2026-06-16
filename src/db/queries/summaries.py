"""Query-uri pe `conversation_summaries` (G6-2 felia 2) — rezumate rolling de conversație.

Append-only checkpoints: fiecare `upto_message_at` e watermark-ul (created_at al celui mai NOU
mesaj inclus în rezumat). Tabelul NU are unique pe (conversation_id, upto_message_at) → insert
simplu; `idx_conv_summaries(conversation_id, upto_message_at DESC)` face „ultimul rezumat" O(1).

Principiul 7: fiecare query cu `business_id = $1`. `conn` deja tenant-scoped.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import asyncpg


async def get_latest_summary(
    conn: asyncpg.Connection, business_id: str, conversation_id: str
) -> dict[str, Any] | None:
    """Ultimul rezumat (după watermark) al conversației: {summary, upto_message_at} sau None.
    Citit de hook-ul de generare (decizie) ȘI de processor (injectare în context)."""
    row = await conn.fetchrow(
        """
        select summary, upto_message_at
        from conversation_summaries
        where business_id = $1 and conversation_id = $2
        order by upto_message_at desc
        limit 1
        """,
        business_id,
        conversation_id,
    )
    return dict(row) if row else None


async def get_summary_for_context(
    conn: asyncpg.Connection, business_id: str, conversation_id: str
) -> str | None:
    """Doar TEXTUL ultimului rezumat (seed pt `ctx.summary` în processor). None → fără rezumat."""
    latest = await get_latest_summary(conn, business_id, conversation_id)
    return latest["summary"] if latest else None


async def insert_conversation_summary(
    conn: asyncpg.Connection,
    business_id: str,
    conversation_id: str,
    upto_message_at: datetime,
    summary: str,
) -> str:
    """Scrie un checkpoint de rezumat (append-only). `upto_message_at` = created_at al celui mai
    nou mesaj INCLUS în rezumat (watermark onest). Întoarce id-ul. Apelat în savepoint din hook."""
    return await conn.fetchval(
        """
        insert into conversation_summaries (business_id, conversation_id, upto_message_at, summary)
        values ($1, $2, $3, $4)
        returning id::text
        """,
        business_id,
        conversation_id,
        upto_message_at,
        summary,
    )
