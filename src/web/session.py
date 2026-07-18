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

import hmac
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
