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
from dataclasses import asdict, dataclass
from uuid import uuid4

import asyncpg
from redis.asyncio import Redis

from src.agent import usage
from src.agent.llm import get_llm
from src.agent.pricing import savings_for
from src.cache.canonical import canonicalize, classify_volatility
from src.channels.media import get_media_registry
from src.config import get_settings
from src.db.queries.analytics import insert_events
from src.db.queries.businesses import get_data_version
from src.db.queries.contacts import get_or_create_contact, update_contact_profile_and_score
from src.db.queries.conversations import (
    get_or_create_conversation,
    patch_conversation_state,
    touch_last_inbound,
)
from src.db.queries.inbound_dedupe import claim_inbound, mark_inbound_completed
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
    Reply,
    TurnContext,
    TurnUsage,
)
from src.worker.compose import ensure_disclaimer
from src.worker.limits import cost_add, cost_over_budget, estimate_turn_cost
from src.worker.profile import compute_lead_score, extract_profile, filter_profile_patch
from src.worker.reply_split import split_reply
from src.worker.runner import DEFAULT_STAGES, PipelineDeps, Stage, run_pipeline
from src.worker.summarizer import generate_summary

log = logging.getLogger(__name__)


@dataclass
class TurnResult:
    """Rezultatul procesării unui tur (pentru logging/teste).

    `reply` + `language` sunt populate DOAR pe calea sincronă (`deliver=False`, gateway web
    request/response): apelantul mapează `reply` (text + produse + chips) direct în răspunsul HTTP,
    fără outbox/dispatcher. Pe calea async (WhatsApp/Telegram/SSE) rămân None — livrarea e prin
    outbox, iar `reply_text` (text PUR, fără disclaimer) e suficient pt log/teste."""

    conversation_id: str | None
    contact_id: str | None
    turn_id: str | None
    reply_text: str | None
    outbox_id: str | None
    deduped: bool = False
    reply: Reply | None = None  # obiectul complet (sync); None pe calea async (outbox)
    language: str | None = None  # limba turului (pt re-aplicarea disclaimer-ului la mapare sync)


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


async def _extract_profile_and_score(
    conn, redis, ctx: TurnContext, llm, *, shadow_mode: bool
) -> None:
    """POST-TUR async (NX-88), best-effort — botul „învață" clientul. Un apel NANO extrage semnale →
    patch FILTRAT pe whitelist pe `contacts.profile` + `lead_score` DETERMINIST (formulă în cod,
    nu numărul LLM-ului). Rulează DUPĂ outbox (nu întârzie livrarea). Owner EXCLUSIV la runtime pe
    contacts.profile + contacts.lead_score (P3).

    Degradare/skip (P6): kill-switch / llm None (cost guard, fără cheie) / shadow mode (NX-93) /
    tur deflectat de free-layer/cache/gates (`ctx.route is None` ⟺ triajul n-a rulat → niciun
    semnal de profil, n-ar trebui taxat cu un apel nano — card §Cost) / orice eroare → skip.
    PII (P12): evenimentele logă DOAR chei + contoare, niciodată valori sau corpul mesajului."""
    settings = get_settings()
    if not settings.profile_extraction_enabled or llm is None or shadow_mode or ctx.route is None:
        return
    try:
        delta = await extract_profile(llm, ctx.history, ctx.message, ctx.language)
        if delta is None:
            return  # parse/API fail → fail-soft, nimic de scris
        # Apelul nano a avut loc → contabilizează-l în contorul zilnic (G2c), altfel scapă bugetul.
        if redis is not None and settings.cost_guard_enabled:
            await cost_add(redis, ctx.business.id, settings.cost_triage_usd)

        patch, dropped = filter_profile_patch(delta.profile_patch, ctx.business.vertical)
        new_score = compute_lead_score(delta.lead_signals, ctx)
        old_score = float(ctx.contact.lead_score)
        score_changed = abs(new_score - old_score) > 1e-9

        # Cheile aruncate sunt un semnal (NX-43) chiar dacă nu scriem nimic altceva.
        events = [Event("profile_key_dropped", {"key": k}) for k in dropped]
        if patch or score_changed:
            # Savepoint propriu: UPDATE eșuat → rollback doar la el (turul a răspuns deja).
            async with conn.transaction():
                await update_contact_profile_and_score(
                    conn, ctx.business.id, ctx.contact.id, patch, new_score
                )
            if patch:
                events.append(
                    Event("profile_updated", {"keys_set": sorted(patch), "dropped": len(dropped)})
                )
            if score_changed:
                events.append(
                    Event(
                        "lead_score_updated",
                        {"old": round(old_score, 2), "new": round(new_score, 2)},
                    )
                )
        if events:
            await insert_events(
                conn,
                ctx.business.id,
                events,
                conversation_id=ctx.conversation_id,
                contact_id=ctx.contact.id,
            )
    except Exception:  # noqa: BLE001 — best-effort: turul a răspuns deja, nimic nu se rupe
        log.exception("extractor profil a eșuat (turul continuă)")


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


