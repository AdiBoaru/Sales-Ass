"""Poller Telegram — inbound prin long polling (NX-61), canal de TEST.

Fără webhook/HTTPS/tunel: procesul cheamă `getUpdates` în buclă și pune mesajele
text pe ACELAȘI stream `inbound` ca WhatsApp (envelope neutru). Consumer-ul
existent rezolvă tenantul (`resolve_channel('telegram', bot_id)`) și rulează
pipeline-ul; răspunsul iese prin outbox → dispatcher (TelegramClient).

Dedupe: `offset`-ul (ținut în Redis) garantează că nu re-procesăm update-uri
confirmate; `inbound_dedupe` (NX-51) rămâne plasa durabilă pe (business, message_id).
"""

import asyncio
import logging

import httpx

from src.channels.base import InboundEvent
from src.channels.telegram.client import TelegramClient
from src.config import get_settings
from src.redis_bus import close_redis, enqueue_inbound, get_redis

log = logging.getLogger(__name__)


def _offset_key(account_id: str) -> str:
    return f"tg:offset:{account_id}"


def _to_event(update: dict, account_id: str) -> InboundEvent | None:
    """Mapează un update Telegram la envelope-ul neutru. None dacă nu e text util."""
    msg = update.get("message")
    if not msg or "text" not in msg:
        return None  # edited_message, callback_query, media fără text → ignorate (TEST)
    chat = msg.get("chat") or {}
    sender = msg.get("from") or {}
    return InboundEvent(
        channel_kind="telegram",
        channel_account_id=account_id,
        sender_external_id=str(chat.get("id")),
        provider_msg_id=str(msg.get("message_id")),
        content_type="text",
        timestamp=str(msg.get("date")) if msg.get("date") is not None else None,
        body=msg.get("text"),
        sender_name=sender.get("first_name"),
        payload=update,
    )


async def poll_once(client: TelegramClient, redis, account_id: str, *, timeout: int = 30) -> int:
    """Un ciclu getUpdates → enqueue. Întoarce câte mesaje text au fost puse pe stream.

    Offset-ul avansează peste TOATE update-urile primite (inclusiv cele ignorate),
    altfel le-am re-cere la infinit."""
    offset = int(await redis.get(_offset_key(account_id)) or 0)
    updates = await client.get_updates(offset, timeout=timeout)
    if not updates:
        return 0

    enqueued = 0
    max_update_id = offset - 1
    for update in updates:
        max_update_id = max(max_update_id, int(update.get("update_id", max_update_id)))
        event = _to_event(update, account_id)
        if event is None:
            continue
        await enqueue_inbound(redis, event.to_dict())
        enqueued += 1

    await redis.set(_offset_key(account_id), max_update_id + 1)
    return enqueued


async def run_poller(client: TelegramClient, redis, account_id: str) -> None:
    """Bucla principală a poller-ului (rulează până la anulare). Un update care
    crapă procesarea NU oprește bucla (principiul 6)."""
    log.info("telegram poller pornit (bot %s)", account_id)
    while True:
        try:
            await poll_once(client, redis, account_id)
        except Exception:  # noqa: BLE001 — rețea/Telegram down → log + retry scurt
            log.exception("telegram getUpdates a eșuat — reîncerc")
            await asyncio.sleep(3)


async def _main() -> None:
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # nu loga URL-uri cu token
    settings = get_settings()
    if not settings.telegram_bot_token:
        log.error("TELEGRAM_BOT_TOKEN lipsește — poller-ul nu pornește")
        return

    redis = await get_redis()
    async with httpx.AsyncClient() as http:
        client = TelegramClient(http, settings.telegram_bot_token)
        me = await client.get_me()  # bot id = channel_account_id (provider_account_id)
        account_id = str(me.get("id"))
        log.info("bot @%s (id=%s)", me.get("username"), account_id)
        try:
            await run_poller(client, redis, account_id)
        finally:
            await close_redis()


if __name__ == "__main__":
    asyncio.run(_main())
