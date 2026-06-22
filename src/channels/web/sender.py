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

from src.channels.base import Capability
from src.channels.web.render import render_web, reply_from_outbox
from src.models import Reply

if TYPE_CHECKING:
    from redis.asyncio import Redis


# NX-120: chei prefixate cu tenantul (token-ul public = id de canal, 1:1 cu businessul) — izolare
# + observabilitate per tenant (P7), nu doar pe `visitor_id` global.
def out_channel(tenant: str, visitor_id: str) -> str:
    return f"web:out:{tenant}:{visitor_id}"


def backlog_key(tenant: str, visitor_id: str) -> str:
    return f"web:backlog:{tenant}:{visitor_id}"


class WebSender:
    """`account_id` = public_token (informativ); `to` = visitor_id (canalul Pub/Sub țintă)."""

    # NX-127: web randează acum RICH (carduri + chips) + CARDS + OFFER nativ (buton), paritate cu
    # ruta sincronă /web/chat. Fără clamp de lungime (frontendul randează bula).
    capabilities = frozenset({Capability.TEXT, Capability.RICH, Capability.CARDS, Capability.OFFER})
    max_text_len: int | None = None
    max_caption_len: int | None = None

    def __init__(self, redis: Redis, *, backlog_size: int = 20, backlog_ttl_s: int = 300) -> None:
        self._redis = redis
        self._backlog_size = backlog_size
        self._backlog_ttl_s = backlog_ttl_s

    async def _push_backlog(self, account_id: str, to: str, evt: str) -> None:
        """Backlog ATOMIC (NX-127): rpush+ltrim+expire într-un singur MULTI/EXEC → un round-trip,
        fără fereastră în care lista crește fără TTL. Apelat DUPĂ un publish reușit."""
        key = backlog_key(account_id, to)
        pipe = self._redis.pipeline(transaction=True)
        pipe.rpush(key, evt)
        pipe.ltrim(key, -self._backlog_size, -1)
        pipe.expire(key, self._backlog_ttl_s)
        await pipe.execute()

    async def _publish(self, account_id: str, to: str, evt: dict) -> str:
        """Publică UN eveniment SSE pe canalul vizitatorului + backlog. PUBLISH ÎNTÂI: dacă pică,
        dispatcher-ul marchează `failed` (retry) și NU `sent` → mesajul nu se pierde tăcut (P6);
        backlog-ul (reconectare) vine DOAR după publish-ul reușit (paritate cu send_text)."""
        payload = json.dumps(evt, ensure_ascii=False)
        # NX-120: `account_id` = token-ul public (tenant) → cheie prefixată cu tenantul (P7).
        await self._redis.publish(out_channel(account_id, to), payload)
        await self._push_backlog(account_id, to, payload)
        return evt["id"]

    async def send_text(self, account_id: str, to: str, text: str) -> str:
        """Publică textul ca eveniment SSE `type:"text"`. Întoarce un provider_msg_id sintetic."""
        msg_id = f"web_out_{uuid4().hex}"
        return await self._publish(account_id, to, {"id": msg_id, "type": "text", "text": text})

    async def send_rich(self, account_id: str, to: str, payload: dict) -> str:
        """NX-127: recomandare BOGATĂ pe ruta async (outbox→dispatcher→SSE). Reconstruiește `Reply`
        din `payload["rich"]` și-l trece prin ACELAȘI `render_web` ca ruta sincronă → publică
        `type:"rich"` cu `content`+`products`+`suggestions`(+`offer`). Cardurile/chips-urile nu mai
        cad tăcut la text (paritate sync↔async, P6)."""
        msg_id = f"web_out_{uuid4().hex}"
        rendered = render_web(reply_from_outbox(payload), payload.get("language") or "ro")
        return await self._publish(account_id, to, {"id": msg_id, "type": "rich", **rendered})

    async def send_products(self, account_id: str, to: str, text: str, products: list[dict]) -> str:
        """NX-127: listă de carduri pe ruta async (payload `products`/`carousel` fără rich). Publică
        `type:"rich"` cu `content`=text + carduri din `products`, fără suggestions."""
        msg_id = f"web_out_{uuid4().hex}"
        rendered = render_web(Reply(text=text or "", products=products), "ro")
        return await self._publish(account_id, to, {"id": msg_id, "type": "rich", **rendered})
