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

from src.agent.llm import get_llm
from src.channels.base import IDENTIFIED_CHANNELS
from src.channels.media import get_media_registry
from src.config import get_settings
from src.db.connection import bot_pool_stats
from src.db.pool_metrics import take_acquire_wait
from src.db.provider import static_db, tenant_db
from src.db.queries.contacts import get_or_create_contact
from src.db.queries.conversations import (
    get_or_create_conversation,
    patch_conversation_state,
    touch_last_inbound,
)
from src.db.queries.facts import (
    fetch_relevant_facts,
)
from src.db.queries.inbound_dedupe import claim_inbound, mark_inbound_completed
from src.db.queries.messages import (
    get_recent_messages,
    insert_message,
)
from src.db.queries.outbox import enqueue_outbox
from src.db.queries.summaries import (
    get_summary_for_context,
)
from src.models import (
    Author,
    BusinessConfig,
    ConversationState,
    Direction,
    InboundMessage,
    Reply,
    TurnContext,
    TurnUsage,
)
from src.worker.aftercare import AftercareWork, _persist_events, run_aftercare
from src.worker.compose import ensure_disclaimer
from src.worker.limits import (
    CONTACT_COST_WINDOW_S,
    contact_scope_key,
    cost_add_and_total,
    cost_over_budget,
    seed_daily_cost,
    spend_capped,
    spend_over_cap,
)
from src.worker.reply_split import split_reply
from src.worker.runner import DEFAULT_STAGES, PipelineDeps, Stage, run_pipeline

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
    # NX-161 F1: cu `defer_aftercare=True`, handle_turn NU rulează aftercare inline — întoarce
    # AICI munca de făcut, iar apelantul o rulează cu `run_aftercare(tenant_db(biz), ...)` DUPĂ ce a
    # închis `tenant_conn` (conn eliberat pe durata LLM). None = aftercare deja rulat inline.
    aftercare: "AftercareWork | None" = None


async def _llm_within_budget(
    ctx: TurnContext, redis: Redis | None, business: BusinessConfig, *, channel_kind: str
):
    """Cost guard (G2c): dacă businessul SAU contactul a depășit plafonul, întoarce None →
    pipeline-ul rulează FĂRĂ LLM (triaj/agent degradează grațios, cache L1 încă servește), dar
    gates rămân intacte. Best-effort: eșec Redis → fail-open (LLM normal). Plafoane:
    businesses.daily_cost_cap_usd | settings.daily_cost_cap_usd (business) și
    settings.contact_daily_cost_cap_usd (per-contact, canale identificate — NX-125)."""
    llm = get_llm()
    settings = get_settings()
    if llm is None or redis is None or not settings.cost_guard_enabled:
        return llm
    cap = business.daily_cost_cap_usd or settings.daily_cost_cap_usd
    contact_cap = settings.contact_daily_cost_cap_usd
    if not cap and not contact_cap:
        return llm
    try:
        if cap and await cost_over_budget(redis, business.id, cap):
            ctx.emit("cost_guard_tripped", cap_usd=cap)
            log.warning(
                "cost guard: business %s peste plafon ($%.2f) → LLM dezactivat", business.id, cap
            )
            return None
        # NX-125: plafon SOFT per-contact (canale identificate; web = NX-120). Pre-check read-only.
        if contact_cap and channel_kind in IDENTIFIED_CHANNELS:
            scope = contact_scope_key(business.id, ctx.contact.id)
            if await spend_capped(redis, scope, contact_cap):
                ctx.emit("contact_spend_capped", cap_usd=contact_cap)
                log.warning(
                    "cost guard: contact peste plafon per-contact ($%.4f) → LLM off", contact_cap
                )
                return None
    except Exception as e:  # noqa: BLE001 — check eșuat → fail-open
        log.warning("cost guard: check eșuat (%s) → fail-open", type(e).__name__)
    return llm


