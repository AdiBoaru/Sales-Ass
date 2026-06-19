"""Gateway web (NX-20) — endpointuri pentru widget-ul de chat pe site (al treilea canal, E26).

  • GET  /web/bootstrap → emite o sesiune anonimă (visitor_id semnat HMAC cu secretul tenantului).
  • POST /web/messages  → verifică sesiunea + rate limit (IP + visitor) → envelope NEUTRU
                          `channel_kind='webchat'` pe stream-ul `inbound` (worker-ul îl procesează
                          ca orice canal — zero cod nou în consumer). ASYNC: reply via SSE.
  • POST /web/chat      → varianta SINCRONĂ (NX-25b): rulează pipeline-ul IN-PROCESS și întoarce
                          `{content, products, suggestions}` în ACELAȘI răspuns HTTP (request/
                          response, fără outbox/SSE). Contract pt widget-uri care randează carduri
                          de produs. Aceeași autentificare ca /web/messages (token + visitor_id +
                          sig) + CORS + rate-limit. Trece prin TOT pipeline-ul (multi-tenant,
                          validator de prețuri, căutare reală, analytics) — nu un endpoint paralel.
  • GET  /web/stream    → Server-Sent Events: abonat la `web:out:{visitor_id}`, replay backlog la
                          reconectare (Last-Event-ID), heartbeat. Outbound vine din `outbox` →
                          dispatcher → `WebSender` (publish), NU direct din endpoint (P5).

ZERO LLM hardcodat (proza vine din pipeline). `visitor_id` e PII de canal (P12) → trăiește în
`channel_identities` (scris de pipeline), niciodată în loguri. SSE (nu WebSocket) → trece prin
orice proxy/CDN. Montat condiționat în `webhook/app.py` (doar dacă `web_enabled`).
"""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from src.channels.base import InboundEvent
from src.channels.web.sender import backlog_key, out_channel
from src.config import get_settings
from src.db.connection import admin_conn, get_pool, tenant_conn
from src.db.queries.businesses import load_business
from src.db.queries.channels import resolve_channel
from src.redis_bus import enqueue_inbound, get_redis
from src.web.session import WebSession, get_session_cache, issue_visitor, verify_web_session
from src.worker.compose import ensure_disclaimer, flatten_framing
from src.worker.limits import incr_window
from src.worker.processor import TurnResult, handle_turn

log = logging.getLogger(__name__)

router = APIRouter(prefix="/web", tags=["web"])


class WebMessageIn(BaseModel):
    token: str = Field(min_length=1)
    visitor_id: str = Field(min_length=1)
    sig: str = Field(min_length=1)
    text: str = Field(min_length=1, max_length=2000)
    client_msg_id: str | None = None


class WebChatIn(BaseModel):
    """Request sincron (POST /web/chat). `message` = textul userului; `history` (opțional, trimis de
    frontend) e IGNORAT — serverul e sursa de adevăr pt istoric (din DB, pe `visitor_id`)."""

    token: str = Field(min_length=1)
    visitor_id: str = Field(min_length=1)
    sig: str = Field(min_length=1)
    message: str = Field(min_length=1, max_length=2000)
    client_msg_id: str | None = None


# --- seam-uri de control plane (admin_conn) — monkeypatch-uite în teste ------


async def _resolve_token(token: str) -> dict | None:
    """`public_token → {business_id, session_secret}` prin cache de control plane (admin_conn)."""
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        return await get_session_cache().get(conn, token)


async def _verify(token: str, visitor_id: str, sig: str) -> WebSession | None:
    """(token, visitor_id, sig) → sesiune validă sau None (token necunoscut / sig invalidă)."""
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        return await verify_web_session(conn, token, visitor_id, sig)


async def web_rate_limited(redis, token: str, ip: str, visitor_id: str) -> bool:
    """Două contoare (IP prinde rotirea de visitor_id; visitor prinde spam-ul unui client legit).
    Oricare peste prag → True. Chei `webrl:*` (NU PII în clar — visitor_id e id de canal)."""
    s = get_settings()
    window = s.web_rate_limit_window_s
    n_visitor = await incr_window(redis, f"webrl:visitor:{visitor_id}", window)
    n_ip = await incr_window(redis, f"webrl:ip:{token}:{ip}", window)
    return n_visitor > s.web_rate_limit_max_visitor or n_ip > s.web_rate_limit_max_ip


