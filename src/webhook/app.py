"""FastAPI app — punctul de INTRARE al sistemului (webhook Meta).

GET /webhook  → handshake de verificare Meta (token).
POST /webhook → mesaje inbound: verifică semnătura, deduplică (Redis layer 1),
                pune pe stream, ACK 200 rapid (<50ms). Rezolvarea business/
                contact/conversație + plasa de dedupe durabilă sunt în worker.
"""

import json
import os

from fastapi import Depends, FastAPI, Query, Request, Response
from fastapi.responses import PlainTextResponse
from redis.asyncio import Redis
from redis.exceptions import RedisError

from src.config import get_settings
from src.redis_bus import enqueue_inbound, get_redis, seen_before
from src.webhook.meta import parse_statuses, parse_webhook
from src.webhook.signature import verify_meta_signature

app = FastAPI(title="Nativx Assistant — webhook")


# --- dependențe (injectabile/overridabile în teste) --------------------------


def get_app_secret() -> str:
    return get_settings().meta_app_secret


async def redis_dep() -> Redis:
    return await get_redis()


# --- endpoints ---------------------------------------------------------------


@app.get("/webhook")
def verify_webhook(
    hub_mode: str | None = Query(default=None, alias="hub.mode"),
    hub_verify_token: str | None = Query(default=None, alias="hub.verify_token"),
    hub_challenge: str | None = Query(default=None, alias="hub.challenge"),
) -> Response:
    """Handshake-ul de verificare Meta.

    Meta trimite GET cu hub.mode=subscribe, hub.verify_token, hub.challenge.
    Dacă token-ul corespunde META_VERIFY_TOKEN → întoarce challenge-ul ca text
    BRUT (nu JSON — Meta compară exact). Altfel → 403.
    """
    expected = os.environ.get("META_VERIFY_TOKEN")
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
      3. mesaje: dedupe layer 1 (Redis SET NX) pe (phone_number_id, wamid)
      4. XADD pe stream-ul de procesare (mesaje + statusuri, cu `kind`)
      5. ACK 200 imediat (Meta oprește retry-ul; restul e async în worker)

    Statusurile NU se deduplică (delivered și read au același wamid). Întoarce
    mereu 200 pe payload valid-semnat, chiar dacă nu conține nimic procesabil —
    altfel Meta reîncearcă inutil.
    """
    raw = await request.body()
    signature = request.headers.get("X-Hub-Signature-256")
    if not verify_meta_signature(app_secret, raw, signature):
        return PlainTextResponse("invalid signature", status_code=403)

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return PlainTextResponse("bad request", status_code=400)

    try:
        for event in parse_webhook(payload):
            if await seen_before(redis, event.phone_number_id, event.provider_msg_id):
                continue  # retry Meta → deja văzut, nu re-enqueua
            await enqueue_inbound(redis, event.to_dict())

        for status in parse_statuses(payload):
            await enqueue_inbound(redis, status.to_dict())
    except RedisError:
        # Redis indisponibil / memorie plină (noeviction → XADD eroare). NU pierdem
        # tăcut: 503 → Meta reîncearcă, iar la retry NX-51 deduplică ce-a prins deja.
        return PlainTextResponse("service unavailable", status_code=503)

    return PlainTextResponse("ok", status_code=200)
