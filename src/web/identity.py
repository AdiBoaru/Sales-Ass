"""NX-129 — verificare JWT host-signed (HS256) pentru login passthrough pe web.

Site-ul gazdă, când randează widget-ul pentru un client AUTENTIFICAT, semnează identitatea lui cu
`identity_secret`-ul per-tenant (din `channels.settings`, separat de `session_secret`) și o pasează
widget-ului ca JWT. Aici îl verificăm la MARGINEA web (ca semnătura de sesiune, NX-20a) → `sub` =
`customer_ref` (id STABIL de client din eshop). Secretul stă pe backend-ul gazdei + control plane,
NICIODATĂ în browser sau pe stream.

Verificare cu STDLIB (hmac/hashlib/base64, ca `session.py`) — fără dependență nouă și cu control
TOTAL pe pinning-ul de algoritm: respingem `alg=none` și confuzia de algoritm (atac clasic JWT).
Funcția NU ridică niciodată pe input ostil → calea web tratează un eșec ca vizitator anonim (P6).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time


def _b64url_decode(seg: str) -> bytes:
    """Base64url → bytes, cu padding restaurat (JWT-urile îl omit). Ridică ValueError pe gunoi."""
    return base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4))


def verify_identity_token(
    token: str, secret: str, *, leeway_s: int = 30
) -> tuple[str | None, str | None]:
    """`(customer_ref, reject_reason)` dintr-un JWT HS256 semnat de gazdă.

    Succes → `(sub, None)`. Orice problemă → `(None, motiv)` cu motiv ∈ {`malformed`, `bad_alg`,
    `bad_signature`, `expired`, `no_sub`}. Verifică, în ordine: structura (3 segmente, base64url,
    JSON), `alg=HS256` DUR (anti `alg=none`), semnătura HMAC-SHA256 în timp constant, `exp`
    obligatoriu + neexpirat (cu leeway), `sub` ne-gol. Nu ridică niciodată (input ostil)."""
    if not token or not secret:
        return None, "malformed"
    parts = token.split(".")
    if len(parts) != 3:
        return None, "malformed"
    header_b64, payload_b64, sig_b64 = parts
    try:
        header = json.loads(_b64url_decode(header_b64))
        payload = json.loads(_b64url_decode(payload_b64))
        signature = _b64url_decode(sig_b64)
    except (ValueError, json.JSONDecodeError):
        return None, "malformed"
    # Pin DUR algoritmul la HS256 → respinge `alg=none` și confuzia de algoritm (atac clasic JWT).
    if not isinstance(header, dict) or header.get("alg") != "HS256":
        return None, "bad_alg"
    expected = hmac.new(
        secret.encode(), f"{header_b64}.{payload_b64}".encode(), hashlib.sha256
    ).digest()
    if not hmac.compare_digest(expected, signature):  # timp constant (anti timing-attack)
        return None, "bad_signature"
    if not isinstance(payload, dict):
        return None, "malformed"
    exp = payload.get("exp")
    # `exp` OBLIGATORIU: un token fără expirare = replay infinit (token furat valabil pe veci).
    if not isinstance(exp, (int, float)) or isinstance(exp, bool):
        return None, "expired"
    if time.time() > float(exp) + leeway_s:
        return None, "expired"
    sub = payload.get("sub")
    if not isinstance(sub, str) or not sub.strip():
        return None, "no_sub"
    return sub.strip(), None
