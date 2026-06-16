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
from src.db.queries.businesses import get_data_version
from src.db.queries.contacts import get_or_create_contact
from src.db.queries.conversations import (
    get_or_create_conversation,
    patch_conversation_state,
    touch_last_inbound,
)
from src.db.queries.inbound_dedupe import claim_inbound
from src.db.queries.messages import (
    count_messages,
    get_messages_for_summary,
    get_recent_messages,
    insert_message,
)
from src.db.queries.outbox import enqueue_outbox
from src.db.queries.semantic_cache import upsert_entry
from src.db.queries.summaries import (
    get_latest_summary,
    get_summary_for_context,
    insert_conversation_summary,
)
from src.models import (
    Author,
    BusinessConfig,
    ConversationState,
    Direction,
    Event,
    InboundMessage,
    TurnContext,
)
from src.worker.limits import cost_add, cost_over_budget, estimate_turn_cost
from src.worker.runner import DEFAULT_STAGES, PipelineDeps, Stage, run_pipeline
from src.worker.summarizer import generate_summary

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
    """Write-back gated (G5b), best-effort — scrie răspunsul în semantic_cache ca să
    serveas tururi viitoare fără LLM. Rulează DUPĂ outbox (nu întârzie livrarea).

    Gate (precision-first): nu re-scriem un hit; doar răspunsuri reutilizabile (cacheable,
    nu clarify/fallback). Două tiere, după volatilitatea query-ului:
      • `static` (fără produse) → entry FAQ/generic, TTL în zile (G5b-1).
      • `dynamic` (recomandare cu produse) → entry cu `retrieval_signature` (snapshot de
        preț) + `data_version`, TTL scurt în minute (G5b-2). Invalidat la lookup prin
        price-check, deci sigur de cache-uit (zero preț învechit servit)."""
    settings = get_settings()
    reply = ctx.reply
    if not settings.cache_enabled or ctx.from_cache or reply is None or llm is None:
        return
    if not reply.cacheable:
        return
    text = (reply.text or "").strip()
    if not 5 <= len(text) <= 4000:
        return

    volatility = classify_volatility(body)
    # Parametrii upsert-ului diferă pe tier; gate-ul de eligibilitate îi decide.
    kwargs: dict = {}
    if volatility == "static" and reply.products is None:
        kwargs = {"ttl_days": settings.cache_ttl_static_days}
    elif volatility == "dynamic" and reply.products:
        kwargs = {
            "ttl_minutes": settings.cache_ttl_dynamic_minutes,
            "retrieval_signature": [
                {"product_id": p["product_id"], "price": p["price"]} for p in reply.products
            ],
            "data_version": await get_data_version(conn, business_id),
        }
    else:
        # static cu produse / dynamic fără produse / realtime → nu se cache-uiește.
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
                volatility_class=volatility,
                embedding_model=settings.model_embed,
                quality_score=1.0,
                **kwargs,
            )
        if volatility == "dynamic":
            ctx.emit("cache_write", volatility="dynamic", n_products=len(reply.products))
        else:
            ctx.emit("cache_write", volatility="static", ttl_days=settings.cache_ttl_static_days)
    except Exception:  # noqa: BLE001 — write-back best-effort, turul a răspuns deja
        log.exception("cache write-back a eșuat (turul continuă)")


async def _summarize_if_needed(conn, redis, business_id, conversation_id, ctx, llm) -> None:
    """POST-TUR async (G6-2 felia 2), best-effort — întreține rezumatul rolling al conversației.
    Rulează DUPĂ outbox (nu întârzie livrarea). NANO (model_triage), în afara pipeline-ului
    sincron → principiul 2 respectat. Degradare (P6): kill-switch/llm None/eroare → skip, turul
    a răspuns deja.

    Acoperire CORECTĂ: sumarizează fereastra `get_messages_for_summary` (mesajele care ies din
    ultimele 8, NU `ctx.history`), iar watermark-ul = cel mai NOU mesaj INCLUS (onest). Anti-
    regenerare: re-sumarizăm doar la >= `summary_regen_delta` mesaje noi. Cost: apelul nano intră
    în contorul zilnic G2c (`cost_add`), altfel ar scăpa bugetului."""
    settings = get_settings()
    if not settings.summary_enabled or llm is None:
        return
    try:
        total = await count_messages(conn, business_id, conversation_id)
        if total < settings.summary_threshold:
            return
        prev = await get_latest_summary(conn, business_id, conversation_id)
        watermark = prev["upto_message_at"] if prev else None
        to_summarize = await get_messages_for_summary(
            conn, business_id, conversation_id, after=watermark
        )
        if not to_summarize:
            return  # totul nou e încă în fereastra de 8 → nimic de comprimat
        if prev is not None and len(to_summarize) < settings.summary_regen_delta:
            return  # prea puține mesaje noi → nu ardem un apel nano (limbo temporar acceptat)

        summary = await generate_summary(
            llm, to_summarize, prev["summary"] if prev else None, ctx.language
        )
        if not summary:
            return
        new_watermark = to_summarize[-1].created_at  # cel mai nou mesaj INCLUS (watermark onest)
        # tranzacție proprie (savepoint dacă rulăm nested, altfel BEGIN): insert eșuat → rollback
        # doar la el; turul a răspuns deja, restul fluxului nu e afectat.
        async with conn.transaction():
            await insert_conversation_summary(
                conn, business_id, conversation_id, new_watermark, summary
            )
        # Cost guard (G2c): apelul nano extra trebuie contabilizat, altfel scapă plafonului zilnic.
        if redis is not None and settings.cost_guard_enabled:
            await cost_add(redis, business_id, settings.cost_triage_usd)
        await insert_events(
            conn,
            business_id,
            [Event("summarizer_run", {"messages": len(to_summarize)})],
            conversation_id=conversation_id,
        )
        log.info(
            "summarizer: rezumat scris conv=%s msgs=%d len=%dch",
            conversation_id,
            len(to_summarize),
            len(summary),
        )
    except Exception:  # noqa: BLE001 — best-effort: turul a răspuns deja, nimic nu se rupe
        log.exception("summarizer a eșuat (turul continuă)")


