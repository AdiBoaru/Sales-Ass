"""Frâna de admission: semafor local (0C) + lease-uri DISTRIBUITE, fairness și fail-closed (NX-231).

Trei niveluri, testate separat:
  1. **local** — comportamentul 0C: acquire/release, saturație globală → deadline → defer, plafon
     per-business → defer imediat, dezactivat → no-op.
  2. **distribuit** — lease-uri într-un sorted set Redis (fake in-process): plafon global comun,
     plafon per-tenant, recuperarea lease-urilor EXPIRATE (replică moartă), fairness între tenanți.
  3. **degradare** — store indisponibil → NU „cap per proces în tăcere", ci fallback local BOUNDED,
     marcat `degraded`, cu respingeri numărate; plafon 0 → fail-closed total.
"""

import asyncio
import time

import pytest

from src.worker.admission import Admission, AdmissionSlot, tenant_bucket

# --------------------------------------------------------------------------- #
# Fake Redis: exact comenzile folosite de backend-ul distribuit (zset + time + pipeline)
# --------------------------------------------------------------------------- #


class FakeRedis:
    """ZSET-uri in-process + un ceas controlabil. Suficient pentru semantica lease-urilor."""

    def __init__(self, now_ms: int = 1_000_000):
        self.z: dict[str, dict[str, float]] = {}
        self.now_ms = now_ms
        self.fail = False

    def advance(self, ms: int) -> None:
        self.now_ms += ms

    async def time(self):
        if self.fail:
            raise OSError("redis down")
        return (self.now_ms // 1000, (self.now_ms % 1000) * 1000)

    def pipeline(self):
        return _FakePipe(self)

    # comenzi (folosite direct de pipeline)
    def _zremrangebyscore(self, key, lo, hi):
        d = self.z.setdefault(key, {})
        drop = [m for m, s in d.items() if lo <= s <= hi]
        for m in drop:
            del d[m]
        return len(drop)

    def _zcard(self, key):
        return len(self.z.get(key, {}))

    def _zadd(self, key, mapping):
        self.z.setdefault(key, {}).update(mapping)
        return len(mapping)

    def _zrem(self, key, member):
        return 1 if self.z.get(key, {}).pop(member, None) is not None else 0

    def _zrank(self, key, member):
        d = self.z.get(key, {})
        if member not in d:
            return None
        return sorted(d, key=lambda m: (d[m], m)).index(member)


class _FakePipe:
    def __init__(self, r: FakeRedis):
        self.r = r
        self.ops: list = []

    def zremrangebyscore(self, key, lo, hi):
        self.ops.append(("zremrangebyscore", key, lo, hi))
        return self

    def zcard(self, key):
        self.ops.append(("zcard", key))
        return self

    def zadd(self, key, mapping):
        self.ops.append(("zadd", key, mapping))
        return self

    def zrem(self, key, member):
        self.ops.append(("zrem", key, member))
        return self

    def zrank(self, key, member):
        self.ops.append(("zrank", key, member))
        return self

    def pexpire(self, key, ms):
        self.ops.append(("pexpire", key, ms))
        return self

    async def execute(self):
        if self.r.fail:
            raise OSError("redis down")
        out = []
        for op in self.ops:
            name = op[0]
            if name == "zremrangebyscore":
                out.append(self.r._zremrangebyscore(op[1], op[2], op[3]))
            elif name == "zcard":
                out.append(self.r._zcard(op[1]))
            elif name == "zadd":
                out.append(self.r._zadd(op[1], op[2]))
            elif name == "zrem":
                out.append(self.r._zrem(op[1], op[2]))
            elif name == "zrank":
                out.append(self.r._zrank(op[1], op[2]))
            else:
                out.append(1)
        return out


# --------------------------------------------------------------------------- #
# 1. Backend LOCAL (comportamentul 0C, păstrat pentru dev/teste/joburi)
# --------------------------------------------------------------------------- #


async def test_local_acquire_release_roundtrip():
    a = Admission(max_inflight=2, max_per_business=0)
    slot = await a.acquire("b1", timeout_s=1.0)
    assert slot.admitted and slot.backend == "local" and slot.wait_ms >= 0.0
    assert a.inflight == 1
    await a.release(slot)
    assert a.inflight == 0


async def test_local_global_saturation_defers_after_deadline():
    a = Admission(max_inflight=1, max_per_business=0)
    first = await a.acquire("b1", 1.0)
    assert first.admitted
    t0 = time.perf_counter()
    denied = await a.acquire("b1", 0.05)
    assert not denied.admitted and denied.reason == "queue_timeout"
    assert (time.perf_counter() - t0) >= 0.04  # chiar a așteptat deadline-ul
    assert a.inflight == 1  # deferul NU a luat slot
    await a.release(first)
    assert (await a.acquire("b1", 1.0)).admitted


async def test_local_per_business_cap_defers_immediately():
    a = Admission(max_inflight=10, max_per_business=1)
    assert (await a.acquire("b1", 1.0)).admitted
    t0 = time.perf_counter()
    denied = await a.acquire("b1", 5.0)
    assert not denied.admitted and denied.reason == "tenant_cap"
    assert (time.perf_counter() - t0) < 0.5  # imediat, n-a blocat 5s pe coada globală
    assert (await a.acquire("b2", 1.0)).admitted  # alt business are slot
    assert a.inflight == 2


async def test_disabled_is_noop():
    a = Admission(max_inflight=0, max_per_business=0)
    slot = await a.acquire("b1", 1.0)
    assert slot.admitted and slot.backend == "off"
    assert a.inflight == 0
    await a.release(slot)  # no-op, fără crash
    assert a.inflight == 0


async def test_local_per_business_counter_cleaned_on_release():
    a = Admission(max_inflight=10, max_per_business=3)
    s1 = await a.acquire("b1", 1.0)
    s2 = await a.acquire("b1", 1.0)
    assert a._per_business["b1"] == 2
    await a.release(s1)
    await a.release(s2)
    assert "b1" not in a._per_business  # curățat la 0 → dict-ul nu crește nemărginit


async def test_local_per_business_cap_holds_under_global_wait():
    # TOCTOU (Codex #207): două task-uri pt ACELAȘI business trec de pre-check (business la 0) și
    # AȘTEAPTĂ pe semaforul global; la eliberare, re-check-ul de DUPĂ acquire trebuie să respecte
    # cap-ul, altfel ambele incrementează → depășesc max_per_business.
    a = Admission(max_inflight=2, max_per_business=1)
    s2 = await a.acquire("b2", 1.0)
    s3 = await a.acquire("b3", 1.0)
    assert s2.admitted and s3.admitted  # global FULL
    t1 = asyncio.create_task(a.acquire("b1", 2.0))
    t2 = asyncio.create_task(a.acquire("b1", 2.0))
    await asyncio.sleep(0.02)  # ambele ajung la await sem.acquire (pre-check trecut, b1=0)
    await a.release(s2)
    await a.release(s3)
    r1, r2 = await asyncio.gather(t1, t2)
    assert sorted([r1.admitted, r2.admitted]) == [False, True]
    assert a._per_business.get("b1", 0) == 1  # niciodată 2
    assert a.inflight == 1


# --------------------------------------------------------------------------- #
# 2. Backend DISTRIBUIT — plafon comun între „replici", TTL, fairness
# --------------------------------------------------------------------------- #


async def test_distributed_enforces_global_cap_across_processes():
    # Două instanțe = două replici ale workerului. Plafonul e al SISTEMULUI, nu al procesului:
    # exact bug-ul pe care 0C nu-l putea vedea (cap N×proces, tăcut).
    r = FakeRedis()
    a1 = Admission(2, 0, redis=r)
    a2 = Admission(2, 0, redis=r)
    s1 = await a1.acquire("b1", 0.05)
    s2 = await a2.acquire("b1", 0.05)
    assert s1.admitted and s2.admitted and s1.backend == "redis"
    denied = await a1.acquire("b1", 0.05)
    assert not denied.admitted and denied.reason == "queue_timeout"
    await a2.release(s2)
    assert (await a1.acquire("b1", 0.05)).admitted  # slot eliberat → capacitate reală


async def test_distributed_tenant_cap_protects_other_tenants():
    # Fairness: burst-ul unui tenant nu poate mânca toată capacitatea globală.
    r = FakeRedis()
    a = Admission(max_inflight=4, max_per_business=2, redis=r)
    held = [await a.acquire("burst", 0.05) for _ in range(2)]
    assert all(s.admitted for s in held)
    denied = await a.acquire("burst", 0.05)
    assert not denied.admitted and denied.reason == "tenant_cap"
    quiet = await a.acquire("quiet-tenant", 0.05)
    assert quiet.admitted  # tenantul liniștit intră imediat, deși „burst" bate la ușă


async def test_distributed_expired_lease_is_reclaimed():
    # O replică moare fără release: lease-ul expiră (scor = momentul expirării) și capacitatea
    # revine singură. Fără asta, un crash ar bloca permanent un slot din plafonul global.
    r = FakeRedis()
    a = Admission(1, 0, redis=r, lease_ttl_ms=1_000)
    assert (await a.acquire("b1", 0.02)).admitted
    assert not (await a.acquire("b1", 0.02)).admitted
    r.advance(1_500)  # peste TTL
    assert (await a.acquire("b1", 0.02)).admitted


async def test_distributed_release_frees_both_keys():
    r = FakeRedis()
    a = Admission(3, 2, redis=r)
    slot = await a.acquire("b1", 0.05)
    assert r._zcard("adm:global") == 1 and r._zcard("adm:biz:b1") == 1
    await a.release(slot)
    assert r._zcard("adm:global") == 0 and r._zcard("adm:biz:b1") == 0


async def test_distributed_release_survives_store_failure():
    # Release pe un store căzut nu are voie să propage: lease-ul expiră singur după TTL.
    r = FakeRedis()
    a = Admission(2, 0, redis=r)
    slot = await a.acquire("b1", 0.05)
    r.fail = True
    await a.release(slot)  # fără excepție


# --------------------------------------------------------------------------- #
# 3. Degradare — store jos: bounded, marcat, numărat (NU cap N×proces în tăcere)
# --------------------------------------------------------------------------- #


async def test_store_down_falls_back_to_bounded_local():
    r = FakeRedis()
    r.fail = True
    a = Admission(100, 0, redis=r, local_fallback_max=2)
    s1 = await a.acquire("b1", 0.05)
    s2 = await a.acquire("b1", 0.05)
    assert s1.admitted and s1.degraded and s1.backend == "local_fallback"
    assert s2.admitted
    denied = await a.acquire("b1", 0.05)
    # plafonul GLOBAL e 100, dar sub store căzut nu-l onorăm: fallback-ul e explicit mai mic
    assert not denied.admitted and denied.reason == "local_full" and denied.degraded
    assert a.stats.rejected["local_full"] == 1
    assert a.stats.degraded >= 3
    await a.release(s1)
    assert (await a.acquire("b1", 0.05)).admitted  # slotul s-a întors


async def test_store_down_with_zero_fallback_is_fail_closed():
    r = FakeRedis()
    r.fail = True
    a = Admission(100, 0, redis=r, local_fallback_max=0)
    denied = await a.acquire("b1", 0.05)
    assert not denied.admitted and denied.reason == "store_unavailable" and denied.degraded


async def test_misconfigured_fairness_warns(caplog):
    # per-business >= global anulează fairness-ul (un tenant poate lua tot) → trebuie să fie
    # zgomotos la construcție, nu o surpriză descoperită sub burst.
    with caplog.at_level("WARNING"):
        Admission(max_inflight=4, max_per_business=4)
    assert any("fairness" in rec.message for rec in caplog.records)


# --------------------------------------------------------------------------- #
# 4. Etichetă de metrică + singleton
# --------------------------------------------------------------------------- #


def test_tenant_bucket_is_low_cardinality_and_not_the_raw_id():
    biz = "6098812a-50fc-44bd-a1ba-bc77e6399158"
    bucket = tenant_bucket(biz)
    assert biz not in bucket and bucket.startswith("t") and len(bucket) == 9
    assert tenant_bucket(biz) == bucket  # stabil
    assert tenant_bucket(None) == "-"


def test_admission_slot_defaults_are_safe():
    slot = AdmissionSlot(admitted=False)
    assert slot.token is None and slot.backend == "off" and not slot.degraded


async def test_singleton_reset():
    from src.worker import admission as adm

    adm.reset_admission()
    first = adm.get_admission()
    assert adm.get_admission() is first  # singleton
    adm.reset_admission()
    assert adm.get_admission() is not first  # reset → instanță nouă
    adm.reset_admission()


@pytest.mark.parametrize("reason", ["tenant_cap", "queue_timeout", "store_unavailable"])
def test_rejection_reasons_are_countable(reason):
    a = Admission(1, 1)
    a.stats.reject(reason)
    a.stats.reject(reason)
    assert a.stats.as_dict()["rejected"][reason] == 2
