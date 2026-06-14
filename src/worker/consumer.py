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

from redis.asyncio import Redis
from redis.exceptions import ResponseError

from src.db.connection import admin_conn, close_pool, get_pool, tenant_conn
from src.db.queries.businesses import load_business
from src.db.queries.channels import resolve_channel
from src.db.queries.message_status import record_status_event
from src.redis_bus import STREAM_INBOUND, close_redis, get_redis
from src.worker.debounce import Debouncer
from src.worker.processor import handle_turn

log = logging.getLogger(__name__)

CONSUMER_GROUP = "workers"


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
    async with tenant_conn(pool, business_id) as conn:
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
        await handle_turn(conn, business, channel["channel_id"], event, redis=redis)


async def consume_once(
    pool, redis: Redis, consumer_name: str, debouncer: Debouncer, *, block_ms: int = 2000
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
            try:
                event = json.loads(fields["data"])
                if event.get("kind", "message") == "message":
                    await debouncer.add(event)  # coalesce + flush async (R1)
                else:
                    await process_event(pool, redis, event)  # status → imediat
            except Exception:  # noqa: BLE001 — bucla nu trebuie să moară pe un mesaj
                log.exception("eroare la procesarea mesajului %s", msg_id)
            finally:
                # ACK chiar și la eșec: mesajul nu blochează coada (e logat).
                await redis.xack(STREAM_INBOUND, CONSUMER_GROUP, msg_id)
            handled += 1
    return handled


async def run_consumer(pool, redis: Redis, consumer_name: str) -> None:
    """Bucla principală a worker-ului (rulează până la anulare)."""
    await ensure_group(redis)

    async def _handle(event: dict) -> None:
        await process_event(pool, redis, event)

    debouncer = Debouncer(_handle)
    log.info("consumer %s pornit pe stream %s", consumer_name, STREAM_INBOUND)
    while True:
        await consume_once(pool, redis, consumer_name, debouncer)


async def _main() -> None:
    """Entrypoint proces worker: `python -m src.worker.consumer`.

    Numele de consumer = hostname-ul (în container = id-ul containerului) → unic
    per replică, ca XREADGROUP să distribuie mesajele corect între workeri."""
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # nu loga URL-uri cu token
    pool = await get_pool()
    redis = await get_redis()
    consumer_name = f"worker-{socket.gethostname()}"
    try:
        await run_consumer(pool, redis, consumer_name)
    finally:
        await close_redis()
        await close_pool()


if __name__ == "__main__":
    asyncio.run(_main())
