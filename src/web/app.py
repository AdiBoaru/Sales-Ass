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

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from time import monotonic
from uuid import UUID, uuid4

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from redis.exceptions import RedisError

from src.channels.base import InboundEvent
from src.channels.web.render import render_web
from src.channels.web.sender import backlog_key, out_channel
from src.config import get_settings
from src.db.connection import admin_conn, get_pool
from src.db.provider import tenant_db
from src.db.queries.businesses import load_business
from src.db.queries.channels import resolve_channel
from src.db.queries.web_turns import WebTurnRow, get_turn, get_turn_by_id
from src.models import Event
from src.privacy import apply_boundary
from src.redis_bus import enqueue_inbound, get_redis
from src.web.contracts_v2 import WebTurnRequestV2, parse_turn_request
from src.web.identity import verify_identity_token
from src.web.security import (
    check_origin,
    normalize_allowlist,
    normalize_origin,
    origin_bucket,
    redact_secret,
    verify_demo_access,
    visitor_bucket,
)
from src.web.session import (
    WebSession,
    get_session_cache,
    issue_session_v2,
    issue_visitor,
    verify_web_session_any,
)
from src.web.turn_events import (
    get_phase,
    result_event,
    status_event,
    status_payload,
    terminal_view,
)
from src.web.turn_executor import wake_executor
from src.web.turn_service import (
    TERMINAL_LEDGER_STATUSES,
    Accepted,
    ActiveTurnConflict,
    EmptyTerminalResult,
    ExistingCompleted,
    ExistingInProgress,
    FencedTurnCompletion,
    IdempotencyConflict,
    accept_web_turn,
    attempt_bucket,
    claim_web_turn,
    complete_web_turn_on_conn,
    error_view,
    fail_web_turn,
    get_turn_for_session,
    project_wire_status,
    request_fingerprint,
    session_ref_hash,
)
from src.webhook.body_limit import enforce_body_cap
from src.worker.admission import get_admission, tenant_bucket
from src.worker.aftercare import persist_events, run_aftercare
from src.worker.limits import (
    cost_over_budget,
    incr_window,
    web_cost_add_visitor,
    web_cost_over_visitor_cap,
)
from src.worker.processor import TurnResult, handle_turn
from src.worker.turn_lock import acquire_turn_lock, release_turn_lock

log = logging.getLogger(__name__)

router = APIRouter(prefix="/web", tags=["web"])


class WebMessageIn(BaseModel):
    token: str = Field(min_length=1)
    visitor_id: str = Field(min_length=1)
    sig: str = Field(min_length=1)
    text: str = Field(min_length=1, max_length=2000)
    client_msg_id: str | None = None
    # NX-129: JWT host-signed de login passthrough (opțional). Absent/invalid → vizitator anonim.
    id_token: str | None = Field(default=None, max_length=4096)


class WebChatIn(BaseModel):
    """Request sincron (POST /web/chat). `message` = textul userului; `history` (opțional, trimis de
    frontend) e IGNORAT — serverul e sursa de adevăr pt istoric (din DB, pe `visitor_id`)."""

    token: str = Field(min_length=1)
    visitor_id: str = Field(min_length=1)
    sig: str = Field(min_length=1)
    message: str = Field(min_length=1, max_length=2000)
    client_msg_id: str | None = None
    # NX-129: JWT host-signed de login passthrough (opțional). Absent/invalid → vizitator anonim.
    id_token: str | None = Field(default=None, max_length=4096)


# --- seam-uri de control plane (admin_conn) — monkeypatch-uite în teste ------


async def _resolve_token(token: str) -> dict | None:
    """`public_token → {business_id, session_secret}` prin cache de control plane (admin_conn)."""
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        return await get_session_cache().get(conn, token)


async def _verify(token: str, visitor_id: str, sig: str) -> WebSession | None:
    """(token, visitor_id, sig) → sesiune validă sau None (token necunoscut / sig invalidă).

    NX-229: acceptă ambele versiuni de semnătură în overlap (v1 HMAC plat, v2 claims semnate).
    Semnătura rămâne cu TREI argumente poziționale — e seam-ul monkeypatch-uit de teste, iar
    legarea de origin se verifică separat, pe claims (`_enforce_session_origin`)."""
    pool = await get_pool()
    s = get_settings()
    async with admin_conn(pool) as conn:
        session, reason = await verify_web_session_any(
            conn, token, visitor_id, sig, v2_required=s.web_session_v2_required
        )
    if reason is not None:
        # Reason code LOW-CARDINALITY, fără token/sig/visitor în clar (P12).
        log.info("web_session_rejected reason=%s tok=%s", reason, redact_secret(token))
    return session


# --- NX-229: poarta de margine (origin, acces demo, legare de sesiune) -------


