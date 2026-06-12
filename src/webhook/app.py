"""FastAPI app — punctul de INTRARE al sistemului (webhook Meta).

Pentru acum doar GET /webhook (verify handshake Meta). POST-ul cu mesaje,
validarea semnăturii și push-ul în Redis vin în T061+.
"""

import os

from fastapi import FastAPI, Query, Response
from fastapi.responses import PlainTextResponse

app = FastAPI(title="Nativx Assistant — webhook")


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