async def _record_turn_cost(
    redis: Redis | None,
    business: BusinessConfig,
    ctx: TurnContext,
    *,
    llm_used: bool,
    channel_kind: str,
) -> None:
    """Adaugă costul EXACT al turului în contorul zilnic (G2c) — DOAR dacă LLM-ul a fost folosit.
    NX-125: costul vine din tokeni reali (`ctx.usage.cost_usd`, cifra dashboard/usage_daily), nu
    din euristica `estimate_turn_cost`. Increment ATOMIC + enforcement POST-increment (fără TOCTOU):
    dacă noul total ≥ plafon, emite `cost_guard_tripped` → turul URMĂTOR e blocat de pre-check.
    Aplică și plafonul per-contact (canale identificate). Best-effort; facturare = usage_daily."""
    settings = get_settings()
    if redis is None or not settings.cost_guard_enabled or not llm_used:
        return
    cost = ctx.usage.cost_usd if ctx.usage else 0.0
    if cost <= 0:  # tur fără apeluri LLM (cache L1/gates) → nimic de contorizat
        return
    try:
        total = await cost_add_and_total(redis, business.id, cost)
        cap = business.daily_cost_cap_usd or settings.daily_cost_cap_usd
        if cap and total >= cap:
            ctx.emit("cost_guard_tripped", cap_usd=cap, total_usd=round(total, 6))
    except Exception as e:  # noqa: BLE001 — contor best-effort, turul a răspuns deja
        log.warning("cost guard: add eșuat (%s)", type(e).__name__)
    # NX-125: plafon per-contact (canale identificate; web = NX-120). Increment atomic + compară.
    contact_cap = settings.contact_daily_cost_cap_usd
    if contact_cap and channel_kind in IDENTIFIED_CHANNELS:
        try:
            scope = contact_scope_key(business.id, ctx.contact.id)
            if await spend_over_cap(redis, scope, cost, contact_cap, CONTACT_COST_WINDOW_S):
                ctx.emit("contact_spend_capped", cap_usd=contact_cap)
        except Exception as e:  # noqa: BLE001 — fail-open pe scope
            log.warning("cost guard: spend per-contact eșuat (%s)", type(e).__name__)


