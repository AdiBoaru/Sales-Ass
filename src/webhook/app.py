"""FastAPI app — punctul de INTRARE al sistemului (webhook Meta).

GET /webhook  → handshake de verificare Meta (token).
POST /webhook → mesaje inbound: verifică semnătura, deduplică (Redis layer 1),
                pune pe stream, ACK 200 rapid (<50ms). Rezolvarea business/
                contact/conversație + plasa de dedupe durabilă sunt în worker.
"""

import json

from fastapi import Depends, FastAPI, Query, Request, Response
from fastapi.responses import PlainTextResponse
from redis.asyncio import Redis
from redis.exceptions import RedisError

from src.config import get_settings
from src.redis_bus import enqueue_inbound, get_redis, seen_before
from src.webhook.body_limit import enforce_body_cap
from src.webhook.meta import parse_statuses, parse_webhook
from src.webhook.redirect import router as redirect_router
from src.webhook.signature import verify_meta_signature, verify_orders_signature

app = FastAPI(title="Nativx Assistant — webhook")

# NX-162: redirect de atribuire click (/r/{business_id}/{ref_code}) — montat necondiționat
# (funnel-ul de checkout e valabil pe orice canal, nu doar web). Face DB sincron (nu e margine
# subțire ca webhook-ul Meta) — vezi src/webhook/redirect.py.
app.include_router(redirect_router)


@app.middleware("http")
async def _request_size_guard(request: Request, call_next):
    """NX-120: respinge Content-Length peste capul global ÎNAINTE de routing/parsing.
    Închide OOM-ul pe /web/* (FastAPI bufferizează corpul în Pydantic înainte de handler).
    Cap global = max (webhook 256KB); `enforce_body_cap` per-endpoint rafinează (web 16KB)."""
    cl = request.headers.get("content-length")
    if cl is not None:
        try:
            if int(cl) > get_settings().webhook_max_body_bytes:
                return PlainTextResponse("payload too large", status_code=413)
        except ValueError:
            return PlainTextResponse("bad content-length", status_code=400)
    return await call_next(request)


# --- dependențe (injectabile/overridabile în teste) --------------------------


def get_app_secret() -> str:
    return get_settings().meta_app_secret


def get_verify_token() -> str:
    return get_settings().meta_verify_token


def get_orders_secret() -> str:
    return get_settings().orders_webhook_secret


async def redis_dep() -> Redis:
    return await get_redis()


# --- endpoints ---------------------------------------------------------------


@app.get("/webhook")
def verify_webhook(
    hub_mode: str | None = Query(default=None, alias="hub.mode"),
    hub_verify_token: str | None = Query(default=None, alias="hub.verify_token"),
    hub_challenge: str | None = Query(default=None, alias="hub.challenge"),
    expected: str = Depends(get_verify_token),
) -> Response:
    """Handshake-ul de verificare Meta.

    Meta trimite GET cu hub.mode=subscribe, hub.verify_token, hub.challenge.
    Dacă token-ul corespunde META_VERIFY_TOKEN (din settings) → întoarce
    challenge-ul ca text BRUT (nu JSON — Meta compară exact). Altfel → 403.
    """
    if hub_mode == "subscribe" and expected and hub_verify_token == expected:
        return PlainTextResponse(hub_challenge or "", status_code=200)
    return PlainTextResponse("forbidden", status_code=403)