def _allowlist_for(resolved: dict | None) -> frozenset[str]:
    """Originile permise pentru un canal: cele PER CANAL dacă există, altfel globalul.

    Per-canal contează pentru că allowlistul e per-tenant prin natura lui: originul unui client
    n-are ce căuta în poarta altuia. Globalul rămâne ca fallback pentru seed-urile care n-au
    declarat încă nimic."""
    per_channel = (resolved or {}).get("allowed_origins")
    if per_channel:
        return normalize_allowlist([o.strip() for o in str(per_channel).split(",") if o.strip()])
    return normalize_allowlist(get_settings().web_cors_origins_list)


def _enforce_origin(request: Request, resolved: dict | None = None) -> str | None:
    """Verifică `Origin` server-side și întoarce forma canonică (pentru legarea sesiunii).

    Se aplică pe TOATE endpointurile, nu doar pe bootstrap: CORS-ul browserului oprește doar
    CITIREA răspunsului de către JS, nu procesarea pe server — un bot îl ignoră complet. În v1
    verificarea exista numai la bootstrap, deci `/chat` (calea care cheltuie LLM) era descoperită.

    Se cheamă în DOUĂ etape, și asta e deliberat. Fără `resolved`, poarta e allowlistul GLOBAL —
    ieftină, rulează ÎNAINTE de orice lookup de tenant, ca un origin de gunoi să nu atingă DB-ul
    (failure matrix: „zero lookup tenant data"). Cu `resolved`, poarta se STRÂNGE pe allowlistul
    canalului. Globalul e prin construcție reuniunea originilor tenanților — e aceeași listă pe
    care o folosește middleware-ul CORS al procesului, deci un origin absent din ea n-ar funcționa
    în browser oricum.
    """
    raw = request.headers.get("origin")
    ok, reason = check_origin(raw, _allowlist_for(resolved))
    if not ok:
        # Originul brut poate identifica tenantul → în log intră doar bucketul.
        log.warning("web_origin_rejected reason=%s bucket=%s", reason, origin_bucket(raw))
        raise HTTPException(status_code=403, detail="origin not allowed")
    return normalize_origin(raw)


def _enforce_demo_access(request: Request) -> None:
    """Poarta de acces la site-ul demo. Validată EXPLICIT sau deloc — niciodată ignorată tăcut.

    Nu produce identitate: `verify_demo_access` întoarce `(ok, reason)`, fără claims. Cine e
    clientul rămâne treaba lui `id_token` din body (NX-129), singurul transport canonic în v2."""
    s = get_settings()
    if not s.web_demo_access_enabled:
        return
    ok, reason = verify_demo_access(
        request.headers.get("authorization"),
        s.web_demo_access_secret,
        leeway_s=s.web_demo_access_leeway_s,
        issuer=s.web_demo_access_issuer or None,
        audience=s.web_demo_access_audience or None,
    )
    if not ok:
        log.warning("web_demo_access_rejected reason=%s", reason)
        raise HTTPException(status_code=401, detail="access denied")


def _enforce_session_origin(session: WebSession, origin: str | None) -> None:
    """O sesiune v2 legată de un origin nu poate fi folosită de pe altul.

    Sesiunile v1 (`claims is None`) n-au legătură de origin și trec — asta e overlapul, nu o
    scăpare: cutoverul se face cu `WEB_SESSION_V2_REQUIRED`, după ce v1 expiră natural."""
    claims = session.claims
    if claims is None or not claims.origin:
        return
    if origin and claims.origin != origin:
        log.warning("web_session_rejected reason=origin_mismatch bucket=%s", origin_bucket(origin))
        raise HTTPException(status_code=403, detail="invalid session")


