"""Client Redis partajat + backbone-ul de coadă (stagiul 2 din arhitectură).

Producătorul (webhook) face XADD pe stream-ul de inbound; consumatorul (worker)
citește cu consumer group. Aici stau și helperele de:
  • dedupe rapid (NX-51 layer 1): SET NX EX peste (channel_account_id, provider_msg_id),
    ca retry-ul agresiv al Meta să nu producă două procesări. Plasa durabilă
    (layer 2, tabel ne-partiționat) e în worker, cu business_id real.
  • enqueue inbound: XADD cu trim aproximativ (cap de siguranță la lungime).

Clientul e `decode_responses=True` → str peste tot (nu bytes), payload-ul de
business e un singur câmp JSON `data`.
"""

import json
import logging
from typing import Any

from redis.asyncio import Redis, from_url

from src.config import get_settings

log = logging.getLogger(__name__)

# Stream unic de inbound. Ordinea per-conversație e impusă în worker prin lock
# pe conversation_id (rezolvat după ce worker-ul atinge DB), nu prin stream key —
# la webhook încă nu știm conversation_id fără un round-trip în DB.
STREAM_INBOUND = "inbound"

# Retenția dedupe: 48h acoperă fereastra de retry Meta cu marjă.
_DEDUPE_TTL_SECONDS = 172_800

# Cap de siguranță pe lungimea stream-ului (trim aproximativ, ieftin).
_STREAM_MAXLEN = 100_000

_redis: Redis | None = None


async def get_redis() -> Redis:
    """Client singleton per proces. Lazy-init la primul apel."""
    global _redis
    if _redis is None:
        _redis = from_url(get_settings().redis_url, decode_responses=True)
    return _redis


async def close_redis() -> None:
    global _redis
    if _redis is not None:
        await _redis.aclose()
        _redis = None


async def seen_before(redis: Redis, channel_account_id: str, provider_msg_id: str) -> bool:
    """True dacă mesajul a mai fost văzut (dedupe layer 1).

    `SET key 1 NX EX` e atomic: întoarce True (cheie setată) doar prima dată.
    Cheia e pe (channel_account_id, provider_msg_id) — channel_account_id (id-ul
    canalului receptor) mapează 1:1 la canal/business, deci e un proxy corect
    pentru unicitate înainte de a avea business_id.
    """
    key = f"dedupe:{channel_account_id}:{provider_msg_id}"
    was_set = await redis.set(key, "1", nx=True, ex=_DEDUPE_TTL_SECONDS)
    return not was_set


async def enqueue_inbound(redis: Redis, event: dict[str, Any]) -> str:
    """XADD un eveniment inbound pe stream-ul de procesare. Întoarce id-ul stream."""
    return await redis.xadd(
        STREAM_INBOUND,
        {"data": json.dumps(event)},
        maxlen=_STREAM_MAXLEN,
        approximate=True,
    )


# --- Lock per conversație (NX-85) -------------------------------------------
# Serializează tururile aceleiași conversații între REPLICI de worker. Cheia include
# `business_id` (P7) + identificatorul de expeditor (PII de canal → DOAR în cheie, efemeră cu TTL;
# NICIODATĂ în loguri, P12). `token` per-tentativă → release atomic (compare-del), nu ștergem
# lock-ul altui worker dacă al nostru a expirat la TTL.

# Lua: șterge cheia DOAR dacă valoarea (token) e a noastră → release sigur sub TTL/race.
_RELEASE_LUA = (
    "if redis.call('get', KEYS[1]) == ARGV[1] "
    "then return redis.call('del', KEYS[1]) else return 0 end"
)


def _conv_lock_key(business_id: str, sender_key: str) -> str:
    return f"convlock:{business_id}:{sender_key}"


async def acquire_conv_lock(
    redis: Redis, business_id: str, sender_key: str, token: str, *, ttl_s: int
) -> bool:
    """`SET NX EX` atomic: True dacă am luat lock-ul, False dacă e ocupat (alt worker procesează
    aceeași conversație). TTL = plasă dacă worker-ul moare cu lock-ul luat. Ridică la eroare de
    Redis (caller-ul tratează fail-open)."""
    got = await redis.set(_conv_lock_key(business_id, sender_key), token, nx=True, ex=ttl_s)
    return bool(got)


async def release_conv_lock(redis: Redis, business_id: str, sender_key: str, token: str) -> None:
    """Eliberează lock-ul DOAR dacă tokenul e al nostru (Lua compare-del atomic). Best-effort:
    o eroare de Redis e logată, lock-ul expiră oricum la TTL (nu blocăm conversația pe veci)."""
    try:
        await redis.eval(_RELEASE_LUA, 1, _conv_lock_key(business_id, sender_key), token)
    except Exception as e:  # noqa: BLE001 — best-effort; TTL e plasa
        log.warning("conv lock: release eșuat (%s) — expiră la TTL", type(e).__name__)
