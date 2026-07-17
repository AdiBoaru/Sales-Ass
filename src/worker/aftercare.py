"""Felia 1 (NX-161) — aftercare POST-TUR off-conn: cache write-back + summarizer + profil/facts.

Rulează DUPĂ ce reply-ul a fost commis (outbox), best-effort (P6: un eșec NU afectează reply/outbox/
`mark_inbound_completed`). Ținta epic-ului: LLM-ul de fundal (embed/summarizer/profil) NU trebuie
să țină o conexiune DB. De aceea aftercare-ul ia un `db` PROVIDER (nu un `conn` viu):

  • inline (`static_db(conn)`, teste/sim) → același conn viu → comportament byte-identic;
  • deferred (`tenant_db(biz)`, PRODUCȚIE) → apelantul închide `tenant_conn` ÎNAINTE → fiecare
    helper ia conn DOAR scurt (read / write), iar LLM-ul rulează cu conn ELIBERAT.

REGULA 1 (Codex #207/F1): niciun helper nu ține `async with db()` peste un apel LLM. Structura e
mereu `read scurt → (LLM fără conn) → write scurt`. Altfel „deferred" ar fi doar pe hârtie.
"""

import logging
from dataclasses import dataclass

from redis.asyncio import Redis

from src.agent import usage
from src.agent.llm import LLMClient
from src.agent.pricing import savings_for
from src.cache.canonical import canonicalize, classify_volatility
from src.config import get_settings
from src.db.provider import DbProvider
from src.db.queries.analytics import insert_events
from src.db.queries.businesses import get_data_version
from src.db.queries.contacts import update_contact_profile_and_score
from src.db.queries.facts import (
    get_messages_for_extraction,
    select_whitelisted_facts,
    upsert_facts,
)
from src.db.queries.messages import count_messages, get_messages_for_summary
from src.db.queries.semantic_cache import upsert_entry
from src.db.queries.summaries import get_latest_summary, insert_conversation_summary
from src.domain.loader import load_domain_pack
from src.models import BusinessConfig, Event, TurnContext
from src.safety.policy import SafetyPolicy
from src.worker.canonicalize import canonical_keys_for
from src.worker.limits import cost_add
from src.worker.memory import process_facts
from src.worker.profile import (
    build_ref_map,
    compute_lead_score,
    extract_profile,
    filter_profile_patch,
)
from src.worker.summarizer import generate_summary

log = logging.getLogger(__name__)


@dataclass
class AftercareWork:
    """Snapshot MIC + controlat cu ce-i trebuie aftercare-ului (nu tot ctx la voia întâmplării, deși
    `ctx` e inclus — îl folosesc toate 3 helperele). Owner: processor (îl umple în handle_turn când
    `defer_aftercare=True`). Fără PII de canal nouă în logs/events (helperele emit chei, nu valori).
    """

    business: BusinessConfig
    conversation_id: str
    contact_id: str
    ctx: TurnContext
    inbound_msg_id: str | None
    shadow_mode: bool
    llm: LLMClient | None
    language: str


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


def _usage_event_props(acc: usage.UsageAccumulator, *, phase: str) -> dict:
    """Props pentru un event `llm_usage` dintr-un acumulator (POST-tur: summarizer + profil + cache
    embed). Aceeași formă ca runner-ul → rollup-ul/raportul le tratează uniform; `phase` separă
    reply-ul de fundalul amortizat."""
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