def _apply_identity(event: InboundEvent, session: WebSession, id_token: str | None) -> None:
    """NX-129 — login passthrough: verifică `id_token`-ul opțional (JWT HS256) la marginea web și
    marchează envelope-ul cu `verified_customer_ref` (worker-ul rezolvă contactul verificat pe el).
    Feature OFF / fără token / tenant fără `identity_secret` → no-op (vizitator anonim). Un token
    invalid/expirat → NU blochează chat-ul (rămâne anonim); motivul respingerii merge în payload
    pentru observabilitate (worker-ul emite `web_identity_rejected`)."""
    s = get_settings()
    if not (s.web_identity_enabled and id_token and session.identity_secret):
        return
    customer_ref, reject = verify_identity_token(
        id_token, session.identity_secret, leeway_s=s.web_identity_leeway_s
    )
    event.verified_customer_ref = customer_ref
    if reject:
        event.payload = {**event.payload, "identity_rejected": reject}


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
    _enforce_demo_access(request)
    # Etapa 1: allowlist GLOBAL, înainte de orice atingere de DB (failure matrix: un origin
    # nepermis nu declanșează lookup de date de tenant).
    origin = _enforce_origin(request)
    resolved = await _resolve_token(token)
    if resolved is None:
        raise HTTPException(status_code=403, detail="unknown token")
    # Etapa 2: acum știm tenantul → poarta se strânge pe allowlistul CANALULUI, dacă declară unul.
    _enforce_origin(request, resolved)
    # NX-229: bootstrapul era NELIMITAT — un atacator putea coase oricâte visitor_id-uri proaspete
    # ca să ocolească limita per-visitor de pe /chat. Cheie pe tenant+IP: `visitor_id` încă nu
    # există aici, iar IP-ul nu intră în clar (bucket hash-uit, P12).
    s = get_settings()
    redis = await get_redis()
    ip = request.client.host if request.client else "unknown"
    try:
        minted = await incr_window(
            redis, f"webrl:boot:{token}:{visitor_bucket(ip)}", s.web_rate_limit_window_s
        )
    except (RedisError, OSError):
        # Emiterea de sesiuni e ieftină, dar e poarta de intrare: fail-CLOSED, ca /chat.
        raise HTTPException(status_code=429, detail="rate limited") from None
    if minted > s.web_bootstrap_rate_limit_max:
        raise HTTPException(status_code=429, detail="rate limited")
    if s.web_session_v2_enabled:
        visitor_id, sig = issue_session_v2(
            token,
            resolved["session_secret"],
            ttl_s=s.web_session_ttl_s,
            origin=origin if s.web_session_origin_binding else None,
        )
    else:
        visitor_id, sig = issue_visitor(token, resolved["session_secret"])
    return {"token": token, "visitor_id": visitor_id, "sig": sig, "sse_url": "/web/stream"}


@router.post("/messages")
async def web_message(req: WebMessageIn, request: Request) -> dict:
    """Client → bot: verifică sesiunea + rate limit → envelope neutru pe stream. NU trimite reply
    (ăla iese prin outbox → dispatcher → WebSender, P5)."""
    await enforce_body_cap(request, get_settings().web_max_body_bytes)  # NX-120
    _enforce_demo_access(request)
    origin = _enforce_origin(request)  # NX-229: lipsea complet pe această cale
    session = await _verify(req.token, req.visitor_id, req.sig)
    if session is None:
        raise HTTPException(status_code=403, detail="invalid session")
    _enforce_session_origin(session, origin)
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
    _apply_identity(event, session, req.id_token)  # NX-129: login passthrough (opțional)
    await enqueue_inbound(redis, event.to_dict())
    return {"accepted": True, "msg_id": event.provider_msg_id}


# ── NX-232: ledgerul durabil al turelor web (flag WEB_TURN_LEDGER_ENABLED) ─────────────────
# Idempotency + replay pe calea SINCRONĂ. Fluxul: accept (insert-or-inspect) → claim (lease +
# epoch) → pipeline → finalizare ÎN tranzacția TurnCommit (rezultat + mesaj + stare împreună
# sau deloc). Duplicatul NU mai produce content gol: terminal → replay EXACT; activ → status +
# turn ID; același ID cu alt input → 409. Endpointurile dedicate 202/GET sunt NX-233.


def _valid_turn_uuid(value: str | None) -> bool:
    """Ledgerul cere un `client_turn_id` UUID real (coloană uuid; frontendul îl generează și
    îl persistă). Fără el nu există cheie de replay → calea veche, nu o eroare."""
    if not value:
        return False
    try:
        UUID(value)
    except ValueError:
        return False
    return True


def _in_progress_response(row: WebTurnRow) -> dict:
    """Duplicat cu procesare ACTIVĂ (alt tab / crash nerecuperat încă): statusul canonic +
    turn ID — clientul știe CE așteaptă, nu primește un blank mut. Ne-terminal, deci P6 nu
    cere conținut comercial; `running` NU iese pe sârmă (proiecția e singurul owner)."""
    return {
        "content": "",
        "products": [],
        "suggestions": [],
        "turn": {
            "id": row.id,
            "client_turn_id": row.client_turn_id,
            "status": project_wire_status(row.status),
        },
    }


async def _ledger_emit(db, row: WebTurnRow, *events: Event) -> None:
    """Evenimentele ledgerului pe traiectoria conversației (best-effort, ca persist_events).
    P12: doar id-uri + etichete low-cardinality, zero body/token/visitor."""
    await persist_events(
        db,
        row.business_id,
        row.conversation_id,
        row.contact_id,
        list(events),
        operation="web_turn_events",
    )


async def _ledger_refresh(db, row: WebTurnRow) -> WebTurnRow | None:
    """Re-citește rândul turului (după fenced/race/deduped) — autoritatea e DB-ul."""
    async with db("web_turn_get") as conn:
        return await get_turn(conn, row.business_id, row.conversation_id, row.client_turn_id)


