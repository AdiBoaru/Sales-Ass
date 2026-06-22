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
from uuid import uuid4

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from redis.exceptions import RedisError

from src.channels.base import InboundEvent
from src.channels.web.render import render_web
from src.channels.web.sender import backlog_key, out_channel
from src.config import get_settings
from src.db.connection import admin_conn, get_pool, tenant_conn
from src.db.queries.businesses import load_business
from src.db.queries.channels import resolve_channel
from src.redis_bus import enqueue_inbound, get_redis
from src.web.session import WebSession, get_session_cache, issue_visitor, verify_web_session
from src.webhook.body_limit import enforce_body_cap
from src.worker.limits import (
    cost_over_budget,
    incr_window,
    web_cost_add_visitor,
    web_cost_over_visitor_cap,
)
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


async def web_rate_limited(
    redis, token: str, ip: str, visitor_id: str, *, fail_closed: bool
) -> bool:
    """Două contoare (IP prinde rotirea de visitor_id; visitor prinde spam-ul unui client legit).
    Oricare peste prag → True. Chei `webrl:*` (NU PII în clar — visitor_id e id de canal).

    NX-120: `fail_closed` decide ce facem când Redis pică. Pe calea SINCRONĂ `/web/chat` (care
    cheltuie LLM real per request) → `fail_closed=True`: tratăm ca „limitat" (429), ca un atacator
    cu token public să NU poată arde bugetul fix când guard-ul e indisponibil. Pe `/web/messages`
    (doar pune un envelope pe stream, spend-ul se evaluează în worker) → `fail_closed=False`
    (fail-open păstrat: indisponibilitatea guard-ului nu blochează ingestia ieftină)."""
    s = get_settings()
    window = s.web_rate_limit_window_s
    try:
        # NX-120: chei prefixate cu tenantul (token) — P7, consecvent cu webcost:*/web:out:*.
        n_visitor = await incr_window(redis, f"webrl:visitor:{token}:{visitor_id}", window)
        n_ip = await incr_window(redis, f"webrl:ip:{token}:{ip}", window)
    except (RedisError, OSError):
        return fail_closed  # True → 429 (fail-CLOSED); False → trece (fail-open)
    return n_visitor > s.web_rate_limit_max_visitor or n_ip > s.web_rate_limit_max_ip


@router.get("/bootstrap")
async def web_bootstrap(token: str, request: Request) -> dict:
    """Emite o sesiune nouă de vizitator (anonim, fără login). 403 dacă tokenul nu mapează la
    un canal `webchat` activ.

    NX-120: CORS-ul de browser blochează doar CITIREA răspunsului de către JS cross-origin, nu
    PROCESAREA pe server (un bot ignoră CORS). Verificăm `Origin` server-side: prezent ȘI ne-
    allowlistat → 403 (secure-by-default: allowlist GOL → ORICE Origin de browser e respins; un
    widget real setează mereu WEB_CORS_ORIGINS). Origin absent (non-browser / same-origin / health)
    → permis (suprafața reală de abuz e browser-driven)."""
    origin = request.headers.get("origin")
    if origin and origin not in get_settings().web_cors_origins_list:
        raise HTTPException(status_code=403, detail="origin not allowed")
    resolved = await _resolve_token(token)
    if resolved is None:
        raise HTTPException(status_code=403, detail="unknown token")
    visitor_id, sig = issue_visitor(token, resolved["session_secret"])
    return {"token": token, "visitor_id": visitor_id, "sig": sig, "sse_url": "/web/stream"}


@router.post("/messages")
async def web_message(req: WebMessageIn, request: Request) -> dict:
    """Client → bot: verifică sesiunea + rate limit → envelope neutru pe stream. NU trimite reply
    (ăla iese prin outbox → dispatcher → WebSender, P5)."""
    await enforce_body_cap(request, get_settings().web_max_body_bytes)  # NX-120
    session = await _verify(req.token, req.visitor_id, req.sig)
    if session is None:
        raise HTTPException(status_code=403, detail="invalid session")
    redis = await get_redis()
    ip = request.client.host if request.client else "unknown"
    # NX-120: fail-OPEN (doar pune envelope pe stream; spend-ul real se evaluează în worker).
    if await web_rate_limited(redis, req.token, ip, req.visitor_id, fail_closed=False):
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