@app.post("/webhook")
async def receive_webhook(
    request: Request,
    app_secret: str = Depends(get_app_secret),
    redis: Redis = Depends(redis_dep),
) -> Response:
    """Primește mesaje inbound de la Meta.

    Pași (toți rapizi, fără LLM, fără DB):
      1. verifică X-Hub-Signature-256 peste corpul BRUT → 403 la eșec
      2. parsează mesaje inbound + update-uri de status
      3. mesaje: dedupe layer 1 (Redis SET NX) pe (channel_account_id, provider_msg_id)
      4. XADD pe stream-ul de procesare (mesaje + statusuri, cu `kind`)
      5. ACK 200 imediat (Meta oprește retry-ul; restul e async în worker)

    Statusurile NU se deduplică (delivered și read au același wamid). Întoarce
    mereu 200 pe payload valid-semnat, chiar dacă nu conține nimic procesabil —
    altfel Meta reîncearcă inutil.
    """
    # NX-120: cap de body ÎNAINTE de a bufferiza/verifica (anti-OOM pe VPS mic). Un corp uriaș
    # e respins cu 413 chiar dacă semnătura ar fi invalidă — nu-l mai citim integral.
    raw = await enforce_body_cap(request, get_settings().webhook_max_body_bytes)
    signature = request.headers.get("X-Hub-Signature-256")
    if not verify_meta_signature(app_secret, raw, signature):
        return PlainTextResponse("invalid signature", status_code=403)

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return PlainTextResponse("bad request", status_code=400)

    try:
        for event in parse_webhook(payload):
            if await seen_before(redis, event.channel_account_id, event.provider_msg_id):
                continue  # retry Meta → deja văzut, nu re-enqueua
            await enqueue_inbound(redis, event.to_dict())

        for status in parse_statuses(payload):
            await enqueue_inbound(redis, status.to_dict())
    except RedisError:
        # Redis indisponibil / memorie plină (noeviction → XADD eroare). NU pierdem
        # tăcut: 503 → Meta reîncearcă, iar la retry NX-51 deduplică ce-a prins deja.
        return PlainTextResponse("service unavailable", status_code=503)

    return PlainTextResponse("ok", status_code=200)


@app.post("/webhook/orders/{business_id}")
async def receive_order(
    business_id: str,
    request: Request,
    secret: str = Depends(get_orders_secret),
    redis: Redis = Depends(redis_dep),
) -> Response:
    """Webhook comenzi (F2-2) → atribuire. Margine SUBȚIRE, fără DB (ca path-ul Meta):
    verifică semnătura HMAC peste corpul brut, parsează, pune un envelope `kind='order'`
    pe stream → worker-ul face atribuirea (`process_order`). Comenzile nu-s evenimente de
    canal, deci `business_id` vine din path (autentificat acum de HMAC — un corp semnat cu
    secretul businessului, NX-94). Verificăm ÎNAINTE de orice parsare (principiul 7)."""
    raw = await enforce_body_cap(request, get_settings().webhook_max_body_bytes)  # NX-120
    signature = request.headers.get("X-Orders-Signature")
    if not verify_orders_signature(secret, raw, signature):
        # NU logăm corpul/header-ul/secretul (P12) — corpul poate conține nume/total.
        return PlainTextResponse("invalid signature", status_code=403)

    try:
        order = json.loads(raw)
    except json.JSONDecodeError:
        return PlainTextResponse("bad request", status_code=400)

    try:
        await enqueue_inbound(redis, {"kind": "order", "business_id": business_id, "order": order})
    except RedisError:
        return PlainTextResponse("service unavailable", status_code=503)
    return PlainTextResponse("ok", status_code=200)


# Gateway web widget (NX-20, E26 — al treilea canal). Montat DOAR dacă web_enabled (V1.5):
# endpointurile /web/* (bootstrap, messages, stream SSE, chat sincron) trăiesc în src/web/app.py.
if get_settings().web_enabled:
    from fastapi.middleware.cors import CORSMiddleware

    from src.web.app import router as web_router

    # CORS pt widget-ul de pe site (browser cross-origin). Preflight-ul (OPTIONS, înainte de body)
    # se gate-uiește pe allowlist-ul de origin-uri (CSV în WEB_CORS_ORIGINS). Gol → fără origin-uri
    # permise (doar same-origin). Endpointurile NE-browser (webhook Meta/orders) n-au Origin →
    # neafectate. Gardele reale rămân server-side: token public + sig HMAC + rate-limit.
    cors_origins = get_settings().web_cors_origins_list
    if cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["Content-Type", "Last-Event-ID"],
        )

    app.include_router(web_router)
