"""Consumer Redis Streams — citește inbound, rezolvă tenantul, rulează turul.

Bucla operațională a stagiului 2: consumer group pe stream-ul `inbound`, fiecare
mesaj → rezolvare canal→business (control plane) → conexiune tenant-scoped →
`handle_turn`. ACK pe stream DUPĂ procesare (sau eșec logat) ca un mesaj să nu
se piardă tăcut (principiul 6); un mesaj care crapă procesarea e tot ACK-uit ca
să nu blocheze coada, dar e logat pentru investigație.

Hardening livrat peste schelet:
  • debounce adaptiv (R1) + ACK-after-flush durabil (NX-87)
  • lock per conversație pentru >1 replică (NX-85) — re-queue cu backoff la contenție
  • dedupe layer 1 (Redis, webhook) + layer 2 durabil în DB (NX-51) + claim-or-resume (NX-86)
  • reaper XAUTOCLAIM pentru mesaje „stuck" de la un consumer mort (NX-86)
"""

import asyncio
import json
import logging
import socket
import time
from uuid import uuid4

import httpx
from redis.asyncio import Redis
from redis.exceptions import ResponseError

from src.channels.base import Capability, ChannelSenderRegistry
from src.channels.media import close_media
from src.config import get_settings
from src.db.connection import admin_conn, close_pool, get_bot_pool, get_pool, tenant_conn
from src.db.queries.businesses import load_business
from src.db.queries.channels import resolve_channel
from src.db.queries.message_status import record_status_event
from src.redis_bus import (
    STREAM_INBOUND,
    acquire_conv_lock,
    close_redis,
    enqueue_inbound,
    get_redis,
    release_conv_lock,
)
from src.webhook.orders import process_order
from src.worker.callback import handle_callback
from src.worker.debounce import Debouncer
from src.worker.dispatcher import build_registry
from src.worker.processor import handle_turn

log = logging.getLogger(__name__)

CONSUMER_GROUP = "workers"

# Reaper PEL (NX-86): un consumer care n-a ACK-uit în `REAP_MIN_IDLE_MS` = probabil mort → îi
# reclamăm intrările cu XAUTOCLAIM și le reprocesăm. Rulat periodic (`REAP_INTERVAL_S`), nu mereu.
REAP_MIN_IDLE_MS = 60_000
REAP_BATCH = 50
REAP_INTERVAL_S = 30.0


async def _safe_typing(registry: ChannelSenderRegistry | None, event: dict) -> None:
    """Trimite „typing/read" pentru un mesaj inbound, INSTANT și best-effort (NX-90). Direct prin
    ChannelSender (NU outbox: un typing întârziat/retry-uit e inutil). Canal fără capabilitatea
    TYPING → skip tăcut. Orice eroare → log fără PII, turul NU se rupe (P6). Argumentele
    (channel_account_id receptor + sender_external_id + provider_msg_id) vin din envelope."""
    if registry is None:
        return
    # NX-115: FĂRĂ default „whatsapp" (un canal necunoscut ar fi trimis typing greșit pe WhatsApp);
    # gardă pe capabilitatea TYPING declarată, nu pe `hasattr(mark_typing)`.
    sender = registry.get(event.get("channel_kind"))
    if sender is None or Capability.TYPING not in getattr(sender, "capabilities", frozenset()):
        return
    try:
        await sender.mark_typing(
            event.get("channel_account_id", ""),
            event.get("sender_external_id", ""),
            event.get("provider_msg_id"),
        )
    except Exception as e:  # noqa: BLE001 — typing eșuat NU rupe turul (P6)
        log.warning("typing eșuat (%s) — ignorat", type(e).__name__)


async def ensure_group(redis: Redis) -> None:
    """Creează consumer group-ul (idempotent, cu MKSTREAM)."""
    try:
        await redis.xgroup_create(STREAM_INBOUND, CONSUMER_GROUP, id="0", mkstream=True)
    except ResponseError as e:
        if "BUSYGROUP" not in str(e):
            raise


async def _requeue_busy(redis: Redis, event: dict, settings) -> str:
    """Conversație ocupată de altă replică (NX-85) → re-pune evenimentul pe stream cu backoff scurt
    (altă replică îl ia după ce eliberează lock-ul). Cap dur de reîncercări → drop (nu rebuclează la
    infinit pe o conversație blocată). Întoarce un status pt log (fără PII)."""
    n = int(event.get("_requeues", 0)) + 1
    if n > settings.conv_lock_max_requeues:
        return f"dropped după {n} reîncercări"
    await asyncio.sleep(settings.conv_lock_requeue_delay_ms / 1000)
    await enqueue_inbound(redis, {**event, "_requeues": n})
    return f"requeue n={n}"