async def _cache_writeback(db: DbProvider, llm, business_id, locale, body, ctx) -> None:
    """Write-back gated (G5b), best-effort — scrie răspunsul în semantic_cache ca să servească
    tururi viitoare fără LLM. Gate (precision-first): nu re-scriem un hit; doar reutilizabile.
    Două tiere: `static` (fără produse, TTL zile) și `dynamic` (produse, `retrieval_signature` +
    `data_version`, TTL minute — invalidat la lookup prin price-check).

    NX-161 F1: `data_version` (read) + upsert (write) în checkout-uri SCURTE separate; embed-ul LLM
    rulează ÎNTRE ele, fără conn ținut (regula 1)."""
    settings = get_settings()
    reply = ctx.reply
    if not settings.cache_enabled or ctx.from_cache or reply is None or llm is None:
        return
    if not reply.cacheable:
        return
    # NX-173 (P0): un răspuns compus sub context de siguranță e relativ la CLIENT, nu la query —
    # nu intră NICIODATĂ în cache-ul partajat (l-ar servi altcuiva, ori i l-ar servi lui altă dată
    # fără gate). `enforce` pune deja `cacheable=False`; verificăm ȘI aici, explicit, fiindcă
    # write-back-ul e ultimul pas dinaintea otrăvirii și n-are voie să depindă de un flag setat de
    # altcineva (P0 ≠ „presupunem că upstream a marcat corect"). Vezi `stages/cache.py` (bypass
    # la CITIRE) — perechea completă read+write.
    if SafetyPolicy.for_turn(ctx).contexts:
        ctx.emit("cache_write_skipped", reason="safety_context")
        return
    text = (reply.text or "").strip()
    if not 5 <= len(text) <= 4000:
        return

    volatility = classify_volatility(body)
    # Gate de eligibilitate (fără DB): decide DOAR dacă se cache-uiește. Read-ul de `data_version`
    # (dynamic) se face în TRY, ca un eșec de CHECKOUT să fie best-effort — nu propage din aftercare
    # (altfel pe web sync ar da 500 după ce reply-ul e deja livrat — review Codex #208).
    if not (
        (volatility == "static" and reply.products is None)
        or (volatility == "dynamic" and reply.products)
    ):
        # static cu produse / dynamic fără produse / realtime / contextual → nu se cache-uiește.
        return

    try:
        if volatility == "static":
            kwargs: dict = {"ttl_days": settings.cache_ttl_static_days}
        else:  # dynamic (produse garantate de gate)
            async with db() as conn:  # READ scurt (data_version) — eliberat ÎNAINTE de embed
                data_version = await get_data_version(conn, business_id)
            kwargs = {
                "ttl_minutes": settings.cache_ttl_dynamic_minutes,
                "retrieval_signature": [
                    {"product_id": p["product_id"], "price": p["price"]} for p in reply.products
                ],
                "data_version": data_version,
            }
        canonical, canonical_hash = canonicalize(body or "")
        if not canonical:
            return
        embedding = (await llm.embed([canonical]))[0]  # LLM — FĂRĂ conn ținut
        async with db() as conn:  # WRITE scurt (upsert)
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


async def _summarize_if_needed(
    db: DbProvider, redis, business_id, conversation_id, ctx, llm
) -> None:
    """POST-TUR async (G6-2 felia 2), best-effort — întreține rezumatul rolling. NANO (triage).
    Sumarizează `get_messages_for_summary` (mesajele care ies din ultimele 8), watermark = cel mai
    NOU mesaj INCLUS. Anti-regenerare: re-sumarizăm doar la >= `summary_regen_delta` mesaje noi.

    NX-161 F1: reads + writes în checkout-uri SCURTE separate; `generate_summary` (LLM) între ele,
    fără conn ținut (regula 1)."""
    settings = get_settings()
    if not settings.summary_enabled or llm is None:
        return
    try:
        async with db() as conn:  # READS scurte
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

        summary = await generate_summary(  # LLM — FĂRĂ conn ținut
            llm, to_summarize, prev["summary"] if prev else None, ctx.language
        )
        if not summary:
            return
        new_watermark = to_summarize[-1].created_at  # cel mai nou mesaj INCLUS (watermark onest)
        async with db() as conn:  # WRITES scurte
            async with conn.transaction():
                await insert_conversation_summary(
                    conn, business_id, conversation_id, new_watermark, summary
                )
            # NX-122: persist OUT-OF-BAND cu turn_id stampat explicit → corelat cu turul la replay.
            await insert_events(
                conn,
                business_id,
                [Event("summarizer_run", {"messages": len(to_summarize), "turn_id": ctx.turn_id})],
                conversation_id=conversation_id,
            )
        # Cost guard (G2c): apelul nano extra trebuie contabilizat, altfel scapă plafonului zilnic.
        if redis is not None and settings.cost_guard_enabled:
            await cost_add(redis, business_id, settings.cost_triage_usd)
        log.info(
            "summarizer: rezumat scris conv=%s msgs=%d len=%dch",
            conversation_id,
            len(to_summarize),
            len(summary),
        )
    except Exception:  # noqa: BLE001 — best-effort: turul a răspuns deja, nimic nu se rupe
        log.exception("summarizer a eșuat (turul continuă)")