@router.get("/bootstrap")
async def web_bootstrap(token: str) -> dict:
    """Emite o sesiune nouă de vizitator (anonim, fără login). 403 dacă tokenul nu mapează la
    un canal `webchat` activ."""
    resolved = await _resolve_token(token)
    if resolved is None:
        raise HTTPException(status_code=403, detail="unknown token")
    visitor_id, sig = issue_visitor(token, resolved["session_secret"])
    return {"token": token, "visitor_id": visitor_id, "sig": sig, "sse_url": "/web/stream"}


@router.post("/messages")
async def web_message(req: WebMessageIn, request: Request) -> dict:
    """Client → bot: verifică sesiunea + rate limit → envelope neutru pe stream. NU trimite reply
    (ăla iese prin outbox → dispatcher → WebSender, P5)."""
    session = await _verify(req.token, req.visitor_id, req.sig)
    if session is None:
        raise HTTPException(status_code=403, detail="invalid session")
    redis = await get_redis()
    ip = request.client.host if request.client else "unknown"
    if await web_rate_limited(redis, req.token, ip, req.visitor_id):
        raise HTTPException(status_code=429, detail="rate limited")
    event = InboundEvent(
        channel_kind="webchat",
        channel_account_id=req.token,  # public token = provider_account_id al canalului webchat
        sender_external_id=req.visitor_id,  # vizitatorul = userul pe canal
        provider_msg_id=req.client_msg_id or str(uuid4()),  # idempotent dacă clientul dă un id
        content_type="text",
        body=req.text.strip(),
    )
    await enqueue_inbound(redis, event.to_dict())
    return {"accepted": True, "msg_id": event.provider_msg_id}


def _card(name, price, image, url, *, product_id=None, rating=None, reason=None) -> dict:
    """Un card de produs pt răspunsul sincron. Câmpuri compacte (P8) din ctx.reply; `image_url`/
    `rating` lipsesc dacă datele nu există (nu inventăm). Frontendul randează ce primește."""
    card: dict[str, Any] = {"product_id": product_id, "name": name, "price": price}
    if image:
        card["image_url"] = image
    if url:
        card["url"] = url
    if rating:
        card["rating"] = rating
    if reason:
        card["reason"] = reason  # fit scurt LLM (din RichItem), ancorat pe un pro real
    return card


def _build_chat_response(result: TurnResult) -> dict:
    """`TurnResult` (calea sincronă) → `{content, products, suggestions}` (contractul widget-ului).

    Pe RICH (recomandare cu carduri): `content` = DOAR framing-ul conversațional (`flatten_framing`:
    intro + recomandare + educație), NU enumerarea produselor — o fac cardurile (`products`) și
    butoanele (`suggestions`). Așa textul nu mai dublează cardurile (vs WhatsApp/cache, care iau
    `flatten()` complet — same engine, prezentare per canal). Pe reply simplu (text/produse fără
    rich): `content` = `reply.text`. Disclaimer-ul AI e re-aplicat idempotent. Tur fără reply
    (handoff tăcut / degradare) → content gol, fără carduri (frontendul afișează fallback)."""
    reply = result.reply
    if reply is None:
        return {"content": "", "products": [], "suggestions": []}
    lang = result.language or "ro"
    products: list[dict] = []
    suggestions: list[str] = []
    if reply.rich is not None:
        products = [
            _card(
                it.name,
                it.price,
                it.image,
                it.url,
                product_id=it.product_id,
                rating=it.rating,
                reason=it.reason,
            )
            for it in reply.rich.items
        ]
        suggestions = [c.label for c in reply.rich.chips]
        # Widget: produsele = CARDURI → content e doar framing (intro + pick + educație),
        # fără lista numerotată (o fac cardurile) și fără „Poți cere și:" (o fac butoanele).
        content = ensure_disclaimer(flatten_framing(reply.rich), lang)
    elif reply.products:
        products = [
            _card(p.get("name"), p.get("price"), p.get("image"), p.get("url"),
                  product_id=p.get("product_id"))
            for p in reply.products
        ]
        content = ensure_disclaimer(reply.text, lang)
    else:
        content = ensure_disclaimer(reply.text, lang)
    return {"content": content, "products": products, "suggestions": suggestions}


