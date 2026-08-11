"""Admission control — frâna EXPLICITĂ de concurență a tururilor (NX-161 Felia 0C → NX-231).

De ce există: poolul DB (max 10) era frâna ACCIDENTALĂ a sistemului. Conn-per-op (NX-231) îl
scoate din rolul ăsta — un tur nu mai ține o conexiune peste apelul la model — deci fără o frână
de înlocuire un burst ar trimite zeci de tururi simultan la OpenAI (cost, memorie, rate-limit).

Ce adaugă NX-231 peste 0C:

  • **Distribuit.** 0C avea un `asyncio.Semaphore` PER PROCES: cu N replici capacitatea reală era
    N × cap, iar nimeni nu o vedea. Acum sloturile sunt LEASE-uri într-un sorted set Redis, cu
    scor = momentul expirării → o replică moartă nu-și ține slotul la infinit, iar limita e reală.
  • **Plafon per-business ACTIV implicit.** Ăsta E mecanismul de fairness: un tenant în burst nu
    poate lua decât `max_per_business` din capacitatea globală, deci alt tenant găsește mereu loc.
    Config greșită (per-business ≥ global) = fairness anulată → WARNING zgomotos la construcție.
  • **Deadline de coadă.** Aștepți un slot cel mult `timeout_s`; peste el, apelantul decide
    (worker: re-queue cu backoff, P6 — niciodată drop tăcut; web sincron: 429 onest).
  • **Fail-CLOSED bounded când store-ul e jos.** NU cădem înapoi pe „cap per proces în tăcere" —
    asta ar readuce exact problema pe care o rezolvăm, fix când sistemul e deja în suferință.
    Cădem pe un semafor local cu plafon EXPLICIT mai mic (`admission_local_fallback_max`),
    marcăm `degraded=True` și numărăm respingerile.

Contract: `acquire()` întoarce un `AdmissionSlot`. `admitted=False` are mereu un `reason`
low-cardinality (`tenant_cap` | `queue_timeout` | `store_unavailable` | `local_full`), potrivit
ca etichetă de metrică. Eliberarea se face cu `release(slot)` — idempotentă.

P12: nicio valoare de client nu intră în chei sau metrici. `business_id` e UUID de tenant;
eticheta de metrică e `tenant_bucket` (hash scurt), ca la `origin_bucket`/`visitor_bucket`.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass, field
from time import perf_counter
from uuid import uuid4

from src.config import get_settings

log = logging.getLogger(__name__)

# Prefixe de chei (namespace separat de `webrl:*` / `turnlock:*`).
_GLOBAL_KEY = "adm:global"
_BIZ_KEY = "adm:biz:"

# Cât așteptăm între două încercări de a prinde un slot global. Suficient de scurt cât să nu
# adauge latență vizibilă, suficient de lung cât să nu transformăm așteptarea în busy-loop pe Redis.
_RETRY_SLEEP_S = 0.05


def tenant_bucket(business_id: str | None) -> str:
    """Etichetă LOW-CARDINALITY de tenant pentru metrici (`admission_wait_ms{tenant_bucket}`).

    `business_id` e un UUID intern, nu PII de client — dar o metrică per-tenant în clar leagă
    dashboardul de identitatea comercială a clientului. Hash scurt, ca `origin_bucket`."""
    if not business_id:
        return "-"
    return f"t{hashlib.sha256(business_id.encode()).hexdigest()[:8]}"


@dataclass(frozen=True)
class AdmissionSlot:
    """Rezultatul unui `acquire`. `token` e lease-ul de eliberat (None când frâna e OFF)."""

    admitted: bool
    wait_ms: float = 0.0
    reason: str | None = None
    token: str | None = None
    backend: str = "off"  # off | redis | local | local_fallback
    business_id: str = ""
    degraded: bool = False


@dataclass
class AdmissionStats:
    """Contoare de proces pentru health/probe. Respingerile NU au traiectorie de tur (turul nici
    n-a început) → nu pot fi evenimente de analytics; rămân contoare + log."""

    admitted: int = 0
    rejected: dict[str, int] = field(default_factory=dict)
    degraded: int = 0

    def reject(self, reason: str) -> None:
        self.rejected[reason] = self.rejected.get(reason, 0) + 1

    def as_dict(self) -> dict:
        return {
            "admitted": self.admitted,
            "rejected": dict(sorted(self.rejected.items())),
            "degraded": self.degraded,
        }


class Admission:
    """Frâna de concurență. Cu `redis` → lease-uri distribuite; fără → semafor local (dev/teste).

    asyncio e single-thread → contoarele locale sunt int-uri simple, fără lock."""

    def __init__(
        self,
        max_inflight: int,
        max_per_business: int,
        *,
        redis=None,
        lease_ttl_ms: int = 120_000,
        local_fallback_max: int = 0,
    ) -> None:
        self._max_inflight = max_inflight
        self._max_per_business = max_per_business
        self._redis = redis
        self._lease_ttl_ms = max(1_000, lease_ttl_ms)
        self._local_fallback_max = max(0, local_fallback_max)
        self._sem = asyncio.Semaphore(max_inflight) if max_inflight > 0 else None
        # Fallback-ul NU e un semafor: nimeni nu AȘTEAPTĂ pe el. Sub o dependență căzută vrem un
        # verdict imediat (re-queue / 429), nu o coadă care crește — deci un contor simplu
        # (asyncio e single-thread → increment/decrement fără lock).
        self._fallback_used = 0
        self._per_business: dict[str, int] = {}
        self._inflight = 0
        self.stats = AdmissionStats()
        if max_inflight > 0 and 0 < max_per_business >= max_inflight:
            # Nu e o eroare fatală, dar e o config care ANULEAZĂ fairness-ul: un singur tenant
            # poate lua tot. Trebuie să fie zgomotos, nu o surpriză descoperită sub burst.
            log.warning(
                "admission: max_per_business (%d) >= max_inflight (%d) → un singur tenant poate "
                "monopoliza capacitatea (fairness anulată)",
                max_per_business,
                max_inflight,
            )

    @property
    def enabled(self) -> bool:
        return self._max_inflight > 0

    @property
    def distributed(self) -> bool:
        return self._redis is not None and self.enabled

    @property
    def inflight(self) -> int:
        """Câte tururi dețin ACUM un slot în ACEST proces (gauge local; cel real e în Redis)."""
        return self._inflight

    # --- API public -------------------------------------------------------

    async def acquire(self, business_id: str, timeout_s: float) -> AdmissionSlot:
        """Ia un slot, așteptând cel mult `timeout_s`. Contractul e în docstring-ul modulului."""
        if not self.enabled:
            return AdmissionSlot(admitted=True, backend="off", business_id=business_id)
        t0 = perf_counter()
        if self._redis is not None:
            try:
                slot = await self._acquire_redis(business_id, timeout_s, t0)
            except Exception as e:  # noqa: BLE001 — vezi mai jos: store-ul nu are voie să rupă turul
                # Store indisponibil: NU trecem tăcut la „cap per proces". Fallback BOUNDED.
                # Prindem `Exception`, nu doar `RedisError`: un client incompatibil sau o eroare de
                # protocol e tot „nu pot coordona" — a lăsa turul unui client să crape fiindcă
                # frâna nu răspunde ar fi mai rău decât a degrada zgomotos (P6). `CancelledError`
                # e `BaseException` în 3.12 → anularea trece mai departe, nu e înghițită aici.
                log.warning(
                    "admission: store indisponibil (%s) → fallback local bounded (max=%d)",
                    type(e).__name__,
                    self._local_fallback_max,
                )
                self.stats.degraded += 1
                return self._acquire_local_fallback(business_id, t0)
        else:
            slot = await self._acquire_local(business_id, timeout_s, t0)
        if slot.admitted:
            self.stats.admitted += 1
        elif slot.reason:
            self.stats.reject(slot.reason)
        return slot

    async def release(self, slot: AdmissionSlot) -> None:
        """Eliberează slotul. Idempotentă și tolerantă la Redis jos (lease-ul expiră singur)."""
        if not slot.admitted or slot.backend == "off":
            return
        if slot.backend == "redis" and slot.token:
            try:
                pipe = self._redis.pipeline()
                pipe.zrem(_GLOBAL_KEY, slot.token)
                pipe.zrem(_BIZ_KEY + slot.business_id, slot.token)
                await pipe.execute()
            except Exception as e:  # noqa: BLE001 — lease-ul expiră după TTL → capacitatea revine
                log.warning(
                    "admission: release eșuat (%s) — lease-ul expiră singur", type(e).__name__
                )
            return
        if slot.backend == "local_fallback":
            self._fallback_used = max(0, self._fallback_used - 1)
            return
        self._release_local(slot.business_id)

    # --- backend distribuit (Redis) ---------------------------------------

    async def _acquire_redis(self, business_id: str, timeout_s: float, t0: float) -> AdmissionSlot:
        biz_key = _BIZ_KEY + business_id
        token = uuid4().hex
        deadline = perf_counter() + max(0.0, timeout_s)
        while True:
            now_ms = await self._server_now_ms()
            expires_at = now_ms + self._lease_ttl_ms
            # Curăță lease-urile expirate (replici moarte) ÎNAINTE de a număra.
            pipe = self._redis.pipeline()
            pipe.zremrangebyscore(_GLOBAL_KEY, 0, now_ms)
            pipe.zremrangebyscore(biz_key, 0, now_ms)
            pipe.zcard(_GLOBAL_KEY)
            pipe.zcard(biz_key)
            _, _, global_n, biz_n = await pipe.execute()

            if self._max_per_business > 0 and biz_n >= self._max_per_business:
                # Plafon de tenant → defer IMEDIAT, fără să ocupăm coada globală. Ăsta e
                # mecanismul de fairness: burst-ul lui A nu consumă sloturile lui B.
                return AdmissionSlot(
                    admitted=False,
                    wait_ms=(perf_counter() - t0) * 1000.0,
                    reason="tenant_cap",
                    business_id=business_id,
                )
            if global_n < self._max_inflight:
                # Adaugă-te, apoi verifică-ți RANGUL: `zcard` + `zadd` nu sunt atomice între
                # replici, deci două procese pot trece simultan de verificare. Rangul (după scor =
                # momentul expirării ≈ ordinea intrării) spune cine a rămas peste plafon; acela
                # își retrage lease-ul. Auto-corector, fără Lua.
                pipe = self._redis.pipeline()
                pipe.zadd(_GLOBAL_KEY, {token: expires_at})
                pipe.zadd(biz_key, {token: expires_at})
                pipe.pexpire(_GLOBAL_KEY, self._lease_ttl_ms * 2)
                pipe.pexpire(biz_key, self._lease_ttl_ms * 2)
                pipe.zrank(_GLOBAL_KEY, token)
                pipe.zrank(biz_key, token)
                *_, global_rank, biz_rank = await pipe.execute()
                over_global = global_rank is None or global_rank >= self._max_inflight
                over_biz = self._max_per_business > 0 and (
                    biz_rank is None or biz_rank >= self._max_per_business
                )
                if not over_global and not over_biz:
                    return AdmissionSlot(
                        admitted=True,
                        wait_ms=(perf_counter() - t0) * 1000.0,
                        token=token,
                        backend="redis",
                        business_id=business_id,
                    )
                # peste plafon la re-verificare → retrage lease-ul și încearcă din nou
                pipe = self._redis.pipeline()
                pipe.zrem(_GLOBAL_KEY, token)
                pipe.zrem(biz_key, token)
                await pipe.execute()
                if over_biz:
                    return AdmissionSlot(
                        admitted=False,
                        wait_ms=(perf_counter() - t0) * 1000.0,
                        reason="tenant_cap",
                        business_id=business_id,
                    )
            if perf_counter() >= deadline:
                return AdmissionSlot(
                    admitted=False,
                    wait_ms=(perf_counter() - t0) * 1000.0,
                    reason="queue_timeout",
                    business_id=business_id,
                )
            await asyncio.sleep(min(_RETRY_SLEEP_S, max(0.0, deadline - perf_counter())))

    async def _server_now_ms(self) -> int:
        """Ceasul SERVERULUI Redis, nu al replicii: lease-urile sunt comparate între procese, iar
        un drift de ceas ar expira sloturi vii (sau ar ține sloturi moarte)."""
        secs, micros = await self._redis.time()
        return int(secs) * 1000 + int(micros) // 1000

    # --- backend local (dev / teste / fallback) ---------------------------

    def _business_saturated(self, business_id: str) -> bool:
        return (
            self._max_per_business > 0
            and self._per_business.get(business_id, 0) >= self._max_per_business
        )

    async def _acquire_local(self, business_id: str, timeout_s: float, t0: float) -> AdmissionSlot:
        if self._business_saturated(business_id):
            return AdmissionSlot(
                admitted=False,
                wait_ms=(perf_counter() - t0) * 1000.0,
                reason="tenant_cap",
                business_id=business_id,
            )
        try:
            async with asyncio.timeout(timeout_s):
                await self._sem.acquire()
        except TimeoutError:
            return AdmissionSlot(
                admitted=False,
                wait_ms=(perf_counter() - t0) * 1000.0,
                reason="queue_timeout",
                business_id=business_id,
            )
        # Re-check per-business DUPĂ acquire (TOCTOU, Codex #207): alt task pt ACELAȘI business a
        # putut trece de pre-check și lua sloturi cât așteptam pe semaforul global. Fără `await`
        # între re-check și increment → atomic în asyncio → cap-ul rămâne respectat strict.
        if self._business_saturated(business_id):
            self._sem.release()
            return AdmissionSlot(
                admitted=False,
                wait_ms=(perf_counter() - t0) * 1000.0,
                reason="tenant_cap",
                business_id=business_id,
            )
        self._inflight += 1
        self._per_business[business_id] = self._per_business.get(business_id, 0) + 1
        return AdmissionSlot(
            admitted=True,
            wait_ms=(perf_counter() - t0) * 1000.0,
            token="local",
            backend="local",
            business_id=business_id,
        )

    def _acquire_local_fallback(self, business_id: str, t0: float) -> AdmissionSlot:
        """Store jos: plafon local EXPLICIT, mai mic. Fără așteptare — sub o dependență căzută
        vrem un răspuns rapid (re-queue / 429), nu o coadă care crește."""
        wait_ms = (perf_counter() - t0) * 1000.0
        if self._local_fallback_max <= 0:
            # Plafon 0 = fail-CLOSED total: cât timp nu putem coordona, nu admitem trafic nou.
            self.stats.reject("store_unavailable")
            return AdmissionSlot(
                admitted=False,
                wait_ms=wait_ms,
                reason="store_unavailable",
                business_id=business_id,
                degraded=True,
            )
        if self._fallback_used >= self._local_fallback_max:
            self.stats.reject("local_full")
            return AdmissionSlot(
                admitted=False,
                wait_ms=wait_ms,
                reason="local_full",
                business_id=business_id,
                degraded=True,
            )
        self._fallback_used += 1
        self.stats.admitted += 1
        return AdmissionSlot(
            admitted=True,
            wait_ms=wait_ms,
            token="local_fallback",
            backend="local_fallback",
            business_id=business_id,
            degraded=True,
        )

    def _release_local(self, business_id: str) -> None:
        if self._sem is None:
            return
        self._inflight = max(0, self._inflight - 1)
        remaining = self._per_business.get(business_id, 0) - 1
        if remaining <= 0:
            self._per_business.pop(business_id, None)
        else:
            self._per_business[business_id] = remaining
        self._sem.release()


_admission: Admission | None = None


def get_admission(redis=None) -> Admission:
    """Singleton/proces (ca poolurile). Citește configul o dată; OFF → limite 0 (no-op).

    `redis` (opțional) activează backend-ul DISTRIBUIT. Trecut de apelanții care au deja un client
    (consumer, ruta web); absent → semafor local (dev, teste, joburi)."""
    global _admission
    if _admission is None:
        s = get_settings()
        on = s.admission_enabled
        use_redis = redis is not None and s.admission_distributed_enabled
        _admission = Admission(
            s.admission_max_inflight if on else 0,
            (
                (
                    s.admission_max_per_business_distributed
                    if use_redis
                    else s.admission_max_per_business
                )
                if on
                else 0
            ),
            redis=redis if use_redis else None,
            lease_ttl_ms=s.admission_lease_ttl_ms,
            local_fallback_max=s.admission_local_fallback_max,
        )
    return _admission


def reset_admission() -> None:
    """Test hook — resetează singleton-ul (ca `get_settings.cache_clear`)."""
    global _admission
    _admission = None
