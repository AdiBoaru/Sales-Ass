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

import logging
from dataclasses import dataclass
from uuid import uuid4

import asyncpg
from redis.asyncio import Redis

from src.agent.llm import get_llm
from src.cache.canonical import canonicalize, classify_volatility
from src.config import get_settings
from src.db.queries.analytics import insert_events
from src.db.queries.contacts import get_or_create_contact
from src.db.queries.conversations import (
    get_or_create_conversation,
    patch_conversation_state,
    touch_last_inbound,
)
from src.db.queries.inbound_dedupe import claim_inbound
from src.db.queries.messages import get_recent_messages, insert_message
from src.db.queries.outbox import enqueue_outbox
from src.db.queries.semantic_cache import upsert_entry
from src.models import (
    Author,
    BusinessConfig,
    Direction,
    InboundMessage,
    TurnContext,
)
from src.worker.runner import DEFAULT_STAGES, PipelineDeps, Stage, run_pipeline

log = logging.getLogger(__name__)


@dataclass
class TurnResult:
    """Rezultatul procesării unui tur (pentru logging/teste)."""

    conversation_id: str | None
    contact_id: str | None
    turn_id: str | None
    reply_text: str | None
    outbox_id: str | None
    deduped: bool = False


async def _persist_events(conn, business_id, conversation_id, contact_id, events) -> None:
    """Scrie evenimentele turului în analytics_events, best-effort.
    Observabilitatea NU blochează turul: un eșec se loghează, nu propagă."""
    if not events:
        return
    try:
        await insert_events(
            conn,
            business_id,
            events,
            conversation_id=conversation_id,
            contact_id=contact_id,
        )
    except Exception:  # noqa: BLE001 — analytics e best-effort
        log.exception("persistarea analytics_events a eșuat (turul continuă)")


async def _cache_writeback(conn, llm, business_id, locale, body, ctx) -> None:
    """Write-back gated (G5b-1), best-effort — scrie răspunsul în semantic_cache ca să
    serveas tururi viitoare fără LLM. Rulează DUPĂ outbox (nu întârzie livrarea).

    Gate (precision-first): nu re-scriem un hit; doar tier static; fără produse
    (dynamic = G5b-2); doar răspunsuri reutilizabile (cacheable, nu clarify/fallback)."""
    settings = get_settings()
    reply = ctx.reply
    if not settings.cache_enabled or ctx.from_cache or reply is None or llm is None:
        return
    if reply.products is not None or not reply.cacheable:
        return
    text = (reply.text or "").strip()
    if not 5 <= len(text) <= 4000:
        return
    if classify_volatility(body) != "static":
        return
    try:
        canonical, canonical_hash = canonicalize(body or "")
        if not canonical:
            return
        embedding = (await llm.embed([canonical]))[0]
        # Savepoint: dacă upsert-ul eșuează (RLS/grant/conflict), rollback DOAR la el —
        # nu poluează tranzacția apelantului (turul a răspuns deja).
        async with conn.transaction():
            await upsert_entry(
                conn,
                business_id,
                locale,
                canonical_str=canonical,
                canonical_hash=canonical_hash,
                embedding=embedding,
                answer=text,
                volatility_class="static",
                embedding_model=settings.model_embed,
                quality_score=1.0,
                ttl_days=settings.cache_ttl_static_days,
            )
        ctx.emit("cache_write", volatility="static", ttl_days=settings.cache_ttl_static_days)
    except Exception:  # noqa: BLE001 — write-back best-effort, turul a răspuns deja
        log.exception("cache write-back a eșuat (turul continuă)")


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

    `event` = envelope-ul neutru (InboundEvent.to_dict): channel_kind,
    channel_account_id, sender_external_id, provider_msg_id, content_type, body, ...
    """
    stages = stages or DEFAULT_STAGES
    turn_id = str(uuid4())
    channel_kind = event.get("channel_kind", "whatsapp")
    sender_external_id = event["sender_external_id"]
    provider_msg_id = event.get("provider_msg_id")

    # Dedupe layer 2 (durabil): retry Meta care a scăpat de Redis (FLUSHALL/restart).
    # Guard ÎNAINTE de orice scriere — un duplicat nu produce mesaj, nici outbox.
    # Trade-off (NX-51): claim-ul se commit-ează imediat, deci un crash în mijlocul
    # turului marchează mesajul ca văzut fără a-l finaliza (dead-letter = follow-up).
    if provider_msg_id and not await claim_inbound(conn, business.id, provider_msg_id):
        log.info("dedupe_hit_db: %s deja procesat (business %s)", provider_msg_id, business.id)
        return TurnResult(None, None, None, None, None, deduped=True)

    contact = await get_or_create_contact(
        conn,
        business.id,
        channel_kind,
        sender_external_id,
        display_name=event.get("sender_name"),
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
        bot_active=conv["bot_active"],
        handoff_until=conv["handoff_until"],
    )

    await run_pipeline(ctx, PipelineDeps(conn=conn, redis=redis, llm=get_llm()), stages)
    await _persist_events(conn, business.id, conv["id"], contact.id, ctx.events)

    if ctx.reply is None:
        if ctx.halt:
            # tăcere INTENȚIONATĂ (Gates): handoff activ / bot oprit — omul se ocupă.
            log.info("tăcere intenționată (handoff): conv=%s turn=%s", conv["id"], turn_id)
        else:
            # „niciodată tăcere" (principiul 6) e responsabilitatea stagiilor reale;
            # aici doar raportăm că turul n-a produs reply.
            log.info("tur procesat fără reply: conv=%s turn=%s", conv["id"], turn_id)
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
        payload = {
            "type": "text",
            "to": sender_external_id,
            "text": ctx.reply.text,
            "message_id": out_msg_id,
        }
        new_state = conv["state"]
        if ctx.reply.products:
            # carusel de produs (R2): primul card + butoane ◀🛒▶; W1 (products) =
            # fallback în dispatcher. Persistăm setul afișat în state → navigarea
            # caruselului (handle_callback) îl citește de acolo (ref-uri, principiul 8).
            payload["type"] = "carousel"
            payload["products"] = ctx.reply.products
            new_state = {**conv["state"], "displayed_products": ctx.reply.products}
        outbox_id = await enqueue_outbox(
            conn,
            business.id,
            conv["id"],
            turn_id,  # idempotency_key = turn → un singur outbox per tur
            payload,
        )
        await patch_conversation_state(
            conn,
            business.id,
            conv["id"],
            new_state,
            conv["state_version"],
            touch_outbound=True,
        )

    # Log per-tur la succes (fără PII: doar id-uri + lungimea reply-ului, nu corpul).
    log.info(
        "tur procesat: conv=%s turn=%s reply=%dch outbox=%s",
        conv["id"],
        turn_id,
        len(ctx.reply.text),
        outbox_id,
    )
    # Write-back cache (G5b-1) — după outbox, nu întârzie livrarea.
    await _cache_writeback(conn, get_llm(), business.id, ctx.language, ctx.message.body, ctx)
    return TurnResult(conv["id"], contact.id, turn_id, ctx.reply.text, outbox_id)
