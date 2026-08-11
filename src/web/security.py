"""NX-229 — poarta de margine a web-ului: origin policy, poartă de acces demo, redactare.

Pur: fără DB, fără Redis, fără LLM. Ce e aici se aplică ÎNAINTE ca requestul să atingă pipeline-ul.

**Ce NU e acest modul.** CORS-ul browserului nu e autentificare, iar `widget_public_token` e
public prin definiție (trăiește în bundle-ul site-ului). Amândouă sunt controale de ADMITERE:
reduc suprafața de abuz, nu dovedesc cine ești. Autorizarea reală rămâne semnătura de sesiune
(`session.py`) plus filtrul `business_id` derivat server-side.

**Trei credențiale, trei semantici — niciodată amestecate:**
  1. `Origin` → policy de admitere. Nu e dovadă de identitate: un bot îl poate scrie orice.
  2. `Authorization: Bearer …` → poarta de acces la site-ul demo. Verificată EXPLICIT aici, dar
     `verify_demo_access` întoarce doar `(ok, reason)` — nu întoarce niciodată claims. Structura
     tipului e ce împiedică headerul să devină identitate de cumpărător, nu disciplina.
  3. `id_token` (body) → identitatea shopperului, singurul transport canonic în v2
     (`identity.py`). Nu se citește din headere.

Motivul pentru care (2) nu poate deveni (3) e scris în tip, pentru că exact asta s-a întâmplat în
v1: headerul sosea, nimeni nu-l valida, și „poate e userul logat" e la o linie de cod distanță de
„userul logat".
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from urllib.parse import urlsplit

# Porturile implicite nu apar în headerul `Origin` trimis de browser: `https://x:443` și
# `https://x` sunt același origin, dar ar fi două string-uri diferite într-un allowlist.
_DEFAULT_PORTS = {"https": "443", "http": "80"}

# Scheme acceptate pentru un origin de browser. `http` e permis DOAR ca să putem allowlista
# explicit un `http://localhost:5173` de dezvoltare; nu e implicit nicăieri.
_ALLOWED_ORIGIN_SCHEMES = frozenset({"https", "http"})


def normalize_origin(raw: str | None) -> str | None:
    """`Origin` brut → formă canonică `scheme://host[:port]`, sau `None` dacă nu e un origin valid.

    `None` înseamnă „nu pot canoniza asta", NU „e în regulă". Apelantul decide; vezi `check_origin`.

    Respinge explicit `null` (origin opac: iframe sandboxed, `file://`, unele redirecturi). E o
    valoare pe care browserele chiar o trimit, iar dacă ar ajunge să se compare ca text obișnuit,
    un allowlist care conține din greșeală „null" ar deschide poarta pentru orice context opac.
    """
    if not raw:
        return None
    value = raw.strip()
    if not value or value.lower() == "null":
        return None
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in _ALLOWED_ORIGIN_SCHEMES:
        return None
    # Un origin e DOAR scheme+host+port. Orice path/query/fragment/userinfo înseamnă că nu ne uităm
    # la un origin, ci la un URL — și un URL nu se compară cu un allowlist de origini.
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        return None
    if "@" in parts.netloc:
        return None
    try:
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:  # port ne-numeric
        return None
    if not host:
        return None
    # IPv6 își păstrează parantezele în forma canonică.
    if ":" in host:
        host = f"[{host}]"
    if port is None or str(port) == _DEFAULT_PORTS.get(scheme):
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{port}"


def normalize_allowlist(origins: list[str] | tuple[str, ...] | None) -> frozenset[str]:
    """Allowlist configurat → mulțime canonică. Intrările necanonizabile sunt DROPATE.

    Un origin scris greșit în config nu devine tăcut „permite orice": nu intră în mulțime, deci
    nimic nu se potrivește cu el.
    """
    if not origins:
        return frozenset()
    return frozenset(n for o in origins if (n := normalize_origin(o)) is not None)


def check_origin(raw: str | None, allowlist: frozenset[str]) -> tuple[bool, str | None]:
    """`(permis, motiv)` pentru headerul `Origin`.

    Header ABSENT → `(True, None)`: requesturile non-browser (health, server-to-server, teste) nu
    au origin, iar suprafața reală de abuz pe care o apărăm e browser-driven.

    Header PREZENT dar necanonizabil (inclusiv `null`) → respins. Header prezent și canonic dar
    în afara allowlistului → respins. Allowlist GOL respinge orice origin de browser
    (secure-by-default): un deployment fără `WEB_CORS_ORIGINS` nu servește niciun widget.
    """
    if raw is None or not raw.strip():
        return True, None
    normalized = normalize_origin(raw)
    if normalized is None:
        return False, "origin_malformed"
    if normalized not in allowlist:
        return False, "origin_not_allowed"
    return True, None


# ── JWT HS256 — primitiva partajată ─────────────────────────────────────────────────────────
# Stdlib, ca `identity.py`: fără dependență nouă și cu control TOTAL pe pinning-ul de algoritm.
# `alg=none` și confuzia de algoritm sunt atacuri clasice pe JWT, iar o bibliotecă generică le
# lasă adesea configurabile.


def _b64url_decode(seg: str) -> bytes:
    return base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4))


def verify_hs256_claims(
    token: str,
    secret: str,
    *,
    leeway_s: int = 30,
    issuer: str | None = None,
    audience: str | None = None,
) -> tuple[dict | None, str | None]:
    """`(claims, motiv_respingere)` dintr-un JWT HS256. Nu ridică niciodată pe input ostil.

    Verifică în ordine: structura, `alg=HS256` DUR, semnătura în timp constant, `exp` obligatoriu
    și neexpirat, apoi `iss`/`aud` DACĂ sunt configurate. `exp` e obligatoriu pentru că un token
    fără expirare e replay infinit: odată scurs, e valabil pe veci.
    """
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
    if not isinstance(header, dict) or header.get("alg") != "HS256":
        return None, "bad_alg"
    expected = hmac.new(
        secret.encode(), f"{header_b64}.{payload_b64}".encode(), hashlib.sha256
    ).digest()
    if not hmac.compare_digest(expected, signature):
        return None, "bad_signature"
    if not isinstance(payload, dict):
        return None, "malformed"
    exp = payload.get("exp")
    if not isinstance(exp, (int, float)) or isinstance(exp, bool):
        return None, "expired"
    if time.time() > float(exp) + leeway_s:
        return None, "expired"
    if issuer and payload.get("iss") != issuer:
        return None, "bad_issuer"
    if audience is not None and not _audience_matches(payload.get("aud"), audience):
        return None, "bad_audience"
    return payload, None


def _audience_matches(claim: object, expected: str) -> bool:
    """`aud` poate fi string sau listă (RFC 7519). Ambele forme, aceeași regulă."""
    if isinstance(claim, str):
        return claim == expected
    if isinstance(claim, list):
        return expected in claim
    return False


# ── Poarta de acces demo ────────────────────────────────────────────────────────────────────


def verify_demo_access(
    header_value: str | None,
    secret: str,
    *,
    leeway_s: int = 30,
    issuer: str | None = None,
    audience: str | None = None,
) -> tuple[bool, str | None]:
    """`Authorization: Bearer <jwt>` → `(trecut, motiv)`. **Nu întoarce niciodată claims.**

    Asta e poarta care ține site-ul demo privat. Nu spune CINE e clientul și nu are voie s-o
    spună: în v1 headerul sosea și nimeni nu-l valida, iar distanța dintre „un JWT valid a sosit"
    și „userul logat e X" e o singură linie de cod scrisă pe grabă. Semnătura tipului
    (`bool`, nu `str | None`) e ce face acea linie imposibil de scris.

    Suportă DOAR HS256. Un proiect Supabase migrat pe chei asimetrice (RS256/ES256 cu JWKS) va
    cădea pe `bad_alg` — fail-closed, vizibil, nu o acceptare tăcută.
    """
    if not header_value:
        return False, "missing"
    value = header_value.strip()
    scheme, _, token = value.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return False, "malformed"
    claims, reason = verify_hs256_claims(
        token.strip(), secret, leeway_s=leeway_s, issuer=issuer, audience=audience
    )
    if reason is not None:
        return False, reason
    del claims  # explicit: subiectul NU iese din această funcție
    return True, None


# ── Redactare / observabilitate ─────────────────────────────────────────────────────────────


def redact_secret(value: str | None) -> str:
    """Un secret în log e un secret pierdut. Păstrăm doar o amprentă corelabilă, nu valoarea.

    Amprenta e primele 8 caractere din SHA-256 — suficient ca să compari două apariții în
    aceeași investigație, insuficient ca să reconstruiești tokenul.
    """
    if not value:
        return "-"
    return f"sha256:{hashlib.sha256(value.encode()).hexdigest()[:8]}"


def origin_bucket(raw: str | None) -> str:
    """Etichetă LOW-CARDINALITY pentru evenimentul `web_origin_rejected`.

    Originul brut poate identifica tenantul (subdomeniul unui client) — deci nu intră în
    analytics ca text. Un allowlist are câteva intrări, dar mulțimea celor RESPINSE e nemărginită
    și controlată de atacator: pusă crudă într-o metrică, e o explozie de cardinalitate.
    """
    normalized = normalize_origin(raw)
    if normalized is None:
        return "malformed"
    return f"h{hashlib.sha256(normalized.encode()).hexdigest()[:6]}"


def visitor_bucket(visitor_id: str | None) -> str:
    """`visitor_id` e PII de canal (P12) → hash-uit înainte de orice cheie de rate limit sau
    etichetă de metrică."""
    if not visitor_id:
        return "-"
    return hashlib.sha256(visitor_id.encode()).hexdigest()[:16]