def _build_chat_response(result: TurnResult) -> dict:
    """`TurnResult` (calea SINCRONĂ `/web/chat`) → contractul widget-ului, prin randorul UNIC
    `render_web` (NX-127). Aceeași sursă de adevăr ca ruta async SSE (`WebSender.send_rich`) →
    paritate de UX rich↔text între rute. Tur fără reply → content gol (frontendul dă fallback)."""
    return render_web(result.reply, result.language or "ro")


@router.post("/chat")
async def web_chat(req: WebChatIn, request: Request) -> dict:
    """Client → bot, SINCRON (NX-25b): verifică sesiunea + rate limit → rulează pipeline-ul
    IN-PROCESS pe o conexiune tenant-scoped (`deliver=False`: fără outbox/dispatcher) → întoarce
    `{content, products, suggestions}` în răspuns. Spre deosebire de /web/messages (ACK + SSE),
    AICI răspunsul HTTP e transportul. Trece prin TOT pipeline-ul (gates, validator, căutare reală,
    analytics) — multi-tenant garantat de `tenant_conn(business_id)` derivat din token (P7)."""
    await enforce_body_cap(request, get_settings().web_max_body_bytes)  # NX-120: cap de body
    session = await _verify(req.token, req.visitor_id, req.sig)
    if session is None:
        raise HTTPException(status_code=403, detail="invalid session")
    redis = await get_redis()
    ip = request.client.host if request.client else "unknown"
    # NX-120: calea care CHELTUIE LLM → fail-CLOSED (Redis căzut = 429, nu „lasă să treacă").
    if await web_rate_limited(redis, req.token, ip, req.visitor_id, fail_closed=True):
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
    s = get_settings()
    async with tenant_conn(session.business_id) as conn:
        business = await load_business(conn, session.business_id)
        if business is None:
            raise HTTPException(status_code=503, detail="business unavailable")
        # NX-120: gard de admitere de cost ÎNAINTE de pipeline — nu cheltui LLM dacă bugetul
        # tenantului SAU al vizitatorului e atins. Redis căzut → fail-CLOSED (429), consecvent cu
        # rate-limit-ul. (Precizia per-tur + cap per-contact pe canalele async = NX-125.)
        cap = business.daily_cost_cap_usd or s.daily_cost_cap_usd
        try:
            over_budget = await cost_over_budget(
                redis, session.business_id, cap
            ) or await web_cost_over_visitor_cap(
                redis, session.business_id, req.visitor_id, s.web_cost_cap_per_visitor_usd
            )
        except (RedisError, OSError):
            over_budget = True  # fail-CLOSED: dacă nu pot verifica, NU cheltui
        if over_budget:
            raise HTTPException(status_code=429, detail="budget exceeded")
        result = await handle_turn(
            conn, business, channel["channel_id"], event, redis=redis, deliver=False
        )
    # NX-120: contor de cost per-vizitator (estimare-plasă de admitere; precizia per-tur = NX-125).
    # Best-effort: un fail de Redis aici NU rupe răspunsul deja calculat.
    try:
        await web_cost_add_visitor(
            redis, session.business_id, req.visitor_id, s.cost_triage_usd + s.cost_agent_usd
        )
    except Exception:  # noqa: BLE001 — best-effort: NU rupem un răspuns deja calculat (orice eroare)
        log.warning("web_cost_add_visitor a eșuat (răspunsul a fost livrat)")
    return _build_chat_response(result)


def _sse(evt: dict) -> str:
    """Formatează un eveniment ca frame SSE (`id:` pentru Last-Event-ID + `data:` JSON)."""
    return f"id: {evt['id']}\ndata: {json.dumps(evt, ensure_ascii=False)}\n\n"


async def _replay_after(
    redis, tenant: str, visitor_id: str, last_event_id: str | None
) -> list[dict]:
    """Evenimentele din backlog de DUPĂ `last_event_id` (reconectare). Gol dacă nu e dat sau dacă
    id-ul nu mai e în backlog (a expirat → clientul a pierdut prea mult; outbox-ul rămâne sursa)."""
    if not last_event_id:
        return []
    raw = await redis.lrange(backlog_key(tenant, visitor_id), 0, -1)
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
        # NX-120: cheie prefixată cu tenantul (token) — același prefix ca publish-ul WebSender.
        await pubsub.subscribe(out_channel(token, visitor_id))
        try:
            for evt in await _replay_after(redis, token, visitor_id, last_event_id):
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
            await pubsub.unsubscribe(out_channel(token, visitor_id))

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
