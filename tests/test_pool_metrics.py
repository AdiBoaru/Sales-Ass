"""NX-161 Felia 0A — instrumentarea pool-ului: acquire-wait (ContextVar) + inflight + snapshot.

Pur, fără DB reală: `pool_snapshot`/gauge sunt pure; `tenant_conn` e testat cu un pool FALS
(acquire → conn fals), izolarea dezactivată (`_isolation_enabled=False`) ca să exercităm doar
wiring-ul de instrumentare (record acquire-wait + inc/dec inflight), nu asserturile NX-04.
"""

from src.db import connection as conn_mod
from src.db import pool_metrics as pm

# --------------------------------------------------------------------------- #
# 1. pool_snapshot — None-safe + best-effort
# --------------------------------------------------------------------------- #


class _FakePool:
    def __init__(self, size=3, idle=1, mx=10):
        self._size, self._idle, self._mx = size, idle, mx

    def get_size(self):
        return self._size

    def get_idle_size(self):
        return self._idle

    def get_max_size(self):
        return self._mx


def test_pool_snapshot_computes_in_use():
    snap = pm.pool_snapshot(_FakePool(size=7, idle=2, mx=10))
    assert snap["pool_size"] == 7
    assert snap["pool_idle"] == 2
    assert snap["pool_in_use"] == 5  # size - idle
    assert snap["pool_max"] == 10
    assert "pool_inflight" in snap


def test_pool_snapshot_none_pool_is_safe():
    # pool neinițializat (boot) → doar gauge-ul inflight, fără crash.
    snap = pm.pool_snapshot(None)
    assert snap == {"pool_inflight": pm.get_inflight()}


def test_pool_snapshot_introspection_error_is_best_effort():
    class _Broken:
        def get_size(self):
            raise RuntimeError("pool închis")

    snap = pm.pool_snapshot(_Broken())  # nu propagă — observabilitatea nu rupe turul
    assert snap == {"pool_inflight": pm.get_inflight()}


# --------------------------------------------------------------------------- #
# 2. acquire-wait ContextVar — record + take (cu reset)
# --------------------------------------------------------------------------- #


def test_acquire_wait_record_then_take_resets():
    pm.record_acquire_wait(12.5)
    assert pm.take_acquire_wait() == 12.5
    # al doilea take în același „tur" → None (nu re-raportăm o valoare stale)
    assert pm.take_acquire_wait() is None


# --------------------------------------------------------------------------- #
# 3. inflight gauge — inc/dec cu clamp la 0
# --------------------------------------------------------------------------- #


def test_inflight_inc_dec_and_clamp():
    before = pm.get_inflight()
    assert pm.inc_inflight() == before + 1
    assert pm.get_inflight() == before + 1
    assert pm.dec_inflight() == before
    # clamp: dec sub 0 rămâne 0 (nu contorizează greșit un release fără acquire)
    while pm.get_inflight() > 0:
        pm.dec_inflight()
    assert pm.dec_inflight() == 0


# --------------------------------------------------------------------------- #
# 4. tenant_conn — wiring de instrumentare (pool fals, izolare off)
# --------------------------------------------------------------------------- #


class _FakeConn:
    async def execute(self, *a, **k):
        return "SET"

    async def fetchrow(self, *a, **k):
        return {"biz": a[-1] if a else "", "usr": "bot_runtime"}


class _FakeAcquire:
    def __init__(self, conn):
        self._c = conn

    async def __aenter__(self):
        return self._c

    async def __aexit__(self, *a):
        return False


class _FakePoolAcquirable(_FakePool):
    def __init__(self):
        super().__init__()
        self._c = _FakeConn()

    def acquire(self):
        return _FakeAcquire(self._c)


async def test_tenant_conn_records_acquire_wait_and_inflight(monkeypatch):
    fake = _FakePoolAcquirable()

    async def _fake_get_bot_pool():
        return fake

    monkeypatch.setattr(conn_mod, "get_bot_pool", _fake_get_bot_pool)
    monkeypatch.setattr(conn_mod, "_isolation_enabled", lambda: False)

    before = pm.get_inflight()
    async with conn_mod.tenant_conn("biz-1") as c:
        assert c is fake._c
        assert pm.get_inflight() == before + 1  # checkout activ → gauge +1
    assert pm.get_inflight() == before  # release → gauge revenit
    # acquire-wait a fost înregistrat (float ≥ 0) de checkout
    wait = pm.take_acquire_wait()
    assert wait is not None and wait >= 0.0


async def test_tenant_conn_decrements_inflight_on_error(monkeypatch):
    fake = _FakePoolAcquirable()

    async def _fake_get_bot_pool():
        return fake

    monkeypatch.setattr(conn_mod, "get_bot_pool", _fake_get_bot_pool)
    monkeypatch.setattr(conn_mod, "_isolation_enabled", lambda: False)

    before = pm.get_inflight()
    try:
        async with conn_mod.tenant_conn("biz-1"):
            raise ValueError("boom în corpul checkout-ului")
    except ValueError:
        pass
    assert pm.get_inflight() == before  # finally a decrementat chiar și la excepție