async def _replay_or_status(db, row: WebTurnRow) -> dict:
    """Rezultatul canonic pentru un turn pe care NU noi l-am finalizat: terminal → replay
    exact (`response_json` persistat); altfel → status + turn ID."""
    refreshed = await _ledger_refresh(db, row) or row
    if refreshed.status in TERMINAL_LEDGER_STATUSES and refreshed.response_json is not None:
        await _ledger_emit(
            db,
            refreshed,
            Event("web_turn_replayed", {"status": refreshed.status, "web_turn_id": refreshed.id}),
        )
        return refreshed.response_json
    return _in_progress_response(refreshed)


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
    _enforce_demo_access(request)
    # NX-229: calea care CHELTUIE LLM n-avea nicio verificare de origin. Un bot cu tokenul public
    # (care e public prin definiție) putea arde bugetul de pe orice pagină.
    origin = _enforce_origin(request)
    session = await _verify(req.token, req.visitor_id, req.sig)
    if session is None:
        raise HTTPException(status_code=403, detail="invalid session")
    _enforce_session_origin(session, origin)
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
    event_obj = InboundEvent(
        channel_kind="webchat",
        channel_account_id=req.token,
        sender_external_id=req.visitor_id,
        provider_msg_id=req.client_msg_id or str(uuid4()),
        content_type="text",
        body=req.message.strip(),
    )
    _apply_identity(event_obj, session, req.id_token)  # NX-129: login passthrough (opțional)
    event = event_obj.to_dict()
    s = get_settings()
    # NX-221: serializare ture per conversație — calea sincronă rulează pipeline-ul IN-PROCESS,
    # deci lock-ul NX-85 dintre replicile de consumer NU o acoperă. Lock Redis scurt la margine
    # (P10: wait-ul se măsoară aici, stagiile nu știu), cheie tenant+expeditor (P7; identitatea e
    # proxy 1:1 pt conversația deschisă — conversation_id nu e cunoscut fără round-trip în DB).
    # Neobținut în fereastră / Redis jos → BYPASS: procesăm oricum (principiul 6 — un lock blocat
    # nu lasă clientul fără răspuns); plasa rămâne optimistic lock-ul din processor (NX-221 fix 2).
    lock = None
    if s.web_turn_lock_enabled:
        sender_key = f"webchat:{req.token}:{event_obj.verified_customer_ref or req.visitor_id}"
        lock = await acquire_turn_lock(
            redis,
            session.business_id,
            sender_key,
            ttl_ms=s.turn_lock_ttl_ms,
            wait_max_ms=s.turn_lock_wait_max_ms,
        )
        if not lock.acquired:
            event["turn_lock_bypass"] = True
        if lock.waited_ms > 0:
            event["turn_lock_wait_ms"] = lock.waited_ms
    db = tenant_db(session.business_id)
    admission = get_admission(redis) if s.web_admission_enabled else None
    slot = None
    ledger_row: WebTurnRow | None = None
    ledger_epoch: int | None = None
    persisted: dict = {}  # umplut de commit hook cu view-ul EXACT persistat (replay identic)
    try:
        async with db("load_business") as conn:
            business = await load_business(conn, session.business_id)
        if business is None:
            raise HTTPException(status_code=503, detail="business unavailable")
        # NX-232 — acceptul durabil, ÎNAINTE de cost guard/admission: un duplicat terminal se
        # REJOACĂ din ledger (zero LLM, zero slot de admission); un conflict e 409 imediat.
        if s.web_turn_ledger_enabled and _valid_turn_uuid(req.client_msg_id):
            fingerprint = request_fingerprint(
                s.web_turn_fingerprint_secret,
                business_id=session.business_id,
                channel_token=req.token,
                text=event_obj.body,
            )
            outcome = await accept_web_turn(
                db,
                business_id=session.business_id,
                channel_id=channel["channel_id"],
                channel_kind="webchat",
                channel_token=req.token,
                sender_external_id=event_obj.verified_customer_ref or req.visitor_id,
                client_turn_id=req.client_msg_id,
                fingerprint=fingerprint,
                verified=bool(event_obj.verified_customer_ref),
                locale=business.default_locale,
            )
            if isinstance(outcome, IdempotencyConflict):
                await _ledger_emit(
                    db,
                    outcome.row,
                    Event("web_turn_idempotency_conflict", {"web_turn_id": outcome.row.id}),
                )
                raise HTTPException(status_code=409, detail="idempotency_conflict")
            if isinstance(outcome, ActiveTurnConflict):
                # Policy explicit single-flight: alt turn e activ pe conversație → respingem
                # onest (frontendul ține oricum inputul inactiv până la terminal, NX-243/245).
                raise HTTPException(status_code=409, detail="active_turn")
            if isinstance(outcome, ExistingCompleted | ExistingInProgress):
                await _ledger_emit(
                    db,
                    outcome.row,
                    Event("web_turn_accepted", {"mode": "existing", "web_turn_id": outcome.row.id}),
                )
                return await _replay_or_status(db, outcome.row)
            assert isinstance(outcome, Accepted)
            ledger_row = outcome.row
            claim = await claim_web_turn(
                db,
                session.business_id,
                ledger_row.id,
                owner=str(uuid4()),
                lease_ttl_s=s.web_turn_lease_ttl_s,
            )
            if claim is None:
                # Race îngust: alt proces a revendicat între insert și claim → el e autoritatea.
                return await _replay_or_status(db, ledger_row)
            ledger_epoch = claim.lease_epoch
            claim_events = [
                Event("web_turn_accepted", {"mode": "new", "web_turn_id": ledger_row.id}),
                Event(
                    "web_turn_claimed",
                    {"attempt_bucket": attempt_bucket(claim.attempt), "web_turn_id": ledger_row.id},
                ),
            ]
            if claim.reclaimed:
                claim_events.append(Event("web_turn_reclaimed", {"web_turn_id": ledger_row.id}))
            await _ledger_emit(db, ledger_row, *claim_events)
        # NX-120: gard de admitere de cost ÎNAINTE de pipeline — nu cheltui LLM dacă bugetul
        # tenantului SAU al vizitatorului e atins. Redis căzut → fail-CLOSED (429), consecvent
        # cu rate-limit-ul. (Precizia per-tur + cap per-contact pe canalele async = NX-125.)
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
        # NX-231: ACEEAȘI poartă de admission ca workerul (lease-uri distribuite, plafon global +
        # per-tenant). Ruta sincronă cheltuie LLM in-process, deci fără ea un burst pe web ocolea
        # complet frâna sistemului. Zero conexiune ținută cât așteptăm un slot. Peste deadline →
        # 429 ONEST cu `Retry-After` (echivalentul web al re-queue-ului: niciun payload gol, P6);
        # store indisponibil → fallback local BOUNDED, nu bypass tăcut.
        if admission is not None:
            slot = await admission.acquire(session.business_id, s.admission_web_wait_ms / 1000.0)
            if not slot.admitted:
                log.warning(
                    "web admission: respins (%s) bucket=%s",
                    slot.reason,
                    tenant_bucket(session.business_id),
                )
                raise HTTPException(
                    status_code=429,
                    detail="busy",
                    headers={"Retry-After": "1"},
                )
            if slot.wait_ms > 0:
                event["admission_wait_ms"] = round(slot.wait_ms, 1)
            if slot.degraded:
                event["admission_degraded"] = True

        async def _ledger_commit(conn, reply, language):
            """NX-232: finalizarea rândului din ledger, ÎN tranzacția TurnCommit. View-ul
            persistat e reținut și SERVIT ca răspuns — replay-ul de mai târziu e identic."""
            view = render_web(reply, language or "ro")
            await complete_web_turn_on_conn(
                conn,
                session.business_id,
                ledger_row.id,
                lease_epoch=ledger_epoch,
                view=view,
            )
            persisted["view"] = view

        try:
            result = await handle_turn(
                db,
                business,
                channel["channel_id"],
                event,
                redis=redis,
                deliver=False,
                defer_aftercare=True,
                # kwarg-ul apare DOAR cu ledger activ — seam-urile vechi (fake handle_turn din
                # teste) rămân compatibile byte-identic pe calea fără flag.
                **({"commit_hook": _ledger_commit} if ledger_epoch is not None else {}),
            )
        except FencedTurnCompletion:
            # Alt owner a reclamat lease-ul cât timp calculam: rezultatul NOSTRU a fost aruncat
            # (rollback complet — mesaj + stare + rezultat), al LUI e autoritatea.
            await _ledger_emit(
                db,
                ledger_row,
                Event("web_turn_fenced_completion_rejected", {"web_turn_id": ledger_row.id}),
            )
            return await _replay_or_status(db, ledger_row)
        except EmptyTerminalResult:
            # P6: terminal fără conținut randabil → NU se comite drept succes; persistăm un
            # error-view SAFE și îl servim — niciodată blank pe o cale terminală.
            lang = business.default_locale or "ro"
            await fail_web_turn(
                db,
                session.business_id,
                ledger_row.id,
                lease_epoch=ledger_epoch,
                code="empty_result",
                language=lang,
            )
            return error_view("empty_result", lang)
    finally:
        if admission is not None and slot is not None:
            await admission.release(slot)
        # Release DUPĂ ce turul (reply + patch de state) e persistat, dar ÎNAINTE de aftercare
        # (LLM de fundal — nu ținem turul următor blocat pe muncă best-effort). TTL = plasă.
        if lock is not None:
            await release_turn_lock(redis, lock)
    # Conexiunile turului sunt DEJA înapoi în pool: fiecare fază și-a luat una scurtă.
    pipeline_cost_usd = (
        result.aftercare.ctx.usage.cost_usd
        if result.aftercare is not None and result.aftercare.ctx.usage is not None
        else 0.0
    )
    post_turn_cost_usd = 0.0
    # Aftercare uses separate DB checkouts. Its real cost is added to the web visitor cap below.
    if result.aftercare is not None:
        post_turn_cost_usd = await run_aftercare(db, redis, result.aftercare)
    try:
        await web_cost_add_visitor(
            redis,
            session.business_id,
            req.visitor_id,
            pipeline_cost_usd + post_turn_cost_usd,
        )
    except Exception:  # noqa: BLE001 - the reply is more important than telemetry
        log.warning("web_cost_add_visitor failed after the reply was built")
    if ledger_epoch is not None:
        if "view" in persisted:
            # Exact ce s-a persistat în tranzacția de commit — replay-ul va fi byte-identic.
            return persisted["view"]
        if result.deduped:
            # Claimul durabil (inbound_dedupe) e încă ținut de un tur crăpat recent — fereastra
            # CLAIM_TTL_S. Ledgerul răspunde cu statusul canonic, NU cu blank (fix-ul NX-232);
            # retry-ul de după expirare reclamă ambele și rulează. Recovery activ = NX-233.
            return await _replay_or_status(db, ledger_row)
        if result.reply is not None:
            # Plasă (nu ar trebui atinsă): reply comis fără view persistat — servim reply-ul;
            # rândul rămâne `running` și expiră în lease, vizibil în metrici, nu tăcut.
            log.warning("web_turn %s: reply fără view persistat în ledger", ledger_row.id)
            return _build_chat_response(result)
        # Tur fără reply (halt/no-reply): pe calea sincronă cu ledger, terminalul e ONEST —
        # un error-view safe persistat, nu tăcere (P6).
        lang = result.language or business.default_locale or "ro"
        await fail_web_turn(
            db,
            session.business_id,
            ledger_row.id,
            lease_epoch=ledger_epoch,
            code="empty_result",
            language=lang,
        )
        return error_view("empty_result", lang)
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
    _enforce_demo_access(request)
    origin = _enforce_origin(request)  # NX-229: lipsea și aici
    session = await _verify(token, visitor_id, sig)
    if session is None:
        raise HTTPException(status_code=403, detail="invalid session")
    _enforce_session_origin(session, origin)
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