async def process_event(pool, redis: Redis, event: dict) -> None:
    """Rezolvă tenantul și rutează evenimentul după `kind` (message | status).

    `channel_kind` (whatsapp|telegram|...) = transportul; `kind` (message|status)
    = tipul de envelope. Rezolvarea tenantului e comună tuturor canalelor."""
    # Comenzile (F2-2) nu-s evenimente de canal: `business_id` vine din envelope (autentificat
    # de secret la webhook), nu din rezolvarea pe canal. Rutăm ÎNAINTE de resolve_channel.
    if event.get("kind") == "order":
        business_id = event.get("business_id")
        if not business_id:
            log.warning("order fără business_id — ignorat")
            return
        async with tenant_conn(business_id) as conn:
            try:
                await process_order(conn, business_id, event.get("order") or {})
            except Exception:  # noqa: BLE001 — un order rău nu blochează coada (e logat)
                log.exception("procesarea comenzii a eșuat (business=%s)", business_id)
        return

    channel_kind = event.get("channel_kind", "whatsapp")
    channel_account_id = event.get("channel_account_id", "")
    async with admin_conn(pool) as conn:
        channel = await resolve_channel(conn, channel_kind, channel_account_id)
    if channel is None:
        log.warning(
            "canal necunoscut: kind=%s account_id=%s — ignorat",
            channel_kind,
            channel_account_id,
        )
        return

    business_id = channel["business_id"]
    kind = event.get("kind", "message")

    # Statusurile sunt idempotente (delivered/read/failed pe provider_msg_id) → fără lock.
    if kind == "status":
        async with tenant_conn(business_id) as conn:
            await record_status_event(
                conn,
                business_id,
                event["provider_msg_id"],
                event["status"],
                payload=event.get("payload"),
            )
        return

    # NX-85: lock per conversație — serializează tururile aceleiași conversații între REPLICI.
    # Mesaj/callback mutează state-ul; cheia = business + expeditor (PII de canal → DOAR în cheie,
    # efemeră cu TTL; nu în loguri). Ocupat → re-queue cu backoff. Redis jos/off → fail-open.
    settings = get_settings()
    sender_key = f"{channel_kind}:{channel_account_id}:{event.get('sender_external_id', '')}"
    token = uuid4().hex
    locked: bool | None = (
        None  # None = fără lock (dezactivat/fail-open); True = deținut; False = ocupat
    )
    if settings.conv_lock_enabled and redis is not None:
        try:
            got = await acquire_conv_lock(
                redis, business_id, sender_key, token, ttl_s=settings.conv_lock_ttl_seconds
            )
            locked = bool(got)
        except Exception as e:  # noqa: BLE001 — Redis jos → fail-open (nu blocăm traficul)
            log.warning("conv lock: acquire eșuat (%s) — fail-open", type(e).__name__)
            locked = None
        if locked is False:  # altă replică procesează aceeași conversație → re-queue
            ctx_emit_busy = await _requeue_busy(redis, event, settings)
            log.info("conv lock: ocupat → re-queue (business=%s, %s)", business_id, ctx_emit_busy)
            return

    try:
        async with tenant_conn(business_id) as conn:
            business = await load_business(conn, business_id)
            if business is None:
                log.warning("business %s lipsește — ignorat", business_id)
                return
            if kind == "callback":
                # navigare carusel (R2): drum determinist, NU pipeline LLM.
                await handle_callback(conn, business, channel["channel_id"], event)
                return
            await handle_turn(conn, business, channel["channel_id"], event, redis=redis)
    finally:
        if locked is True:
            await release_conv_lock(redis, business_id, sender_key, token)


async def consume_once(
    pool,
    redis: Redis,
    consumer_name: str,
    debouncer: Debouncer,
    registry: ChannelSenderRegistry | None = None,
    *,
    block_ms: int = 2000,
) -> int:
    """Un ciclu de citire+procesare. Mesajele trec prin debounce (lot per expeditor);
    statusurile se procesează imediat. Întoarce numărul de evenimente tratate."""
    resp = await redis.xreadgroup(
        CONSUMER_GROUP,
        consumer_name,
        {STREAM_INBOUND: ">"},
        count=10,
        block=block_ms,
    )
    if not resp:
        return 0

    handled = 0
    for _stream, entries in resp:
        for msg_id, fields in entries:
            handled += 1
            try:
                event = json.loads(fields["data"])
            except Exception:  # noqa: BLE001 — payload nevalid → ACK + skip (nu blochează coada)
                log.exception("mesaj inbound nevalid %s — ACK + skip", msg_id)
                await redis.xack(STREAM_INBOUND, CONSUMER_GROUP, msg_id)
                continue
            try:
                if event.get("kind", "message") == "message":
                    # NX-90: „typing…" INSTANT la primire, înainte de debounce (fire-and-forget).
                    if get_settings().typing_enabled:
                        asyncio.create_task(_safe_typing(registry, event))
                    # NX-87: ACK delegat Debouncer-ului DUPĂ flush reușit (durabilitate) — NU aici.
                    await debouncer.add(event, msg_id)
                else:
                    await process_event(pool, redis, event)  # status/callback/order → imediat
                    await redis.xack(STREAM_INBOUND, CONSUMER_GROUP, msg_id)
            except Exception:  # noqa: BLE001 — eroare → ACK (mesajul e logat, nu blochează coada)
                log.exception("eroare la procesarea mesajului %s", msg_id)
                await redis.xack(STREAM_INBOUND, CONSUMER_GROUP, msg_id)
    return handled