@router.post("/chat")
async def web_chat(req: WebChatIn, request: Request) -> dict:
    """Client → bot, SINCRON (NX-25b): verifică sesiunea + rate limit → rulează pipeline-ul
    IN-PROCESS pe o conexiune tenant-scoped (`deliver=False`: fără outbox/dispatcher) → întoarce
    `{content, products, suggestions}` în răspuns. Spre deosebire de /web/messages (ACK + SSE),
    AICI răspunsul HTTP e transportul. Trece prin TOT pipeline-ul (gates, validator, căutare reală,
    analytics) — multi-tenant garantat de `tenant_conn(business_id)` derivat din token (P7)."""
    session = await _verify(req.token, req.visitor_id, req.sig)
    if session is None:
        raise HTTPException(status_code=403, detail="invalid session")
    redis = await get_redis()
    ip = request.client.host if request.client else "unknown"
    if await web_rate_limited(redis, req.token, ip, req.visitor_id):
        raise HTTPException(status_code=429, detail="rate limited")
    # channel_id pt get_or_create_conversation (control plane, ca resolve_web_session: token e
    # provider_account_id-ul canalului webchat). business_id vine din sesiunea deja verificată.
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        channel = await resolve_channel(conn, "webchat", req.token)
    if channel is None:
        raise HTTPException(status_code=403, detail="unknown channel")
    event = InboundEvent(
        channel_kind="webchat",
        channel_account_id=req.token,
        sender_external_id=req.visitor_id,
        provider_msg_id=req.client_msg_id or str(uuid4()),
        content_type="text",
        body=req.message.strip(),
    ).to_dict()
    async with tenant_conn(session.business_id) as conn:
        business = await load_business(conn, session.business_id)
        if business is None:
            raise HTTPException(status_code=503, detail="business unavailable")
        result = await handle_turn(
            conn, business, channel["channel_id"], event, redis=redis, deliver=False
        )
    return _build_chat_response(result)


def _sse(evt: dict) -> str:
    """Formatează un eveniment ca frame SSE (`id:` pentru Last-Event-ID + `data:` JSON)."""
    return f"id: {evt['id']}\ndata: {json.dumps(evt, ensure_ascii=False)}\n\n"


async def _replay_after(redis, visitor_id: str, last_event_id: str | None) -> list[dict]:
    """Evenimentele din backlog de DUPĂ `last_event_id` (reconectare). Gol dacă nu e dat sau dacă
    id-ul nu mai e în backlog (a expirat → clientul a pierdut prea mult; outbox-ul rămâne sursa)."""
    if not last_event_id:
        return []
    raw = await redis.lrange(backlog_key(visitor_id), 0, -1)
    out: list[dict] = []
    seen = False
    for item in raw:
        evt = json.loads(item)
        if seen:
            out.append(evt)
        elif evt.get("id") == last_event_id:
            seen = True
    return out


@router.get("/stream")
async def web_stream(
    token: str,
    visitor_id: str,
    sig: str,
    request: Request,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    """Bot → client: conexiune SSE persistentă. Abonat la `web:out:{visitor_id}`, replay backlog
    la reconectare, heartbeat la idle (ține proxy-ul deschis), iese curat la deconectare."""
    session = await _verify(token, visitor_id, sig)
    if session is None:
        raise HTTPException(status_code=403, detail="invalid session")
    redis = await get_redis()
    heartbeat = get_settings().web_sse_heartbeat_s

    async def gen():
        pubsub = redis.pubsub()
        await pubsub.subscribe(out_channel(visitor_id))
        try:
            for evt in await _replay_after(redis, visitor_id, last_event_id):
                yield _sse(evt)
            while True:
                if await request.is_disconnected():
                    break
                msg = await pubsub.get_message(timeout=heartbeat, ignore_subscribe_messages=True)
                if msg is None:
                    yield ": keepalive\n\n"  # heartbeat (proxy idle-timeout)
                    continue
                yield _sse(json.loads(msg["data"]))
        finally:
            await pubsub.unsubscribe(out_channel(visitor_id))

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