# ── NX-233: rutele v2 — accept durabil, status/result și SSE (flag WEB_TURN_V2_ENABLED) ─────
# Requestul HTTP DOAR acceptă/notifică: pipeline-ul, LLM-ul și tool-urile rulează în executorul
# separat (src/web/turn_executor.py), iar GET/SSE sunt PROIECȚII ale ledgerului (autoritatea).
# Auth: aceleași (token, visitor_id, sig) ca /web/stream, în query — EventSource nu poate seta
# headere, iar POST-ul rămâne simetric; corpul e EXACT `WebTurnRequestV2` (NX-228, extra=forbid).


def _v2_error(status_code: int, code: str, message: str, **extra) -> JSONResponse:
    """Eroare TERMINALĂ STRUCTURATĂ (înainte de accept: auth/schema/admission). `code` e stabil
    și low-cardinality; `message` e al nostru — niciodată ecoul inputului sau al excepției."""
    return JSONResponse(
        status_code=status_code, content={"error": {"code": code, "message": message}, **extra}
    )


async def _v2_gate(request: Request, token: str, visitor_id: str, sig: str) -> WebSession:
    """Poarta comună a rutelor v2: flag → demo access → origin → sesiune → legare de origin.
    404 cu flag OFF (ruta nu există încă), nu 403 — nu confirmăm existența feature-ului."""
    if not get_settings().web_turn_v2_enabled:
        raise HTTPException(status_code=404, detail="not found")
    _enforce_demo_access(request)
    origin = _enforce_origin(request)
    session = await _verify(token, visitor_id, sig)
    if session is None:
        raise HTTPException(status_code=403, detail="invalid session")
    _enforce_session_origin(session, origin)
    return session


