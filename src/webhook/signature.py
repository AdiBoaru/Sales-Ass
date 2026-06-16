"""Verificarea semnăturilor HMAC peste corpul BRUT al cererilor externe.

Atât Meta (`X-Hub-Signature-256`, APP_SECRET) cât și webhook-ul de comenzi
(`X-Orders-Signature`, ORDERS_WEBHOOK_SECRET) semnează fiecare POST cu HMAC-SHA256
peste corpul BRUT. Verificăm înainte de orice parsare — un payload nesemnat corect
nu trebuie nici măcar deserializat (principiul 7: nu avem încredere în input extern).
Avantajul față de un secret-header static: un secret scurs din loguri/proxy NU mai
autentifică nimic, fiindcă atacatorul tot nu poate semna un corp pe care nu-l cunoaște.
"""

import hashlib
import hmac

_PREFIX = "sha256="


def _verify_hmac_sha256(secret: str, raw_body: bytes, signature_header: str | None) -> bool:
    """True dacă `signature_header` e `sha256=<HMAC-SHA256(secret, raw_body)>`.

    Compară în timp constant (anti timing-attack). Fail-closed: fără secret configurat,
    fără antet, sau prefix greșit → False (preferăm să respingem decât să acceptăm
    orbește). `raw_body` trebuie să fie EXACT octeții primiți, nu re-serializați.
    """
    if not secret or not signature_header or not signature_header.startswith(_PREFIX):
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    received = signature_header[len(_PREFIX) :]
    return hmac.compare_digest(expected, received)


def verify_meta_signature(
    app_secret: str,
    raw_body: bytes,
    signature_header: str | None,
) -> bool:
    """Semnătura Meta `X-Hub-Signature-256` peste corpul brut (APP_SECRET)."""
    return _verify_hmac_sha256(app_secret, raw_body, signature_header)


def verify_orders_signature(
    secret: str,
    raw_body: bytes,
    signature_header: str | None,
) -> bool:
    """Semnătura webhook-ului de comenzi `X-Orders-Signature` peste corpul brut (NX-94).

    Identic ca semantică cu `verify_meta_signature` — leagă autentificarea de conținut:
    un secret scurs nu poate fi rejucat fără a semna și un corp valid."""
    return _verify_hmac_sha256(secret, raw_body, signature_header)