def _message_usage_kwargs(turn_usage: TurnUsage | None) -> dict:
    """Câmpurile de observabilitate (NX-103) atașate pe rândul `messages` outbound al botului:
    tokeni/cost/latență/model. DOAR pe primul fragment al reply-ului (split-ul e același reply).
    None/zero apeluri → {} (mesaj fără cost: cache/free-layer/welcome — corect 0, nu NULL fals)."""
    if turn_usage is None or turn_usage.calls == 0:
        return {}
    return {
        "model_route": ",".join(turn_usage.models) or None,
        "tokens_in": turn_usage.tokens_in,
        "tokens_out": turn_usage.tokens_out,
        "cost_usd": round(turn_usage.cost_usd, 6),
        "latency_ms": int(round(turn_usage.latency_ms)),
    }


def _usage_event_props(acc: usage.UsageAccumulator, *, phase: str) -> dict:
    """Props pentru un event `llm_usage` dintr-un acumulator (folosit la POST-tur: summarizer +
    profil + cache write-back embed). Aceeași formă ca runner-ul → rollup-ul/raportul le tratează
    uniform; `phase` separă reply-ul de fundalul amortizat."""
    savings = sum(savings_for(model, row["cached_tokens"]) for model, row in acc.by_model.items())
    return {
        "phase": phase,
        "tokens_in": acc.tokens_in,
        "tokens_out": acc.tokens_out,
        "cached_tokens": acc.cached_tokens,
        "cost_usd": round(acc.cost_usd, 6),
        "savings_usd": round(savings, 6),
        "llm_calls": acc.calls,
        "by_model": acc.by_model,
    }