def _v2_status_response(row: WebTurnRow, *, phase: str | None = None) -> JSONResponse:
    s = get_settings()
    return JSONResponse(
        status_code=202,
        content=status_payload(
            row,
            phase=phase,
            poll_after_ms=s.web_turn_poll_after_ms,
            sse_enabled=s.web_turn_sse_enabled,
        ),
    )


async def _v2_business(db, business_id: str):
    async with db("load_business") as conn:
        business = await load_business(conn, business_id)
    if business is None:
        raise HTTPException(status_code=503, detail="business unavailable")
    return business


@router.post("/v2/turns")
async def web_turn_accept_v2(request: Request, token: str, visitor_id: str, sig: str):
    """Acceptul DURABIL al unui turn (NX-233): validează, scrie rândul de ledger + inputul SAFE
    în același checkout, trezește executorii (best-effort, DUPĂ commit) și răspunde imediat.
    NICIODATĂ pipeline/LLM/tool aici — recovery-ul (refresh, alt tab, worker mort) e din DB."""
    s = get_settings()
    raw = await enforce_body_cap(request, s.web_max_body_bytes)
    session = await _v2_gate(request, token, visitor_id, sig)
    redis = await get_redis()
    ip = request.client.host if request.client else "unknown"
    # Acceptul pune LLM la coadă → aceleași gărzi fail-CLOSED ca /web/chat (NX-120).
    if await web_rate_limited(redis, token, ip, visitor_id, fail_closed=True):
        raise HTTPException(status_code=429, detail="rate limited")
    # Schema (eroare terminală STRUCTURATĂ, înainte de accept — singurele permise: auth/schema/
    # admission). Mesajul e al nostru: nu ecoul body-ului, nu excepția Pydantic (poate conține
    # inputul clientului — el l-a trimis, dar logurile/răspunsurile nu-l repetă).
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _v2_error(400, "invalid_json", "corpul nu este JSON valid")
    try:
        turn_req: WebTurnRequestV2 = parse_turn_request(payload)
    except (ValueError, TypeError):
        return _v2_error(422, "schema_invalid", "corpul nu respectă contractul web-turn.v2")
    if turn_req.input.type == "action":
        # Tokenurile opace semnate sunt NX-236 — până atunci, refuz onest, nu interpretare.
        return _v2_error(422, "action_not_supported", "acțiunile opace nu sunt încă acceptate")

    pool = await get_pool()
    async with admin_conn(pool) as conn:
        channel = await resolve_channel(conn, "webchat", token)
    if channel is None:
        raise HTTPException(status_code=403, detail="unknown channel")
    db = tenant_db(session.business_id)
    business = await _v2_business(db, session.business_id)
    # Gard de cost ÎNAINTE de accept (NX-120, fail-CLOSED): nu punem la coadă muncă pe care
    # bugetul tenantului/vizitatorului n-o mai acoperă.
    cap = business.daily_cost_cap_usd or s.daily_cost_cap_usd
    try:
        over_budget = await cost_over_budget(
            redis, session.business_id, cap
        ) or await web_cost_over_visitor_cap(
            redis, session.business_id, visitor_id, s.web_cost_cap_per_visitor_usd
        )
    except (RedisError, OSError):
        over_budget = True
    if over_budget:
        raise HTTPException(status_code=429, detail="budget exceeded")

    text = turn_req.input.text.strip()
    # NX-129: identitate verificată opțională (JWT host-signed) — contactul se rezolvă pe ea.
    verified_ref: str | None = None
    if s.web_identity_enabled and turn_req.id_token and session.identity_secret:
        verified_ref, _reject = verify_identity_token(
            turn_req.id_token, session.identity_secret, leeway_s=s.web_identity_leeway_s
        )
    # NX-230 — frontiera de privacy LA ACCEPT: doar forma SAFE atinge discul. Executorul
    # rulează pipeline-ul pe aceeași formă persistată → recovery determinist (același input,
    # indiferent cine și când reia turul).
    _raw_inbound, safe_inbound = apply_boundary(text)
    if safe_inbound.degraded:
        log.warning("privacy_guard_failed stage=web_v2_accept reason=degraded")
    fingerprint = request_fingerprint(
        s.web_turn_fingerprint_secret,
        business_id=session.business_id,
        channel_token=token,
        text=text,
    )
    outcome = await accept_web_turn(
        db,
        business_id=session.business_id,
        channel_id=channel["channel_id"],
        channel_kind="webchat",
        channel_token=token,
        sender_external_id=verified_ref or visitor_id,
        client_turn_id=str(turn_req.client_turn_id),
        fingerprint=fingerprint,
        verified=bool(verified_ref),
        locale=business.default_locale,
        deadline_at=datetime.now(UTC) + timedelta(seconds=s.web_turn_deadline_s),
        # Autorizarea GET/SSE e pe SESIUNEA de browser (token+visitor), nu pe identitatea
        # comercială: exact sesiunea care a pornit turnul îl poate citi.
        session_ref=session_ref_hash(token, visitor_id),
        persist_inbound=True,
        safe_body=safe_inbound.text,
    )
    lang = business.default_locale or "ro"
    if isinstance(outcome, IdempotencyConflict):
        await _ledger_emit(
            db,
            outcome.row,
            Event("web_turn_idempotency_conflict", {"web_turn_id": outcome.row.id}),
        )
        return _v2_error(409, "idempotency_conflict", "același ID cu alt conținut")
    if isinstance(outcome, ActiveTurnConflict):
        extra = {}
        if outcome.active is not None:
            extra["active_turn"] = status_payload(
                outcome.active,
                poll_after_ms=s.web_turn_poll_after_ms,
                sse_enabled=s.web_turn_sse_enabled,
            )
        return _v2_error(
            409,
            "conversation_turn_in_progress",
            "conversația are deja un turn activ",
            **extra,
        )
    if isinstance(outcome, ExistingCompleted):
        await _ledger_emit(
            db,
            outcome.row,
            Event(
                "web_turn_replayed",
                {"status": outcome.row.status, "web_turn_id": outcome.row.id},
            ),
        )
        return JSONResponse(status_code=200, content=terminal_view(outcome.row, lang))
    if isinstance(outcome, ExistingInProgress):
        await _ledger_emit(
            db,
            outcome.row,
            Event("web_turn_accepted", {"mode": "existing", "web_turn_id": outcome.row.id}),
        )
        phase = await get_phase(redis, session.business_id, outcome.row.id)
        return _v2_status_response(outcome.row, phase=phase)
    assert isinstance(outcome, Accepted)
    # Wake DUPĂ ce acceptul e comis (checkout-ul s-a închis) — niciodată înainte. Best-effort:
    # pierderea lui e acoperită de scanul executorului + sweeper (DB = autoritatea).
    await wake_executor(redis, session.business_id, outcome.row.id)
    await _ledger_emit(
        db, outcome.row, Event("web_turn_accepted", {"mode": "new", "web_turn_id": outcome.row.id})
    )
    return _v2_status_response(outcome.row)


