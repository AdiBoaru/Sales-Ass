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

import json
import logging

from redis.asyncio import Redis
from redis.exceptions import ResponseError

from src.db.connection import admin_conn, tenant_conn
from src.db.queries.businesses import load_business
from src.db.queries.channels import resolve_channel_by_phone
from src.db.queries.message_status import record_status_event
from src.redis_bus import STREAM_INBOUND
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
    """Rezolvă tenantul și rutează evenimentul după `kind` (message | status)."""
    phone_number_id = event.get("phone_number_id", "")
    async with admin_conn(pool) as conn:
        channel = await resolve_channel_by_phone(conn, phone_number_id)
    if channel is None:
        log.warning("canal necunoscut pentru phone_number_id=%s — ignorat", phone_number_id)
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


async def consume_once(pool, redis: Redis, consumer_name: str, *, block_ms: int = 2000) -> int:
    """Un ciclu de citire+procesare. Întoarce numărul de mesaje tratate."""
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
                await process_event(pool, redis, event)
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
    log.info("consumer %s pornit pe stream %s", consumer_name, STREAM_INBOUND)
    while True:
        await consume_once(pool, redis, consumer_name)