async def handle_turn(
    conn: asyncpg.Connection,
    business: BusinessConfig,
    channel_id: str,
    event: dict,
    *,
    redis: Redis | None = None,
    stages: list[Stage] | None = None,
    deliver: bool = True,
) -> TurnResult:
    """Procesează un mesaj inbound pe o conexiune tenant-scoped pe `business.id`.

    `event` = envelope-ul neutru (InboundEvent.to_dict): channel_kind,
    channel_account_id, sender_external_id, provider_msg_id, content_type, body, ...

    `deliver` (NX-25b — gateway web sincron): True (default, calea async) = Sender-ul scrie
    reply-ul în `outbox` → dispatcher-ul îl livrează (WhatsApp/Telegram/SSE), eventual spart în 2.
    False (request/response: răspunsul HTTP E transportul) = persistăm mesajul outbound (status
    `sent`, un singur fragment) + state, dar NU punem în outbox (n-ar avea cine-l livra) și
    întoarcem `ctx.reply` în `TurnResult` ca apelantul să-l mapeze în răspuns. Restul (dedupe,
    history, analytics, cache, profil) e identic — sincronul nu pierde nimic din pipeline.
    """
    stages = stages or DEFAULT_STAGES
    turn_id = str(uuid4())
    channel_kind = event.get("channel_kind", "whatsapp")
    sender_external_id = event["sender_external_id"]
    provider_msg_id = event.get("provider_msg_id")

    # Dedupe layer 2 (durabil): retry Meta care a scăpat de Redis (FLUSHALL/restart).
    # Guard ÎNAINTE de orice scriere — un duplicat nu produce mesaj, nici outbox.
    # NX-86 (dead-letter închis): claim-or-resume — un crash în mijlocul turului lasă
    # completed_at NULL → orfanul e reclamat după CLAIM_TTL (nu mai e „văzut dar neprocesat").
    # mark_inbound_completed (mai jos, în TX-ul de outbox) îl finalizează atomic la succes.
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
            channel_kind=channel_kind,
            channel_account_id=event.get("channel_account_id", ""),
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
    # Media routing (NX-76): registry de fetchers (singleton, ca llm) → gate-ul descarcă poza.
    media = get_media_registry()
    await run_pipeline(ctx, PipelineDeps(conn=conn, redis=redis, llm=llm, media=media), stages)
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
        # NX-86: tur DONE (halt/no-reply) → finalizează claim-ul (altfel reaper-ul l-ar reprocesa).
        if provider_msg_id:
            await mark_inbound_completed(conn, business.id, provider_msg_id)
        return TurnResult(
            conv["id"], contact.id, turn_id, None, None, language=ctx.language
        )

    # Sender (P5): garantează disclaimer-ul AI (art. 50) pe text, o singură dată, pt TOATE
    # rutele (simple/clarify/prose/fallback/cache). Idempotent — rich/welcome și-l aplatizează
    # deja → nu dublează. `ctx.reply.text` rămâne PUR (owner = stagiul; cache-ul îl stochează fără
    # disclaimer și-l re-aplică la hit). NX-134.
    reply_text = ensure_disclaimer(ctx.reply.text, ctx.language)

    # Sender (P5): 1-2 mesaje outbound + outbox + state, în aceeași tranzacție. NX-90: reply lung
    # de TEXT PUR → spart în max 2 fragmente (citire ușoară pe telefon); carusel/rich → un singur
    # fragment (spargerea lead-in-ului ar strica ordinea cardurilor). Spargerea NU atinge state-ul.
    is_rich = ctx.reply.rich is not None
    has_products = bool(ctx.reply.products)
    # Sync (deliver=False): un singur `content` în răspunsul HTTP — nu spargem (frontendul randează
    # o bulă). Rich/carusel rămân un fragment oricum (spargerea lead-in-ului ar strica ordinea).
    if is_rich or has_products or not deliver:
        fragments = [reply_text]
    else:
        fragments = split_reply(reply_text, limit=get_settings().reply_split_chars)

    async with conn.transaction():
        new_state = conv["state"]
        if (is_rich or has_products) and ctx.reply.products:
            # Recomandare BOGATĂ (iZi) / carusel (R2): persistăm setul afișat → navigarea
            # caruselului (handle_callback) îl citește din state (ref-uri, principiul 8).
            new_state = {**conv["state"], "displayed_products": ctx.reply.products}
        # NX-130: persistă slotul de clarificare (reply CLARIFY) sau curăță-l (orice alt reply →
        # pending_question default None) → nu lăsăm întrebări zombi în state.
        new_state = {**new_state, "pending_question": ctx.reply.pending_question}
        # NX-79: mutații de state cerute de tool-uri (ex. cart_add → {"cart": [...]}), acumulate
        # în stagiul Agent. Owner unic = Agent; processor-ul doar le merge-uiește la scriere (P3).
        if ctx.state_patch:
            new_state = {**new_state, **ctx.state_patch}

        first_outbox_id = None
        for i, frag in enumerate(fragments):
            out_msg_id = await insert_message(
                conn,
                business.id,
                conv["id"],
                contact.id,
                Direction.OUTBOUND,
                Author.BOT,
                body=frag,
                content_type="text",
                # Sync: livrarea e răspunsul HTTP → mesajul e deja `sent` (n-are dispatcher care
                # să-l ducă din `queued`). Async: `queued` până trece dispatcher-ul.
                status="queued" if deliver else "sent",
                # NX-103: cost/tokeni/latență/model pe PRIMUL fragment (reply-ul botului). Split-ul
                # (frag 2) e același reply → nu dublăm costul. messages.cost_usd devine real.
                **(_message_usage_kwargs(ctx.usage) if i == 0 else {}),
            )
            if not deliver:
                # Sync (deliver=False): NU punem în outbox — răspunsul HTTP e transportul.
                # Persistăm doar mesajul outbound (history) + state mai jos.
                continue
            payload = {
                "type": "text",
                "to": sender_external_id,
                "text": frag,
                "message_id": out_msg_id,
            }
            # Extras-urile bogate stau pe PRIMUL fragment (rich/carusel = un fragment oricum):
            # `type=text` rămâne (allow-list); canalele cu send_rich/carousel randează bogat.
            if i == 0 and is_rich:
                payload["rich"] = asdict(ctx.reply.rich)
            elif i == 0 and has_products:
                payload["type"] = "carousel"
                payload["products"] = ctx.reply.products
            outbox_id = await enqueue_outbox(
                conn,
                business.id,
                conv["id"],
                f"{turn_id}:{i}",  # idempotency_key per fragment (turn:0 / turn:1)
                payload,
            )
            if first_outbox_id is None:
                first_outbox_id = outbox_id
        await patch_conversation_state(
            conn,
            business.id,
            conv["id"],
            new_state,
            conv["state_version"],
            touch_outbound=True,
        )
        # NX-86: finalizează claim-ul ÎN aceeași TX cu outbox → atomic. Crash înainte de commit →
        # completed_at NULL → orfan recuperabil; commit → finalizat, niciodată reprocesat.
        if provider_msg_id:
            await mark_inbound_completed(conn, business.id, provider_msg_id)

    outbox_id = first_outbox_id
    if len(fragments) > 1:
        # Observabilitate (P10): emis după persistarea principală de events → persistăm separat.
        split_ev = Event("reply_split", {"parts": len(fragments)})
        ctx.events.append(split_ev)
        await _persist_events(conn, business.id, conv["id"], contact.id, [split_ev])

    # Log per-tur la succes (fără PII: doar id-uri + lungimea reply-ului, nu corpul).
    log.info(
        "tur procesat: conv=%s turn=%s reply=%dch outbox=%s",
        conv["id"],
        turn_id,
        len(reply_text),
        outbox_id,
    )
    # POST-TUR async (cache write-back + summarizer + profil) — după outbox, nu întârzie livrarea.
    # NX-103: îl învelim într-un acumulator de usage propriu → un al doilea event `llm_usage`
    # (phase=post_turn) ca embed-ul de cache + apelurile nano de fundal să NU scape rollup-ului
    # zilnic (altfel costul real al zilei e subestimat). NU intră pe rândul `messages` al reply-ului
    # (nu e parte din reply); intră doar în analytics_events (rollup + raport de cost).
    post_acc, post_token = usage.push()
    try:
        # Write-back cache (G5b-1). LLM-ul guardat (cost guard): peste buget → None → sărit.
        await _cache_writeback(conn, llm, business.id, ctx.language, ctx.message.body, ctx)
        # Summarizer (G6-2 felia 2) — întreține rezumatul rolling. `llm` guardat → None → sare.
        await _summarize_if_needed(conn, redis, business.id, conv["id"], ctx, llm)
        # Extractor profil + lead_score (NX-88) — nano extrage semnale → patch whitelist pe
        # contacts.profile + scor determinist. Guardat (cost guard / shadow / free-layer → skip).
        await _extract_profile_and_score(
            conn, redis, ctx, llm, shadow_mode=bool(conv.get("shadow_mode"))
        )
    finally:
        usage.pop(post_token)
    if post_acc.calls:
        post_ev = Event("llm_usage", _usage_event_props(post_acc, phase="post_turn"))
        ctx.events.append(post_ev)
        await _persist_events(conn, business.id, conv["id"], contact.id, [post_ev])
    return TurnResult(
        conv["id"],
        contact.id,
        turn_id,
        ctx.reply.text,
        outbox_id,
        reply=ctx.reply,  # sync: apelantul mapează text+produse+chips în răspunsul HTTP
        language=ctx.language,
    )