def _message_usage_kwargs(turn_usage: TurnUsage | None) -> dict:
    """Câmpurile de observabilitate (NX-103) atașate pe rândul `messages` outbound al botului:
    tokeni/cost/latență/model. DOAR pe primul fragment al reply-ului (split-ul e același reply).

    CONV-COMMERCE: salvăm pentru ORICE tur (nu doar cele cu LLM) — timpul + tokenii + costul intră
    în DB la FIECARE răspuns. Tokeni/cost = 0 când n-a fost apel LLM (cache/free-layer/welcome —
    corect 0, nu NULL); `model_route` = modelele folosite sau None. `None` = niciun tur prin runner
    (ex. mesaj proactiv pus direct în outbox)."""
    if turn_usage is None:
        return {}
    return {
        "model_route": ",".join(turn_usage.models) or None,
        "tokens_in": turn_usage.tokens_in,
        "tokens_out": turn_usage.tokens_out,
        "cost_usd": round(turn_usage.cost_usd, 6),
        "latency_ms": int(round(turn_usage.latency_ms)),
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
    defer_aftercare: bool = False,
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
    # NX-129: login passthrough — dacă marginea de canal a verificat o identitate stabilă
    # (`customer_ref` din JWT host-signed), rezolvăm contactul pe EA (verified=true → contact stabil
    # peste sesiuni/device-uri), nu pe visitor_id-ul anonim. Absent → comportament anonim (ca azi).
    verified_customer_ref = event.get("verified_customer_ref")
    identity_external_id = verified_customer_ref or sender_external_id

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
        identity_external_id,
        display_name=event.get("sender_name"),
        verified=bool(verified_customer_ref),
    )
    conv = await get_or_create_conversation(
        conn,
        business.id,
        contact.id,
        channel_id,
        locale=business.default_locale,
    )

    inbound_msg_id = await insert_message(
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
        # NX-146 felia 2 (fix): turn_id în payload → Turn Replay citește EXACT mesajele turului,
        # nu o euristică „ultimele N ale conversației" (greșită dacă discuția a continuat).
        # fragment_index=0: un singur mesaj inbound per tur, dar `get_turn_messages` ordonează
        # uniform pe (direction, fragment_index) — vezi fix-ul de mai jos pe outbound.
        payload={"turn_id": turn_id, "fragment_index": 0},
    )
    await touch_last_inbound(conn, business.id, conv["id"])

    # NX-148: memoria structurată (facts) încărcată o dată, injectată de facts_block. Guardată de
    # kill-switch (OFF → gol → bloc absent). BEST-EFFORT (ca summary/cache): un fail de citire nu
    # blochează turul — degradare la history+state (P6). Owner: processor.
    facts: list = []
    _s = get_settings()
    # NX-160: injectăm doar dacă memoria E ON ȘI injectarea safe e permisă (flag separat: facts se
    # pot persista fără a fi injectate). fetch_relevant_facts întoarce DOAR visibility='inject'.
    if _s.conversation_facts_enabled and _s.memory_safe_injection_enabled:
        try:
            facts = await fetch_relevant_facts(conn, business.id, contact.id)
        except Exception:  # noqa: BLE001 — memoria e opțională, turul răspunde oricum
            log.exception("încărcarea facts a eșuat (turul continuă)")

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
        facts=facts,  # NX-148: memorie structurată (facts_block)
        language=conv["locale"] or business.default_locale,
        bot_active=conv["bot_active"],
        handoff_until=conv["handoff_until"],
        verified_customer_ref=verified_customer_ref,  # NX-129: login passthrough (None = anonim)
    )

    # NX-148: acoperirea memoriei (chei/contoare, nu valori — P12).
    if facts:
        ctx.emit("facts_injected", n_injected=len(facts))

    # Felia 0A (NX-161): semnalul de WAIT (acquire-wait al checkout-ului, din tenant_conn prin
    # ContextVar) + ocuparea pool-ului (in_use/idle/inflight), corelat pe tur. Declanșatorul
    # deciziei de conn-per-op (docs/CONN-HOLD-ANALYSIS-2026.md §Faza 0A). P10/P12: fără PII.
    if _s.pool_metrics_enabled:
        ctx.emit("pool_metrics", acquire_wait_ms=take_acquire_wait(), **bot_pool_stats())

    # NX-129: observabilitate login passthrough (P12: fără PII — doar succes/motiv, nu valoarea).
    if verified_customer_ref:
        ctx.emit("web_identity_verified")
    else:
        reject = (event.get("payload") or {}).get("identity_rejected")
        if reject:
            ctx.emit("web_identity_rejected", reason=reject)

    # Cost guard (G2c): peste plafonul zilnic → llm=None (degradare). Gates rulează oricum.
    # NX-125: reseed LAZY al contorului zilei din usage_daily (supraviețuiește pierderii Redis),
    # ÎNAINTE de pre-check; santinelă internă → o singură dată/zi, best-effort.
    if redis is not None and get_settings().cost_guard_enabled:
        await seed_daily_cost(conn, redis, business.id)
    llm = await _llm_within_budget(ctx, redis, business, channel_kind=channel_kind)
    # Media routing (NX-76): registry de fetchers (singleton, ca llm) → gate-ul descarcă poza.
    media = get_media_registry()
    # NX-161 Felia 0B: providerul tenant-scoped e disponibil pentru stagii (deps.db()), dar în 0B
    # NICIUN stagiu nu-l apelează încă → stagiile folosesc `conn` (viu, ca înainte) = zero schimbare
    # de runtime. Feliile următoare migrează gradual la deps.db(). AMBELE trecute intenționat:
    # __post_init__ NU suprascrie `db` explicit cu static(conn).
    deps = PipelineDeps(conn=conn, db=tenant_db(business.id), redis=redis, llm=llm, media=media)
    await run_pipeline(ctx, deps, stages)
    await _persist_events(conn, business.id, conv["id"], contact.id, ctx.events)
    await _record_turn_cost(
        redis, business, ctx, llm_used=llm is not None, channel_kind=channel_kind
    )

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
        return TurnResult(conv["id"], contact.id, turn_id, None, None, language=ctx.language)

    # Sender (P5): garantează disclaimer-ul AI (art. 50) pe text, o singură dată, pt TOATE
    # rutele (simple/clarify/prose/fallback/cache). Idempotent — rich/welcome și-l aplatizează
    # deja → nu dublează. `ctx.reply.text` rămâne PUR (owner = stagiul; cache-ul îl stochează fără
    # disclaimer și-l re-aplică la hit). NX-134.
    reply_text = ensure_disclaimer(ctx.reply.text, ctx.language)  # self-gated

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
        # NX-112 (P3: processor = singurul scriitor explicit): merge canonic din ctx.state pentru
        # câmpurile pe care stagiile le mută IN-PLACE (clarify umple constraints; clarify scrie
        # asked_intents). Fără asta, un slot NOU (ex. „buget 200") trăiește doar pe ctx.state
        # (dict detașat de from_jsonb) și se pierde silențios la write-back → botul „uită".
        # `cart` rămâne owned de Agent prin state_patch (ctx.state.cart NU e ținut sincron de
        # cart_add) → NU îl includem aici; persistă via conv["state"] brut + state_patch mai jos.
        new_state = {
            **new_state,
            "constraints": ctx.state.constraints,
            "asked_intents": ctx.state.asked_intents,
            # NX-133: stiva de constrângeri de căutare (mutată in-place de agent) — la fel ca
            # `constraints`, trebuie merge-uită canonic aici, altfel se pierde la write-back.
            "search_constraints": ctx.state.search_constraints,
        }
        # NX-119b: resetează sesiunea de căutare dacă reply-ul NU e o căutare de produse (fără
        # sesiuni zombi — un „mai arată-mi" ulterior nu trebuie să reia o sesiune veche, fără
        # legătură). Dacă tool-ul/agentul a scris `active_search` în state_patch (căutare nouă sau
        # pagină), acela are întâietate prin merge-ul de mai jos.
        if not (is_rich or has_products):
            new_state = {**new_state, "active_search": None}
        # NX-79: mutații de state cerute de tool-uri (ex. cart_add → {"cart": [...]}), acumulate
        # în stagiul Agent. Owner unic = Agent; processor-ul doar le merge-uiește la scriere (P3).
        # Rămâne ULTIMUL → state_patch (Agent) are întâietate peste merge-ul canonic.
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
                # NX-146 felia 2 (fix, corectat pe finding Codex): turn_id + fragment_index în
                # payload. Fragmentele outbound se scriu în ACEEAȘI tranzacție → `created_at` poate
                # fi identic (now() e constant per tranzacție) → `created_at asc` NU garantează
                # ordinea. `fragment_index` = poziția reală a fragmentului (split NX-90, max 2).
                payload={"turn_id": turn_id, "fragment_index": i},
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
                "language": ctx.language,  # NX-127: randorul de canal re-aplică disclaimer/locale
            }
            # Extras-urile bogate stau pe PRIMUL fragment (rich/carusel = un fragment oricum):
            # `type=text` rămâne (allow-list); canalele cu send_rich/carousel randează bogat.
            if i == 0 and is_rich:
                payload["rich"] = asdict(ctx.reply.rich)
            elif i == 0 and ctx.reply.comparison is not None:
                # IZI-compare: tabelul structurat + cardurile produselor comparate. `type` rămâne
                # 'text' (floor = tabelul aplatizat pe canalele fără COMPARISON); web rutează pe
                # send_rich după payload['comparison']. reply_from_outbox îl reconstruiește.
                payload["comparison"] = asdict(ctx.reply.comparison)
                if ctx.reply.products:
                    payload["products"] = ctx.reply.products
            elif i == 0 and has_products:
                payload["type"] = "carousel"
                payload["products"] = ctx.reply.products
            # NX-127: offer neutru (NX-114) pe primul fragment → randat NATIV (buton web / CTA),
            # nu doar floor-uit în text. reply_from_outbox îl reconstruiește pe ruta async.
            if i == 0 and ctx.reply.offer is not None:
                payload["offer"] = asdict(ctx.reply.offer)
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
        # NX-122: prin ctx.emit → primește turn_id (parte din traiectoria aceluiași tur).
        ctx.emit("reply_split", parts=len(fragments))
        await _persist_events(conn, business.id, conv["id"], contact.id, [ctx.events[-1]])

    # Log per-tur la succes (fără PII: doar id-uri + lungimea reply-ului, nu corpul).
    log.info(
        "tur procesat: conv=%s turn=%s reply=%dch outbox=%s",
        conv["id"],
        turn_id,
        len(reply_text),
        outbox_id,
    )
    # POST-TUR (cache write-back + summarizer + profil/facts) — best-effort, DUPĂ outbox, nu
    # întârzie livrarea. NX-161 F1: mutat în `run_aftercare(db_provider, ...)`. INLINE (static_db —
    # calea sync/teste/sim) = comportament vechi (conn viu). DEFERRED (producție, `defer_aftercare`)
    # → apelantul îl rulează cu `tenant_db(biz)` DUPĂ ce închide `tenant_conn` → conn ELIBERAT pe
    # durata LLM-ului de fundal. Un eșec NU afectează reply/outbox/completed (deja commise).
    work = AftercareWork(
        business=business,
        conversation_id=conv["id"],
        contact_id=contact.id,
        ctx=ctx,
        inbound_msg_id=inbound_msg_id,
        shadow_mode=bool(conv.get("shadow_mode")),
        llm=llm,
        language=ctx.language,
    )
    if not defer_aftercare:
        await run_aftercare(static_db(conn), redis, work)
        work = None  # rulat inline → nimic de întors apelantului
    return TurnResult(
        conv["id"],
        contact.id,
        turn_id,
        ctx.reply.text,
        outbox_id,
        reply=ctx.reply,  # sync: apelantul mapează text+produse+chips în răspunsul HTTP
        language=ctx.language,
        aftercare=work,  # deferred → apelantul rulează run_aftercare; inline → None
    )