async def _llm_within_budget(ctx: TurnContext, redis: Redis | None, business: BusinessConfig):
    """Cost guard (G2c): dacă businessul a depășit plafonul zilnic, întoarce None → pipeline-ul
    rulează FĂRĂ LLM (triaj/agent degradează grațios, cache L1 încă servește), dar gates rămân
    intacte. Best-effort: eșec Redis → fail-open (LLM normal). Plafon =
    businesses.daily_cost_cap_usd sau settings.daily_cost_cap_usd."""
    llm = get_llm()
    settings = get_settings()
    if llm is None or redis is None or not settings.cost_guard_enabled:
        return llm
    cap = business.daily_cost_cap_usd or settings.daily_cost_cap_usd
    if not cap:
        return llm
    try:
        if await cost_over_budget(redis, business.id, cap):
            ctx.emit("cost_guard_tripped", cap_usd=cap)
            log.warning(
                "cost guard: business %s peste plafon ($%.2f) → LLM dezactivat", business.id, cap
            )
            return None
    except Exception as e:  # noqa: BLE001 — check eșuat → fail-open
        log.warning("cost guard: check eșuat (%s) → fail-open", type(e).__name__)
    return llm


async def _record_turn_cost(
    redis: Redis | None, business_id: str, ctx: TurnContext, *, llm_used: bool
) -> None:
    """Adaugă estimarea de cost a turului în contorul zilnic (G2c) — DOAR dacă LLM-ul a fost
    folosit (peste buget nu acumulează). Best-effort; sursa de facturare = usage_daily."""
    settings = get_settings()
    if redis is None or not settings.cost_guard_enabled or not llm_used:
        return
    cost = estimate_turn_cost(
        ctx.events, cost_triage_usd=settings.cost_triage_usd, cost_agent_usd=settings.cost_agent_usd
    )
    if cost <= 0:
        return
    try:
        await cost_add(redis, business_id, cost)
    except Exception as e:  # noqa: BLE001 — contor best-effort, turul a răspuns deja
        log.warning("cost guard: add eșuat (%s)", type(e).__name__)


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
        state=ConversationState.from_jsonb(conv["state"]),  # G6-2: agentul vede ce-a afișat
        summary=await get_summary_for_context(conn, business.id, conv["id"]),  # G6-2 felia 2
        language=conv["locale"] or business.default_locale,
        bot_active=conv["bot_active"],
        handoff_until=conv["handoff_until"],
    )

    # Cost guard (G2c): peste plafonul zilnic → llm=None (degradare). Gates rulează oricum.
    llm = await _llm_within_budget(ctx, redis, business)
    await run_pipeline(ctx, PipelineDeps(conn=conn, redis=redis, llm=llm), stages)
    await _persist_events(conn, business.id, conv["id"], contact.id, ctx.events)
    await _record_turn_cost(redis, business.id, ctx, llm_used=llm is not None)

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
    # Write-back cache (G5b-1) — după outbox, nu întârzie livrarea. LLM-ul guardat (cost
    # guard): peste buget → None → write-back sărit (nu mai consumăm embed).
    await _cache_writeback(conn, llm, business.id, ctx.language, ctx.message.body, ctx)
    # Summarizer (G6-2 felia 2) — post-tur async, întreține rezumatul rolling. `llm` e cel
    # guardat de cost guard: peste buget → None → hook-ul se sare (P2 + P6 gratis).
    await _summarize_if_needed(conn, redis, business.id, conv["id"], ctx, llm)
    return TurnResult(conv["id"], contact.id, turn_id, ctx.reply.text, outbox_id)
