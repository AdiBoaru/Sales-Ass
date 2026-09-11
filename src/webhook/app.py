"""FastAPI app — punctul de INTRARE HTTP al sistemului.

Montează: health (necondiționat), redirectul de atribuire, webhook-ul de comenzi
(POST /webhook/orders/{business_id}) și, dacă `web_enabled`, gateway-ul widgetului web.

NX-289: rutele Meta (GET/POST /webhook) au fost ȘTERSE odată cu canalul WhatsApp. Webhook-ul
de comenzi rămâne: comenzile NU sunt evenimente de canal (vin din ecommerce, se atribuie pe
`ref_code`), deci nu depindeau de WhatsApp decât prin vecinătate în fișier.
"""

import json
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import PlainTextResponse
from redis.asyncio import Redis
from redis.exceptions import RedisError

from src.config import get_settings
from src.observability import bootstrap as observability_bootstrap
from src.redis_bus import enqueue_inbound, get_redis
from src.webhook.body_limit import enforce_body_cap
from src.webhook.health import router as health_router
from src.webhook.redirect import router as redirect_router
from src.webhook.signature import verify_orders_signature


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    """NX-246: observabilitatea se montează O SINGURĂ dată, la pornirea aplicației.

    În `lifespan`, nu la import: bucla de export e un task asyncio, iar la momentul importului nu
    există încă event loop — un `setup()` la import ar instala exporterul și n-ar porni niciodată
    consumatorul, adică o coadă care se umple în tăcere. Config invalidă a picat deja în
    `Settings` (poarta de boot); aici doar instalăm providerul.

    Cu `OBSERVABILITY_ENABLED=false` (default) totul rămâne no-op: `NullSink`, zero coadă, zero
    span — calea fierbinte byte-identică.
    """
    observability_bootstrap.setup(get_settings())
    try:
        yield
    finally:
        # Flush final MĂRGINIT: telemetria nu are voie să țină procesul (vezi `bootstrap.shutdown`).
        await observability_bootstrap.shutdown()


app = FastAPI(title="Nativx Assistant — webhook", lifespan=_lifespan)

# NX-162: redirect de atribuire click (/r/{business_id}/{ref_code}) — montat necondiționat
# (funnel-ul de checkout e valabil pe orice canal, nu doar web). Face DB sincron (nu e o margine
# subțire ca webhook-ul de comenzi) — vezi src/webhook/redirect.py.
app.include_router(redirect_router)

# NX-248: live/startup/ready + vederea de operator. Montate NECONDIȚIONAT și înaintea oricărui
# flag: dacă un flag ar putea stinge health-ul, atunci exact configurația greșită pe care vrei
# s-o diagnostichezi ar fi cea în care nu poți întreba nimic.
app.include_router(health_router)


@app.middleware("http")
async def _request_size_guard(request: Request, call_next):
    """NX-120: respinge Content-Length peste capul global ÎNAINTE de routing/parsing.
    Închide OOM-ul pe /web/* (FastAPI bufferizează corpul în Pydantic înainte de handler).
    Cap global = max (comenzi 256KB); `enforce_body_cap` per-endpoint rafinează (web 16KB)."""
    cl = request.headers.get("content-length")
    if cl is not None:
        try:
            if int(cl) > get_settings().webhook_max_body_bytes:
                return PlainTextResponse("payload too large", status_code=413)
        except ValueError:
            return PlainTextResponse("bad content-length", status_code=400)
    return await call_next(request)


# --- dependențe (injectabile/overridabile în teste) --------------------------


def get_orders_secret() -> str:
    return get_settings().orders_webhook_secret


async def redis_dep() -> Redis:
    return await get_redis()


# --- endpoints ---------------------------------------------------------------


@app.post("/webhook/orders/{business_id}")
async def receive_order(
    business_id: str,
    request: Request,
    secret: str = Depends(get_orders_secret),
    redis: Redis = Depends(redis_dep),
) -> Response:
    """Webhook comenzi (F2-2) → atribuire. Margine SUBȚIRE, fără DB:
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


# Gateway web widget (NX-20, E26 — singurul canal). Montat DOAR dacă web_enabled (V1.5):
# endpointurile /web/* (bootstrap, messages, stream SSE, chat sincron) trăiesc în src/web/app.py.
if get_settings().web_enabled:
    from fastapi.middleware.cors import CORSMiddleware

    from src.web.app import router as web_router

    # CORS pt widget-ul de pe site (browser cross-origin). Preflight-ul (OPTIONS, înainte de body)
    # se gate-uiește pe allowlist-ul de origin-uri (CSV în WEB_CORS_ORIGINS). Gol → fără origin-uri
    # permise (doar same-origin). Endpointurile NE-browser (webhook de comenzi) n-au Origin →
    # neafectate. Gardele reale rămân server-side: token public + sig HMAC + rate-limit.
    from src.web.security import normalize_allowlist

    _s = get_settings()
    # NX-229: originile se NORMALIZEAZĂ înainte de a ajunge în middleware. `https://x:443/` și
    # `https://x` sunt același origin, dar browserul trimite mereu forma canonică — o intrare
    # necanonică în config n-ar prinde niciodată, tăcut. Ce nu se poate canoniza e dropat, deci o
    # greșeală de scriere nu devine „permite orice".
    cors_origins = sorted(normalize_allowlist(_s.web_cors_origins_list))
    if cors_origins:
        # `Authorization` intră în allowlist DOAR dacă poarta de acces demo chiar îl validează
        # (card: „headers numai dacă sunt efectiv validate"). Altfel l-am invita pe client să
        # trimită un credential pe care nu-l citește nimeni — exact situația din v1, unde
        # frontendul trimitea un Supabase JWT ignorat tăcut de backend.
        allow_headers = ["Content-Type", "Last-Event-ID"]
        if _s.web_demo_access_enabled:
            allow_headers.append("Authorization")
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=allow_headers,
        )

    app.include_router(web_router)
