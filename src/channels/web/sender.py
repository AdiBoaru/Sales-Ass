"""WebSender (NX-20) — transport outbound web prin Redis Pub/Sub + backlog.

Implementează `ChannelSender`, dar „trimite" PUBLICÂND pe `web:out:{visitor_id}` (handler-ul SSE
e abonat și retransmite ca eveniment), nu prin HTTP la o platformă. În plus scrie un backlog
LIST per vizitator (ultimele N, cu TTL) pentru reconectare (Last-Event-ID): Pub/Sub nu persistă,
backlog-ul e plasa. Dispatcher-ul îl alege pentru `channel_kind='webchat'` (P5: tot prin
outbox → dispatcher; SSE-ul doar retransmite ce-a publicat dispatcher-ul).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from uuid import uuid4

if TYPE_CHECKING:
    from redis.asyncio import Redis


def out_channel(visitor_id: str) -> str:
    return f"web:out:{visitor_id}"


def backlog_key(visitor_id: str) -> str:
    return f"web:backlog:{visitor_id}"


class WebSender:
    """`account_id` = public_token (informativ); `to` = visitor_id (canalul Pub/Sub țintă)."""

    def __init__(self, redis: Redis, *, backlog_size: int = 20, backlog_ttl_s: int = 300) -> None:
        self._redis = redis
        self._backlog_size = backlog_size
        self._backlog_ttl_s = backlog_ttl_s

    async def send_text(self, account_id: str, to: str, text: str) -> str:
        """Publică textul pe canalul vizitatorului + îl pune în backlog. Întoarce un provider_msg_id
        sintetic (consistență cu contractul). PUBLISH ÎNTÂI: dacă pică, dispatcher-ul marchează
        `failed` (retry) și NU `sent` → mesajul nu se pierde tăcut (P6). Backlog-ul (reconectare)
        vine după publish-ul reușit."""
        msg_id = f"web_out_{uuid4().hex}"
        evt = json.dumps({"id": msg_id, "type": "text", "text": text}, ensure_ascii=False)
        await self._redis.publish(out_channel(to), evt)
        key = backlog_key(to)
        await self._redis.rpush(key, evt)
        await self._redis.ltrim(key, -self._backlog_size, -1)
        await self._redis.expire(key, self._backlog_ttl_s)
        return msg_id
