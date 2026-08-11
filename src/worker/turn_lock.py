"""NX-221 — serializare ture per conversație pe calea web SINCRONĂ (`POST /web/chat`).

Pe web, două mesaje trimise în rafală rulează două pipeline-uri CONCURENTE pe același
snapshot de stare (calea sincronă nu trece prin consumer, deci lock-ul NX-85 dintre
replici nu o acoperă). Lock-ul de aici serializează tururile ACELEIAȘI conversații la
marginea web, ÎNAINTE de `handle_turn`, cu așteptare (retry + backoff) până la
`turn_lock_wait_max_ms`.

Semantica (principiul 6 — niciodată tăcere):
  • lock liber → acquire instant, turul rulează;
  • lock ocupat → așteptăm cu backoff; eliberat în fereastră → turul rulează serializat;
  • fereastra expiră SAU Redis e indisponibil → BYPASS: procesăm oricum (un lock blocat
    nu are voie să lase clientul fără răspuns); plasa de conflict rămâne optimistic
    lock-ul `state_version` din processor (retry + drop-patch, tot NX-221).

Cheia include `business_id` (P7 — niciun lock cross-tenant) + identitatea expeditorului
pe canal (proxy 1:1 pentru conversația deschisă — la margine `conversation_id` nu e
cunoscut încă fără round-trip în DB; același raționament ca NX-85). Identitatea e PII de
canal → trăiește DOAR în cheia Redis, efemeră cu TTL, NICIODATĂ în loguri (P12).

TTL-ul e plasa anti-deadlock dacă procesul moare cu lock-ul luat; release-ul e
compare-and-delete (Lua) pe token — nu ștergem lock-ul altui tur dacă al nostru a
expirat între timp.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from uuid import uuid4

from redis.asyncio import Redis

log = logging.getLogger(__name__)

# Lua: șterge cheia DOAR dacă valoarea (token) e a noastră → release sigur sub TTL/race.
_RELEASE_LUA = (
    "if redis.call('get', KEYS[1]) == ARGV[1] "
    "then return redis.call('del', KEYS[1]) else return 0 end"
)

# Backoff-ul de așteptare: pornește scurt (lock-urile se eliberează în sub o secundă în
# mod normal — durata unui tur), crește exponențial ca să nu ciocănim Redis-ul degeaba.
_POLL_INITIAL_S = 0.05
_POLL_MAX_S = 0.5


def turn_lock_key(business_id: str, sender_key: str) -> str:
    """Cheia de lock: tenant + expeditor (P7). `sender_key` = identitatea pe canal."""
    return f"turnlock:{business_id}:{sender_key}"


@dataclass
class TurnLock:
    """Rezultatul unei tentative de acquire.

    `acquired=False` = BYPASS (fereastră expirată / Redis jos) — turul se procesează
    oricum, dar `release_turn_lock` devine no-op (nu deținem lock-ul, n-avem ce
    elibera; compare-del pe token ne-ar proteja oricum, dar nu apelăm degeaba)."""

    acquired: bool
    waited_ms: float
    token: str
    key: str


async def acquire_turn_lock(
    redis: Redis,
    business_id: str,
    sender_key: str,
    *,
    ttl_ms: int,
    wait_max_ms: int,
) -> TurnLock:
    """`SET key token NX PX ttl_ms` cu așteptare (backoff) până la `wait_max_ms`.

    Nu ridică NICIODATĂ spre apelant: orice eroare de Redis = bypass (fail-open,
    principiul 6) — clientul primește răspuns chiar dacă serializarea e indisponibilă.
    """
    token = uuid4().hex
    key = turn_lock_key(business_id, sender_key)
    started = time.monotonic()
    deadline = started + wait_max_ms / 1000.0
    delay = _POLL_INITIAL_S
    first_attempt = True
    while True:
        try:
            got = await redis.set(key, token, nx=True, px=ttl_ms)
        except Exception as e:  # noqa: BLE001 — Redis jos → bypass, nu blocăm turul
            waited_ms = (time.monotonic() - started) * 1000.0
            log.warning("turn lock: acquire eșuat (%s) — bypass", type(e).__name__)
            return TurnLock(acquired=False, waited_ms=waited_ms, token=token, key=key)
        now = time.monotonic()
        if got:
            # Din prima încercare = zero AȘTEPTARE (RTT-ul Redis nu e contenție) → waited_ms=0,
            # ca `turn_lock_wait` să se emită doar când chiar s-a stat după alt tur.
            waited_ms = 0.0 if first_attempt else (now - started) * 1000.0
            return TurnLock(acquired=True, waited_ms=waited_ms, token=token, key=key)
        first_attempt = False
        if now >= deadline:  # fereastra a expirat → bypass (procesăm oricum)
            return TurnLock(
                acquired=False, waited_ms=(now - started) * 1000.0, token=token, key=key
            )
        await asyncio.sleep(min(delay, max(deadline - now, 0.0)))
        delay = min(delay * 2, _POLL_MAX_S)


async def release_turn_lock(redis: Redis, lock: TurnLock) -> None:
    """Eliberează lock-ul DOAR dacă tokenul e al nostru (Lua compare-del atomic).
    Best-effort: o eroare de Redis e logată, lock-ul expiră oricum la TTL. No-op pe
    bypass (nu deținem lock-ul — nu ștergem lock-ul turului care îl deține)."""
    if not lock.acquired:
        return
    try:
        await redis.eval(_RELEASE_LUA, 1, lock.key, lock.token)
    except Exception as e:  # noqa: BLE001 — best-effort; TTL e plasa
        log.warning("turn lock: release eșuat (%s) — expiră la TTL", type(e).__name__)
