"""Sesiune web semnată (NX-20a) — token public per tenant + visitor_id HMAC.

Web-ul e anonim (fără login, fără PII de user). La primul contact widget-ul primește un
`visitor_id` generat + semnat HMAC cu `session_secret`-ul tenantului → clientul nu poate
falsifica identitatea altui vizitator, dar nici nu cerem cont. Secretul vine din
`channels.settings` (control plane, `admin_conn`) — cache LRU+TTL ca să nu lovim DB la fiecare
mesaj/heartbeat. Lookup-ul `public_token → tenant` derivă business-ul ÎNAINTE de a-l ști, ca
`resolve_channel` (excepția documentată de control plane, P7).

ZERO LLM (cod pur determinist). `visitor_id` e PII de canal → trăiește DOAR în
`channel_identities` (P12), niciodată în loguri; `public_token` e un secret de site (nu PII).

> Notă v1: `session_secret` e o cheie de SEMNĂTURĂ per-tenant pentru id-uri anonime de vizitator
> (sensibilitate mică), ținută în `channels.settings`. Hardening de producție = mutare în secret
> manager (ca `credentials_ref`) — follow-up, nu blochează MVP-ul.
"""

from __future__ import annotations

import base64
import hmac
import json
import time
from dataclasses import dataclass
from functools import lru_cache
from hashlib import sha256
from typing import TYPE_CHECKING
from uuid import uuid4

from src.config import get_settings
from src.db.queries.channels import resolve_web_session

if TYPE_CHECKING:
    import asyncpg


@dataclass(frozen=True)
class WebSession:
    """O sesiune web VERIFICATĂ. `visitor_id` = id-ul vizitatorului (PII de canal).

    `identity_secret` (NX-129, opțional) = cheia per-tenant cu care marginea verifică JWT-ul de
    login passthrough; None = passthrough inactiv pe tenant (rămâne sesiune anonimă)."""

    business_id: str
    token: str
    visitor_id: str
    identity_secret: str | None = None
    # NX-229: claims-urile sesiunii v2 (expirare, key id, origin). `None` = sesiune v1, care nu
    # are niciun claim — distincția rămâne vizibilă în cod, nu ascunsă în valori implicite.
    claims: SessionClaims | None = None


def _compute_sig(token: str, visitor_id: str, secret: str) -> str:
    return hmac.new(secret.encode(), f"{token}:{visitor_id}".encode(), sha256).hexdigest()


def issue_visitor(token: str, session_secret: str, *, prefix: str = "web") -> tuple[str, str]:
    """Generează un `visitor_id` nou + semnătura lui (la bootstrap). Widget-ul le ține și le
    retrimite la fiecare request.

    `prefix` (default `web`) e partea de dinaintea uuid-ului în `visitor_id`. Traficul real
    rămâne `web_*`; harness-ul de audit ([scripts/sim/web_audit.py](../../scripts/sim/web_audit.py))
    trece `web_audit` ca vizitatorii lui să fie DISTINȘI de trafic real și curățabili (prefix scanat
    la purjă). `visitor_id` e opac pe calea web (sig-ul e peste `{token}:{visitor_id}`), deci
    prefixul nu schimbă nicio validare."""
    visitor_id = f"{prefix}_{uuid4().hex}"
    return visitor_id, _compute_sig(token, visitor_id, session_secret)


def verify_sig(token: str, visitor_id: str, sig: str, secret: str) -> bool:
    """Semnătură validă? Comparare în timp CONSTANT (anti timing-attack). False la orice câmp
    gol sau nepotrivire."""
    if not (token and visitor_id and sig and secret):
        return False
    return hmac.compare_digest(_compute_sig(token, visitor_id, secret), sig)


class SessionSecretCache:
    """Cache LRU+TTL pentru `public_token → {business_id, session_secret}`. Evită un query pe
    `admin_conn` la fiecare mesaj/heartbeat. Cache-uiește ȘI miss-urile (negative cache cu același
    TTL scurt) ca un flux de tokenuri invalide pe endpointul PUBLIC să nu bombardeze DB-ul. TTL
    scurt (default 60s) → revocarea/seed-ul unui canal se propagă repede."""

    def __init__(self, ttl_s: float, maxsize: int = 1024) -> None:
        self._ttl = ttl_s
        self._maxsize = maxsize
        self._store: dict[str, tuple[float, dict | None]] = {}

    async def get(self, conn: asyncpg.Connection, public_token: str) -> dict | None:
        now = time.monotonic()
        cached = self._store.get(public_token)
        if cached is not None and (now - cached[0]) < self._ttl:
            return cached[1]
        resolved = await resolve_web_session(conn, public_token)
        self._put(public_token, resolved, now)
        return resolved

    def _put(self, key: str, value: dict | None, now: float) -> None:
        # evict cel mai vechi DOAR la inserare de cheie nouă peste maxsize (LRU aproximativ pe timp)
        if key not in self._store and len(self._store) >= self._maxsize:
            oldest = min(self._store, key=lambda k: self._store[k][0])
            del self._store[oldest]
        self._store[key] = (now, value)

    def clear(self) -> None:
        self._store.clear()


