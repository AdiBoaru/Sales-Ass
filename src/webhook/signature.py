"""Verificarea semnăturii Meta (X-Hub-Signature-256).

Meta semnează fiecare POST cu HMAC-SHA256 peste corpul BRUT al cererii, folosind
APP_SECRET. Verificăm înainte de orice parsare — un payload nesemnat corect nu
trebuie nici măcar deserializat (principiul 7: nu avem încredere în input extern).
"""

import hashlib
import hmac

_PREFIX = "sha256="


def verify_meta_signature(
    app_secret: str,
    raw_body: bytes,
    signature_header: str | None,
) -> bool:
    """True dacă `signature_header` e HMAC-SHA256 valid peste `raw_body`.

    Compară în timp constant (anti timing-attack). Fără secret configurat sau
    fără antet → False (fail-closed): preferăm să respingem decât să acceptăm
    orbește. `raw_body` trebuie să fie EXACT octeții primiți, nu re-serializați.
    """
    if not app_secret or not signature_header or not signature_header.startswith(_PREFIX):
        return False
    expected = hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    received = signature_header[len(_PREFIX) :]
    return hmac.compare_digest(expected, received)
