"""Marginea de INTRARE web (NX-20b) — router FastAPI `/web/*`.

Al treilea canal (`channel_kind='webchat'`) intră prin ACEEAȘI margine neutră ca
WhatsApp/Telegram (NX-60): endpointul produce un `InboundEvent` neutru pe stream-ul `inbound`,
iar worker-ul rezolvă tenantul cu `resolve_channel('webchat', token)` — zero cod nou în consumer.

Trei endpointuri (publice, fără auth de user — autentificate prin token public + HMAC, vezi
`session.py`):
  • `GET  /web/bootstrap` — emite o sesiune nouă de vizitator (visitor_id semnat).
  • `POST /web/messages`  — client → bot: verifică sesiunea, rate limit (IP+visitor), buget de
    input, dedupe L1, XADD pe stream.
  • `GET  /web/stream`    — bot → client (SSE) — vine în NX-20c.

Margine SUBȚIRE (ca path-ul Meta): zero LLM, zero scriere de date de tenant. `visitor_id`/IP/token
NU apar în loguri (P12). Default OFF (`WEB_ENABLED=false`, V1.5) → endpointurile răspund 404.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from redis.exceptions import RedisError

from src.channels.base import InboundEvent
from src.config import get_settings
from src.db.connection import admin_conn, get_pool
from src.redis_bus import enqueue_inbound, get_redis, seen_before
from src.web.limits import web_rate_limited
from src.web.session import get_session_cache, issue_visitor, verify_web_session

if TYPE_CHECKING:
    import asyncpg
    from redis.asyncio import Redis

router = APIRouter(prefix="/web", tags=["web"])

_MAX_TEXT = 2000  # buget de input dur (anti-abuz) — peste = 400


class WebMessageIn(BaseModel):
    token: str
    visitor_id: str
    sig: str
    text: str
    client_msg_id: str | None = None  # id de client → idempotență la retry (dedupe L1)


# --- dependențe (injectabile/overridabile în teste) --------------------------


def require_web_enabled() -> None:
    """Kill-switch global: canalul web e OFF până la V1.5. Dezactivat → 404 (canalul nu există)."""
    if not get_settings().web_enabled:
        raise HTTPException(status_code=404, detail="web channel disabled")


async def redis_dep() -> Redis:
    return await get_redis()


async def admin_conn_dep() -> AsyncIterator[asyncpg.Connection]:
    """Conexiune de CONTROL PLANE pt lookup-ul `public_token → secret` (derivă tenantul, ca
    `resolve_channel`). Cache-ul LRU+TTL din `session.py` o atinge DOAR la miss → la trafic real
    aproape toate requesturile sunt servite din memorie, nu din DB."""
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        yield conn


def _client_ip(request: Request) -> str:
    """IP-ul clientului — prima intrare din `X-Forwarded-For` (în spatele unui proxy/CDN), altfel
    peer-ul direct. Folosit DOAR ca cheie de rate-limit; niciodată logat în clar (P12)."""
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else ""


# --- endpoints ---------------------------------------------------------------


@router.get("/bootstrap")
async def web_bootstrap(
    token: str,
    conn: asyncpg.Connection = Depends(admin_conn_dep),
    _: None = Depends(require_web_enabled),
) -> dict:
    """Emite o sesiune nouă de vizitator pt un token public valid.

    `visitor_id` generat + semnat HMAC cu `session_secret`-ul tenantului → widget-ul le ține și le
    retrimite la fiecare mesaj. `403` la token necunoscut/inactiv (fără a distinge de o sesiune
    invalidă — nu dăm un oracol)."""
    resolved = await get_session_cache().get(conn, token)
    if resolved is None:
        raise HTTPException(status_code=403, detail="unknown token")
    visitor_id, sig = issue_visitor(token, resolved["session_secret"])
    return {
        "public_token": token,
        "visitor_id": visitor_id,
        "sig": sig,
        "sse_url": f"/web/stream?token={token}&visitor_id={visitor_id}&sig={sig}",
    }


@router.post("/messages")
async def web_message(
    body: WebMessageIn,
    request: Request,
    conn: asyncpg.Connection = Depends(admin_conn_dep),
    redis: Redis = Depends(redis_dep),
    _: None = Depends(require_web_enabled),
) -> dict:
    """Client → bot: produce un envelope neutru pe stream-ul `inbound` (ca poller-ul Telegram).

    Pași (rapizi, fără LLM, fără date de tenant): verifică sesiunea (HMAC) → rate limit (IP+visitor)
    → buget de input → dedupe L1 (idempotent pe `client_msg_id`) → XADD. Worker-ul rezolvă tenantul
    din `channel_account_id=token` și rulează pipeline-ul ca pt orice canal."""
    session = await verify_web_session(conn, body.token, body.visitor_id, body.sig)
    if session is None:
        raise HTTPException(status_code=403, detail="invalid session")

    s = get_settings()
    try:
        tripped = await web_rate_limited(
            redis,
            session.token,
            _client_ip(request),
            session.visitor_id,
            max_ip=s.web_rate_limit_max_ip,
            max_visitor=s.web_rate_limit_max_visitor,
            window_s=s.web_rate_limit_window_seconds,
        )
        if tripped:
            raise HTTPException(status_code=429, detail="rate limited")

        text = body.text.strip()
        if not 1 <= len(text) <= _MAX_TEXT:
            raise HTTPException(status_code=400, detail="empty or too long")

        provider_msg_id = body.client_msg_id or f"web_{uuid4().hex}"
        if await seen_before(redis, session.token, provider_msg_id):
            return {"accepted": True, "msg_id": provider_msg_id, "deduped": True}

        event = InboundEvent(
            channel_kind="webchat",
            channel_account_id=session.token,  # public token = canalul receptor
            sender_external_id=session.visitor_id,  # vizitatorul = userul pe canal
            provider_msg_id=provider_msg_id,
            content_type="text",
            body=text,
        )
        await enqueue_inbound(redis, event.to_dict())
    except RedisError:
        # Redis indisponibil (rate-limit/dedupe/XADD) → NU pierdem tăcut: 503, clientul reîncearcă
        # (idempotent pe client_msg_id). HTTPException-urile de mai sus (400/429) propagă neatinse.
        raise HTTPException(status_code=503, detail="service unavailable") from None

    return {"accepted": True, "msg_id": provider_msg_id}