@lru_cache
def get_session_cache() -> SessionSecretCache:
    """Singleton per proces; TTL din settings."""
    return SessionSecretCache(ttl_s=get_settings().web_session_secret_ttl_s)


async def verify_web_session(
    conn: asyncpg.Connection, token: str, visitor_id: str, sig: str
) -> WebSession | None:
    """(token, visitor_id, sig) → `WebSession` validă, sau None. Lookup secret prin cache
    (control plane), apoi verificare HMAC. None = token necunoscut/inactiv SAU semnătură invalidă
    (endpointul răspunde 403 fără a distinge cele două — nu dăm un oracol unui atacator)."""
    resolved = await get_session_cache().get(conn, token)
    if resolved is None:
        return None
    if not verify_sig(token, visitor_id, sig, resolved["session_secret"]):
        return None
    return WebSession(
        business_id=resolved["business_id"],
        token=token,
        visitor_id=visitor_id,
        identity_secret=resolved.get("identity_secret"),  # NX-129; absent pe seed-uri vechi
    )


# ════════════════════════════════════════════════════════════════════════════════════════════
# NX-229 — sesiune v2: claims semnate cu expirare, key id, rotație dual-key și origin binding.
#
# DE CE. Semnătura v1 e `HMAC(secret, "{token}:{visitor_id}")` — o valoare care nu conține NIMIC
# despre timp. Consecințele nu sunt teoretice:
#   • nu expiră niciodată: o semnătură scursă azi e validă și peste un an;
#   • nu se poate roti: schimbi `session_secret`-ul și inviezi toate sesiunile simultan, deci
#     în practică nu-l schimbi niciodată;
#   • nu e legată de origin: aceeași pereche merge de pe orice pagină.
#
# FORMA DE PE SÂRMĂ RĂMÂNE ACEEAȘI. v2 pune claims-urile în câmpul `sig`, ca `v2.<claims>.<mac>`.
# Widgetul trimite tot `{token, visitor_id, sig}` și tot nu interpretează nimic — pentru el sig-ul
# e opac în ambele versiuni. Detectarea versiunii se face după prefix, deci v1 și v2 coexistă în
# overlap fără un al doilea câmp și fără o rută paralelă.
#
# KEY ID DERIVAT, NU CONFIGURAT. `kid = sha256(secret)[:8]`. Rotația devine „pune noul secret în
# `session_secret` și mută-l pe cel vechi în `session_secret_prev`" — fără un al treilea câmp de
# configurare care să se desincronizeze de chei.
# ════════════════════════════════════════════════════════════════════════════════════════════

_V2_PREFIX = "v2"
# Toleranță pentru ceasuri nesincronizate între emitent și verificator. Fără ea, un server cu
# ceasul cu două secunde înainte respinge sesiuni pe care tocmai le-a emis.
_CLOCK_SKEW_S = 60


@dataclass(frozen=True)
class SessionClaims:
    """Claims-urile verificate ale unei sesiuni v2. `key_age` spune cu care cheie s-a validat —
    `current` sau `previous` — ca overlapul de rotație să fie măsurabil, nu presupus."""

    visitor_id: str
    issued_at: int
    expires_at: int
    key_id: str
    key_age: str
    origin: str | None = None


def key_id_for(secret: str) -> str:
    """Identificatorul cheii = amprentă a secretului. Nu îl expune: e derivat, nu inversabil."""
    return sha256(secret.encode()).hexdigest()[:8]


def _token_fingerprint(token: str) -> str:
    """Legăm sesiunea de tokenul public CARE A EMIS-O, fără să-l copiem în claims.

    Ăsta e exact atacul „refolosește sesiunea cu alt public token": fără legătură, o semnătură
    validă pentru tenantul A ar trece și pe tenantul B dacă B ar avea din întâmplare același
    secret — sau, mai realist, dacă un atacator ar putea alege ce token prezintă.
    """
    return sha256(token.encode()).hexdigest()[:16]


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64url_decode(seg: str) -> bytes:
    return base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4))


def _sign_claims(claims_b64: str, secret: str) -> str:
    return _b64url_encode(hmac.new(secret.encode(), claims_b64.encode(), sha256).digest())


def issue_session_v2(
    token: str,
    session_secret: str,
    *,
    ttl_s: int,
    prefix: str = "web",
    origin: str | None = None,
) -> tuple[str, str]:
    """Emite `(visitor_id, sig_v2)`. `origin` normalizat, dacă e dat, leagă sesiunea de pagină."""
    visitor_id = f"{prefix}_{uuid4().hex}"
    return visitor_id, sign_session_v2(
        token, visitor_id, session_secret, ttl_s=ttl_s, origin=origin
    )


