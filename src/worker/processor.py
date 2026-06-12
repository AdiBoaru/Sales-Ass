"""Procesarea unui mesaj inbound — de la eveniment la răspuns în outbox.

`handle_turn` e miezul determinist al unui tur, pe o conexiune DEJA tenant-scoped:
  1. rezolvă contactul (identity resolution) + conversația
  2. scrie mesajul inbound + alimentează fereastra 24h (last_inbound_at)
  3. construiește TurnContext + rulează pipeline-ul (runner)
  4. dacă a ieșit un reply → îl scrie TRANZACȚIONAL: mesaj outbound (queued) +
     rând în outbox (idempotent pe turn_id) + patch state (touch_outbound) —
     exact contractul Sender-ului (stagiul 9): un singur punct de ieșire, atomic.

Dispatcher-ul (separat) citește outbox, trimite la Meta și leagă provider_msg_id.
"""

from dataclasses import dataclass
from uuid import uuid4

import asyncpg
from redis.asyncio import Redis

from src.db.queries.contacts import get_or_create_contact
from src.db.queries.conversations import (
    get_or_create_conversation,
    patch_conversation_state,
    touch_last_inbound,
)
from src.db.queries.messages import get_recent_messages, insert_message
from src.db.queries.outbox import enqueue_outbox
from src.models import (
    Author,
    BusinessConfig,
    Direction,
    InboundMessage,
    TurnContext,
)
from src.worker.runner import DEFAULT_STAGES, PipelineDeps, Stage, run_pipeline

_CHANNEL_KIND = "whatsapp"


@dataclass
class TurnResult:
    """Rezultatul procesării unui tur (pentru logging/teste)."""

    conversation_id: str
    contact_id: str
    turn_id: str
    reply_text: str | None
    outbox_id: str | None


async def handle_turn(
    conn: asyncpg.Connection,
    business: BusinessConfig,
    channel_id: str,
    event: dict,
    *,
    redis: Redis | None = None,
    stages: list[Stage] | None = None,
) -> TurnResult:
    """Procesează un mesaj inbound pe o conexiune tenant-scoped pe `business.id`.

    `event` = dict-ul produs de webhook (InboundEvent.to_dict): wa_id,
    provider_msg_id, content_type, body, profile_name, ...
    """
    stages = stages or DEFAULT_STAGES
    turn_id = str(uuid4())
    wa_id = event["wa_id"]

    contact = await get_or_create_contact(
        conn,
        business.id,
        _CHANNEL_KIND,
        wa_id,
        display_name=event.get("profile_name"),
    )
    conv = await get_or_create_conversation(
        conn,
        business.id,
        contact.id,
        channel_id,
        locale=business.default_locale,
    )

    await insert_message(
        conn,
        business.id,
        conv["id"],
        contact.id,
        Direction.INBOUND,
        Author.CONTACT,
        body=event.get("body"),
        content_type=event.get("content_type", "text"),
        provider_msg_id=event.get("provider_msg_id"),
        media_ref=event.get("media_id"),
    )
    await touch_last_inbound(conn, business.id, conv["id"])

    ctx = TurnContext(
        turn_id=turn_id,
        business=business,
        contact=contact,
        message=InboundMessage(
            provider_msg_id=event.get("provider_msg_id", ""),
            content_type=event.get("content_type", "text"),
            body=event.get("body"),
            media_ref=event.get("media_id"),
        ),
        conversation_id=conv["id"],
        history=await get_recent_messages(conn, business.id, conv["id"]),
        language=conv["locale"] or business.default_locale,
    )

    await run_pipeline(ctx, PipelineDeps(conn=conn, redis=redis), stages)

    if ctx.reply is None:
        # „niciodată tăcere" (principiul 6) e responsabilitatea stagiilor reale;
        # aici doar raportăm că turul n-a produs reply.
        return TurnResult(conv["id"], contact.id, turn_id, None, None)

    # Scrierea Sender-ului: mesaj outbound + outbox + state, în aceeași tranzacție.
    async with conn.transaction():
        out_msg_id = await insert_message(
            conn,
            business.id,
            conv["id"],
            contact.id,
            Direction.OUTBOUND,
            Author.BOT,
            body=ctx.reply.text,
            content_type="text",
            status="queued",
        )
        outbox_id = await enqueue_outbox(
            conn,
            business.id,
            conv["id"],
            turn_id,  # idempotency_key = turn → un singur outbox per tur
            {
                "type": "text",
                "to": wa_id,
                "text": ctx.reply.text,
                "message_id": out_msg_id,
            },
        )
        await patch_conversation_state(
            conn,
            business.id,
            conv["id"],
            conv["state"],
            conv["state_version"],
            touch_outbound=True,
        )

    return TurnResult(conv["id"], contact.id, turn_id, ctx.reply.text, outbox_id)