def _v2_turn_or_404(turn_id: str) -> str:
    """Ruta cere un UUID real; orice altceva e indistinct de „nu există"."""
    try:
        UUID(turn_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="not found") from None
    return turn_id


@router.get("/v2/turns/{turn_id}")
async def web_turn_status_v2(turn_id: str, token: str, visitor_id: str, sig: str, request: Request):
    """Statusul sau EXACT rezultatul terminal persistat (proiecția `web-view.v2` a aceluiași
    rând, determinist — replay byte-identic). Reautorizează tenant + sesiune la FIECARE citire;
    alt tenant/vizitator → 404 indistinct de inexistent."""
    session = await _v2_gate(request, token, visitor_id, sig)
    _v2_turn_or_404(turn_id)
    db = tenant_db(session.business_id)
    row = await get_turn_for_session(
        db,
        business_id=session.business_id,
        turn_id=turn_id,
        channel_token=token,
        visitor_id=visitor_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    if row.status in TERMINAL_LEDGER_STATUSES:
        business = await _v2_business(db, session.business_id)
        return JSONResponse(
            status_code=200, content=terminal_view(row, business.default_locale or "ro")
        )
    redis = await get_redis()
    return _v2_status_response(row, phase=await get_phase(redis, session.business_id, row.id))


@router.get("/v2/turns/{turn_id}/events")
async def web_turn_events_v2(
    turn_id: str,
    token: str,
    visitor_id: str,
    sig: str,
    request: Request,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    """SSE opțional (flag separat): PROIECȚIE a ledgerului — doar schimbări REALE de status,
    heartbeat tehnic rar și rezultatul terminal deja comis. Fără tokeni LLM, fără draft, fără
    chain-of-thought (nu există sursă pentru ele aici). `Last-Event-ID` reia strict crescător;
    GET rămâne fallback-ul autoritativ. Zero conexiune DB ținută: fiecare poll e checkout scurt."""
    s = get_settings()
    if not s.web_turn_sse_enabled:
        raise HTTPException(status_code=404, detail="not found")
    session = await _v2_gate(request, token, visitor_id, sig)
    _v2_turn_or_404(turn_id)
    db = tenant_db(session.business_id)
    first = await get_turn_for_session(
        db,
        business_id=session.business_id,
        turn_id=turn_id,
        channel_token=token,
        visitor_id=visitor_id,
    )
    if first is None:
        raise HTTPException(status_code=404, detail="not found")
    business = await _v2_business(db, session.business_id)
    lang = business.default_locale or "ro"
    redis = await get_redis()
    try:
        last_sent = int(last_event_id) if last_event_id else -1
    except ValueError:
        last_sent = -1

    async def gen():
        sent = last_sent
        row = first
        started = monotonic()
        last_emit = monotonic()
        while True:
            if row.status in TERMINAL_LEDGER_STATUSES:
                # Rezultatul deja comis, O dată: un client care l-a primit (Last-Event-ID=3)
                # nu-l primește dublu la reconectare — GET rămâne calea de re-citire.
                ordinal, frame = result_event(row, lang)
                if ordinal > sent:
                    yield frame
                return
            phase = await get_phase(redis, session.business_id, row.id)
            ordinal, frame = status_event(row, phase)
            if ordinal > sent:
                yield frame
                sent = ordinal
                last_emit = monotonic()
            elif monotonic() - last_emit >= s.web_sse_heartbeat_s:
                yield ": keepalive\n\n"
                last_emit = monotonic()
            if await request.is_disconnected():
                return
            if monotonic() - started >= s.web_turn_sse_max_s:
                return  # sesiune bounded; clientul reconectează cu Last-Event-ID / face GET
            await asyncio.sleep(s.web_turn_sse_poll_ms / 1000.0)
            async with db("web_turn_get") as conn:
                fresh = await get_turn_by_id(conn, session.business_id, row.id)
            if fresh is None:
                return  # șters (retenție/GDPR) — GET-ul va spune 404; nu inventăm status
            row = fresh

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
