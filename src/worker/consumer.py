"""Consumer Redis Streams — citește inbound, rezolvă tenantul, rulează turul.

Bucla operațională a stagiului 2: consumer group pe stream-ul `inbound`, fiecare
mesaj → rezolvare canal→business (control plane) → conexiune tenant-scoped →
`handle_turn`. ACK pe stream DUPĂ procesare (sau eșec logat) ca un mesaj să nu
se piardă tăcut (principiul 6); un mesaj care crapă procesarea e tot ACK-uit ca
să nu blocheze coada, dar e logat pentru investigație.

Defer (follow-up, peste acest schelet):
  • debounce adaptiv 2-3s (lot de mesaje, nu string lipit)
  • lock per conversație pentru >1 consumer (acum: ordinea ține pe un consumer)
  • dedupe layer 2 durabil în DB (NX-51) — acum avem layer 1 (Redis) din webhook
  • claim mesaje „stuck" via XAUTOCLAIM (consumer mort)
"""

import asyncio
import json
import logging
import socket

import httpx
from redis.asyncio import Redis
from redis.exceptions import ResponseError

from src.channels.base import ChannelSenderRegistry
from src.channels.media import close_media
from src.config import get_settings
from src.db.connection import admin_conn, close_pool, get_bot_pool, get_pool, tenant_conn
from src.db.queries.businesses import load_business
from src.db.queries.channels import resolve_channel
from src.db.queries.message_status import record_status_event
from src.redis_bus import STREAM_INBOUND, close_redis, get_redis
from src.webhook.orders import process_order
from src.worker.callback import handle_callback
from src.worker.debounce import Debouncer
from src.worker.dispatcher import build_registry
from src.worker.processor import handle_turn

log = logging.getLogger(__name__)

CONSUMER_GROUP = "workers"


async def _safe_typing(registry: ChannelSenderRegistry | None, event: dict) -> None:
    """Trimite „typing/read" pentru un mesaj inbound, INSTANT și best-effort (NX-90). Direct prin
    ChannelSender (NU outbox: un typing întârziat/retry-uit e inutil). Canal fără `mark_typing`
    (hasattr) → skip tăcut. Orice eroare → log fără PII, turul NU se rupe (P6). Argumentele
    (channel_account_id receptor + sender_external_id + provider_msg_id) vin din envelope."""
    if registry is None:
        return
    sender = registry.get(event.get("channel_kind", "whatsapp"))
    if sender is None or not hasattr(sender, "mark_typing"):
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
    async with tenant_conn(business_id) as conn:
        if kind == "status":
            await record_status_event(
                conn,
                business_id,
                event["provider_msg_id"],
                event["status"],
                payload=event.get("payload"),
            )
            return
        business = await load_business(conn, business_id)
        if business is None:
            log.warning("business %s lipsește — ignorat", business_id)
            return
        if kind == "callback":
            # navigare carusel (R2): drum determinist, NU pipeline LLM.
            await handle_callback(conn, business, channel["channel_id"], event)
            return
        await handle_turn(conn, business, channel["channel_id"], event, redis=redis)


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


async def run_consumer(
    pool, redis: Redis, consumer_name: str, registry: ChannelSenderRegistry | None = None
) -> None:
    """Bucla principală a worker-ului (rulează până la anulare). `registry` (NX-90) = sender-ele
    de canal pentru typing-ul instant pe inbound; None → fără typing (compat dev/test)."""
    await ensure_group(redis)

    async def _handle(event: dict) -> None:
        await process_event(pool, redis, event)

    async def _ack_batch(msg_ids: list[str]) -> None:
        # NX-87: ACK lotul de mesaje DUPĂ flush reușit (durabilitate — nu la citire).
        if msg_ids:
            await redis.xack(STREAM_INBOUND, CONSUMER_GROUP, *msg_ids)

    debouncer = Debouncer(_handle, ack=_ack_batch)
    log.info("consumer %s pornit pe stream %s", consumer_name, STREAM_INBOUND)
    while True:
        await consume_once(pool, redis, consumer_name, debouncer, registry)


async def _main() -> None:
    """Entrypoint proces worker: `python -m src.worker.consumer`.

    Numele de consumer = hostname-ul (în container = id-ul containerului) → unic
    per replică, ca XREADGROUP să distribuie mesajele corect între workeri."""
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # nu loga URL-uri cu token
    pool = await get_pool()  # admin (control plane: resolve_channel)
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