async def _extract_profile_and_score(
    db: DbProvider, redis, ctx: TurnContext, llm, *, shadow_mode: bool, source_message_id=None
) -> None:
    """POST-TUR async (NX-88), best-effort — botul „învață" clientul. Un apel NANO extrage semnale →
    patch FILTRAT pe whitelist pe `contacts.profile` + `lead_score` DETERMINIST. Owner EXCLUSIV la
    runtime pe contacts.profile + lead_score (P3). Skip (P6): kill-switch / llm None / shadow /
    `ctx.route is None` (free-layer/cache) / eroare. PII (P12): events logă DOAR chei + contoare.

    NX-161 F1: read window (short) → extract_profile (LLM, fără conn) → procesare pură → writes
    (short). LLM-ul NU ține niciodată conn (regula 1)."""
    settings = get_settings()
    if not settings.profile_extraction_enabled or llm is None or shadow_mode or ctx.route is None:
        return
    try:
        facts_on = settings.conversation_facts_enabled
        memory_v2 = facts_on and settings.memory_v2_enabled
        pack = load_domain_pack(ctx.business) if facts_on else None
        # NX-160: cu Memory v2, oferim modelului cheile canonice ale businessului (P9, generic).
        canonical_keys = canonical_keys_for(pack) if memory_v2 else None
        # READ window (short checkout) — apoi eliberat ÎNAINTE de LLM.
        if facts_on:
            async with db() as conn:
                window = await get_messages_for_extraction(
                    conn, ctx.business.id, ctx.conversation_id
                )
        else:
            window = ctx.history

        delta = await extract_profile(  # LLM — FĂRĂ conn ținut
            llm,
            window or ctx.history,
            ctx.message,
            ctx.language,
            include_facts=facts_on,
            canonical_keys=canonical_keys,
        )
        if delta is None:
            return  # parse/API fail → fail-soft, nimic de scris
        if redis is not None and settings.cost_guard_enabled:
            await cost_add(redis, ctx.business.id, settings.cost_triage_usd)

        # PROCESARE PURĂ (fără conn) — construim events + `kept` + `fact_types` înainte de writes.
        patch, dropped = filter_profile_patch(delta.profile_patch, ctx.business.vertical)
        new_score = compute_lead_score(delta.lead_signals, ctx)
        old_score = float(ctx.contact.lead_score)
        score_changed = abs(new_score - old_score) > 1e-9

        tid = ctx.turn_id
        events = [Event("profile_key_dropped", {"key": k, "turn_id": tid}) for k in dropped]
        kept: list | None = None
        fact_types: list = []

        if facts_on and delta.facts:
            events.append(Event("facts_extract_attempted", {"turn_id": tid}))
            if memory_v2:
                ref_map = build_ref_map(window or ctx.history)
                candidates = [
                    {
                        "raw_key": f.key,
                        "fact_value": f.fact_value,
                        "confidence": f.confidence,
                        "source_ref": f.source_ref,
                    }
                    for f in delta.facts
                ]
                proc = process_facts(
                    candidates,
                    pack,
                    source_message_id=source_message_id,
                    ref_map=ref_map,
                    canonicalize=settings.memory_canonicalize_enabled,
                )
                events.append(
                    Event("facts_candidates_extracted", {"n": len(delta.facts), "turn_id": tid})
                )
                events.append(
                    Event(
                        "facts_filtered",
                        {
                            "dropped_pii": proc.dropped,
                            "candidate_sensitive": proc.candidate,
                            "injectable": proc.injectable,
                            "turn_id": tid,
                        },
                    )
                )
                if proc.canonicalized:
                    events.append(
                        Event("facts_canonicalized", {"n": proc.canonicalized, "turn_id": tid})
                    )
                # captura LARGĂ OFF → păstrăm DOAR facts canonizabile (fără raw candidates).
                kept = (
                    proc.rows
                    if settings.memory_open_capture_enabled
                    else [r for r in proc.rows if r["canonical_key"]]
                )
                # NX-160: contacts.profile = cache al canonical facts. Facts canonice care-s ȘI chei
                # de profil completează patch-ul, dar `profile_patch` direct are PRIORITATE.
                canon_patch = {
                    r["canonical_key"]: r["fact_value"]
                    for r in kept
                    if r.get("canonical_key") and isinstance(r.get("fact_value"), (str, int, float))
                }
                canon_kept, _ = filter_profile_patch(canon_patch, ctx.business.vertical)
                for k, v in canon_kept.items():
                    patch.setdefault(k, v)
            else:
                wl = pack.fact_type_whitelist if pack else frozenset()
                legacy = [
                    {"fact_type": f.key, "fact_value": f.fact_value, "confidence": f.confidence}
                    for f in delta.facts
                    if f.key
                ]
                kept = select_whitelisted_facts(legacy, wl)

            if kept:
                # chei canonice/raw (P12 — chei, nu valori). Filtrul None evită TypeError pe un rând
                # raw safe (raw_key dar fără fact_type/canonical_key) → fix review Codex #201.
                fact_types = sorted(
                    t
                    for t in {
                        k.get("canonical_key") or k.get("raw_key") or k.get("fact_type")
                        for k in kept
                    }
                    if t
                )

        # WRITES (short checkout) — LLM-ul a rulat deja fără conn.
        async with db() as conn:
            if kept:
                try:
                    async with conn.transaction():
                        await upsert_facts(
                            conn, ctx.business.id, ctx.contact.id, ctx.conversation_id, kept
                        )
                    events.append(
                        Event(
                            "facts_extracted",
                            {"n_facts": len(kept), "types": fact_types, "turn_id": tid},
                        )
                    )
                except Exception as e:  # noqa: BLE001 — NX-160: NU tăcere (P6), semnalăm layer mort
                    log.exception("upsert_facts a eșuat (turul continuă)")
                    events.append(
                        Event(
                            "facts_persist_failed", {"error_type": type(e).__name__, "turn_id": tid}
                        )
                    )

            if patch or score_changed:
                async with conn.transaction():
                    await update_contact_profile_and_score(
                        conn, ctx.business.id, ctx.contact.id, patch, new_score
                    )
                if patch:
                    events.append(
                        Event(
                            "profile_updated",
                            {"keys_set": sorted(patch), "dropped": len(dropped), "turn_id": tid},
                        )
                    )
                if score_changed:
                    events.append(
                        Event(
                            "lead_score_updated",
                            {
                                "old": round(old_score, 2),
                                "new": round(new_score, 2),
                                "turn_id": tid,
                            },
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


async def run_aftercare(db: DbProvider, redis: Redis | None, work: AftercareWork) -> None:
    """Orchestrează munca POST-TUR (best-effort): cache write-back + summarizer + profil/facts,
    într-un acumulator de usage propriu (al doilea `llm_usage`, phase=post_turn, ca fundalul să NU
    scape rollup-ului). `db` = PROVIDER: inline (static_db) = vechi; deferred (tenant_db) = conn
    eliberat pe durata LLM. Un eșec într-un helper e prins de el (best-effort) — nu afectează
    reply/outbox/`mark_inbound_completed` (deja commise)."""
    post_acc, post_token = usage.push()
    try:
        await _cache_writeback(
            db, work.llm, work.business.id, work.language, work.ctx.message.body, work.ctx
        )
        await _summarize_if_needed(
            db, redis, work.business.id, work.conversation_id, work.ctx, work.llm
        )
        await _extract_profile_and_score(
            db,
            redis,
            work.ctx,
            work.llm,
            shadow_mode=work.shadow_mode,
            source_message_id=work.inbound_msg_id,
        )
    except Exception:  # noqa: BLE001 — backstop: helperele prind deja, dar aftercare NU are voie
        log.exception("aftercare a eșuat (turul continuă)")  # să propage (reply e deja commis)
    finally:
        usage.pop(post_token)
    if post_acc.calls:
        # NX-122: prin ctx.emit → turn_id atașat (corelează costul post-tur cu turul).
        work.ctx.emit("llm_usage", **_usage_event_props(post_acc, phase="post_turn"))
        # best-effort: eșecul de CHECKOUT sau de insert NU rupe turul (review Codex #208) — pe web
        # sync ar da 500 după ce reply-ul a fost deja calculat/livrat.
        try:
            async with db() as conn:
                await _persist_events(
                    conn,
                    work.business.id,
                    work.conversation_id,
                    work.contact_id,
                    [work.ctx.events[-1]],
                )
        except Exception:  # noqa: BLE001 — persistarea llm_usage e best-effort
            log.exception("persistarea llm_usage post-tur a eșuat (turul continuă)")