def sign_session_v2(
    token: str,
    visitor_id: str,
    session_secret: str,
    *,
    ttl_s: int,
    origin: str | None = None,
    now: int | None = None,
) -> str:
    issued = int(now if now is not None else time.time())
    claims: dict[str, object] = {
        "tok": _token_fingerprint(token),
        "vis": visitor_id,
        "iat": issued,
        "exp": issued + int(ttl_s),
        "kid": key_id_for(session_secret),
    }
    if origin:
        claims["org"] = origin
    # `separators` + `sort_keys` → serializare canonică: aceeași intrare produce același octet,
    # deci semnătura e reproductibilă între procese și versiuni de Python.
    claims_b64 = _b64url_encode(json.dumps(claims, sort_keys=True, separators=(",", ":")).encode())
    return f"{_V2_PREFIX}.{claims_b64}.{_sign_claims(claims_b64, session_secret)}"


def is_v2_sig(sig: str) -> bool:
    return bool(sig) and sig.startswith(f"{_V2_PREFIX}.")


def verify_session_v2(
    token: str,
    visitor_id: str,
    sig: str,
    *,
    secrets: dict[str, str],
    origin: str | None = None,
    require_origin_match: bool = False,
    now: int | None = None,
) -> tuple[SessionClaims | None, str | None]:
    """`(claims, motiv)`. `secrets` = `{"current": …}` plus opțional `{"previous": …}`.

    Ordinea verificărilor e deliberată: structura, apoi cheia, apoi semnătura, apoi legăturile
    (token, visitor), apoi timpul, apoi originul. Semnătura se verifică ÎNAINTE de orice claim,
    ca să nu luăm nicio decizie pe date pe care nu le-am autentificat încă.
    """
    if not is_v2_sig(sig):
        return None, "not_v2"
    parts = sig.split(".")
    if len(parts) != 3:
        return None, "malformed"
    _, claims_b64, mac = parts
    try:
        claims = json.loads(_b64url_decode(claims_b64))
    except (ValueError, json.JSONDecodeError):
        return None, "malformed"
    if not isinstance(claims, dict):
        return None, "malformed"

    kid = claims.get("kid")
    if not isinstance(kid, str) or not kid:
        return None, "malformed"
    key_age = next(
        (age for age, secret in secrets.items() if secret and key_id_for(secret) == kid), None
    )
    if key_age is None:
        # Cheie necunoscută: rotită de două ori, sau semnătură fabricată. Aceeași reacție pentru
        # amândouă — nu confirmăm unui atacator că a nimerit un kid valid.
        return None, "unknown_key"
    if not hmac.compare_digest(_sign_claims(claims_b64, secrets[key_age]), mac):
        return None, "bad_signature"

    if claims.get("tok") != _token_fingerprint(token):
        return None, "token_mismatch"
    if claims.get("vis") != visitor_id:
        return None, "visitor_mismatch"

    issued_at, expires_at = claims.get("iat"), claims.get("exp")
    if not isinstance(issued_at, int) or not isinstance(expires_at, int):
        return None, "malformed"
    current = int(now if now is not None else time.time())
    if current > expires_at + _CLOCK_SKEW_S:
        return None, "expired"
    if issued_at > current + _CLOCK_SKEW_S:
        return None, "issued_in_future"

    bound_origin = claims.get("org")
    if bound_origin is not None and not isinstance(bound_origin, str):
        return None, "malformed"
    if bound_origin and origin and bound_origin != origin:
        return None, "origin_mismatch"
    if require_origin_match and not bound_origin:
        return None, "origin_unbound"

    return (
        SessionClaims(
            visitor_id=visitor_id,
            issued_at=issued_at,
            expires_at=expires_at,
            key_id=kid,
            key_age=key_age,
            origin=bound_origin,
        ),
        None,
    )


async def verify_web_session_any(
    conn: asyncpg.Connection,
    token: str,
    visitor_id: str,
    sig: str,
    *,
    origin: str | None = None,
    v2_required: bool = False,
    require_origin_match: bool = False,
) -> tuple[WebSession | None, str | None]:
    """Punctul UNIC de verificare a sesiunii pe toate endpointurile web.

    În overlap acceptă ambele versiuni; cu `v2_required` refuză v1 (comutatorul de cutover). Motivul
    respingerii iese ca reason code pentru `web_session_rejected{reason}` — endpointul răspunde tot
    403 nediferențiat, ca să nu ofere un oracol.
    """
    resolved = await get_session_cache().get(conn, token)
    if resolved is None:
        return None, "unknown_token"

    def _session(claims: SessionClaims | None) -> WebSession:
        return WebSession(
            business_id=resolved["business_id"],
            token=token,
            visitor_id=visitor_id,
            identity_secret=resolved.get("identity_secret"),
            claims=claims,
        )

    if is_v2_sig(sig):
        claims, reason = verify_session_v2(
            token,
            visitor_id,
            sig,
            secrets={
                "current": resolved["session_secret"],
                "previous": resolved.get("session_secret_prev") or "",
            },
            origin=origin,
            require_origin_match=require_origin_match,
        )
        if reason is not None:
            return None, reason
        return _session(claims), None

    if v2_required:
        return None, "v1_refused"
    if not verify_sig(token, visitor_id, sig, resolved["session_secret"]):
        return None, "bad_signature"
    return _session(None), None