async def reap_pending(pool, redis: Redis, consumer_name: str, debouncer: Debouncer) -> int:
    """XAUTOCLAIM (NX-86): reclamă intrările PEL rămase de la un consumer MORT (citite cu
    XREADGROUP dar ne-ACK-uite > `min_idle`) și le reprocesează pe ACEEAȘI cale (mesaje → debounce
    cu ACK-after-flush; restul → imediat + ACK). Intrări șterse între timp (`fields` None) →
    ACK-only. Închide gaura „consumer mort între citire și ACK". Best-effort: orice eroare e
    logată, nu oprește bucla. Întoarce câte intrări a tratat."""
    try:
        resp = await redis.xautoclaim(
            STREAM_INBOUND,
            CONSUMER_GROUP,
            consumer_name,
            min_idle_time=REAP_MIN_IDLE_MS,
            start_id="0-0",
            count=REAP_BATCH,
        )
    except ResponseError as e:
        log.warning("reaper: XAUTOCLAIM eșuat (%s) — sărit", type(e).__name__)
        return 0
    # redis-py: [cursor, messages, deleted] (sau [cursor, messages] pe versiuni mai vechi).
    messages = resp[1] if len(resp) > 1 else []
    handled = 0
    for msg_id, fields in messages:
        handled += 1
        if not fields:  # intrare ștearsă din stream între claim și reap → doar ACK
            await redis.xack(STREAM_INBOUND, CONSUMER_GROUP, msg_id)
            continue
        try:
            event = json.loads(fields["data"])
            if event.get("kind", "message") == "message":
                await debouncer.add(event, msg_id)  # ACK delegat Debouncer-ului (NX-87)
            else:
                await process_event(pool, redis, event)
                await redis.xack(STREAM_INBOUND, CONSUMER_GROUP, msg_id)
        except Exception:  # noqa: BLE001 — o intrare stricată nu oprește reaper-ul
            log.exception("reaper: reprocesare eșuată %s — ACK", msg_id)
            await redis.xack(STREAM_INBOUND, CONSUMER_GROUP, msg_id)
    if handled:
        log.info("reaper: %d intrări PEL reclamate de la consumeri morți", handled)
    return handled


async def run_consumer(
    pool,
    redis: Redis,
    consumer_name: str,
    registry: ChannelSenderRegistry | None = None,
    *,
    reap_interval_s: float = REAP_INTERVAL_S,
) -> None:
    """Bucla principală a worker-ului (rulează până la anulare). `registry` (NX-90) = sender-ele
    de canal pt typing instant; None → fără typing. Rulează periodic reaper-ul PEL (NX-86)."""
    await ensure_group(redis)

    async def _handle(event: dict) -> None:
        await process_event(pool, redis, event)

    async def _ack_batch(msg_ids: list[str]) -> None:
        # NX-87: ACK lotul de mesaje DUPĂ flush reușit (durabilitate — nu la citire).
        if msg_ids:
            await redis.xack(STREAM_INBOUND, CONSUMER_GROUP, *msg_ids)

    debouncer = Debouncer(_handle, ack=_ack_batch)
    log.info("consumer %s pornit pe stream %s", consumer_name, STREAM_INBOUND)
    last_reap = time.monotonic()
    while True:
        await consume_once(pool, redis, consumer_name, debouncer, registry)
        now = time.monotonic()
        if now - last_reap >= reap_interval_s:
            last_reap = now
            await reap_pending(pool, redis, consumer_name, debouncer)


async def _main() -> None:
    """Entrypoint proces worker: `python -m src.worker.consumer`.

    Numele de consumer = hostname-ul (în container = id-ul containerului) → unic
    per replică, ca XREADGROUP să distribuie mesajele corect între workeri."""
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # nu loga URL-uri cu token
    # NX-123: poarta de boot trăiește în runner-ul de migrări (scripts/migrate.py); import local
    # ca să nu cuplăm src→scripts la încărcarea modulului (și calea e sensibilă la sys.path).
    from scripts.migrate import assert_migrations_current

    pool = await get_pool()  # admin (control plane: resolve_channel)
    # NX-123 (P6): poartă de boot — workerul NU pornește tăcut peste o schemă incompletă.
    # Migrare pending = boot refuzat cu eroare explicită (regresia 010/012 care crăpa primul
    # mesaj al fiecărui client nou), nu un crash la primul inbound.
    await assert_migrations_current(pool)
    await get_bot_pool()  # eager: parolă bot_runtime greșită → crapă la boot, nu la primul mesaj
    redis = await get_redis()
    consumer_name = f"worker-{socket.gethostname()}"
    # NX-90: client httpx + registru de sender-e pentru typing instant (reutilizăm build_registry
    # din dispatcher). Canalele fără credențiale nu se înregistrează → typing-ul lor e skip tăcut.
    async with httpx.AsyncClient(timeout=15.0) as http:
        registry = build_registry(http, get_settings())
        try:
            await run_consumer(pool, redis, consumer_name, registry)
        finally:
            await close_media()  # închide httpx-ul de download media (NX-76)
            await close_redis()
            await close_pool()


if __name__ == "__main__":
    asyncio.run(_main())
