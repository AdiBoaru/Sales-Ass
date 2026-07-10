"""NX-85 — lock per conversație (ordonare multi-consumer). Fără Redis/DB real: fake redis
(SET NX EX + eval compare-del + xadd) + stub-uri pe dependențele lui process_event. Acoperă:
primitivele de lock, release atomic pe token, re-queue cu cap, și integrarea în process_event
(ocupat → re-queue fără handle_turn; liber → handle_turn + release)."""

import contextlib
from types import SimpleNamespace

from src.models import BusinessConfig
from src.redis_bus import acquire_conv_lock, release_conv_lock
from src.worker import consumer as cons


class LockRedis:
    """Fake Redis pentru lock: SET NX EX (dict) + eval (Lua compare-del) + xadd (re-queue)."""

    def __init__(self, occupied=False):
        self.store: dict = {}
        self._occupied = occupied  # simulează un lock deja deținut de altă replică
        self.xadds: list = []

    async def set(self, key, value, nx=False, ex=None):
        if self._occupied or (nx and key in self.store):
            return None
        self.store[key] = value
        return True

    async def eval(self, script, numkeys, key, arg):
        if self.store.get(key) == arg:
            del self.store[key]
            return 1
        return 0

    async def xadd(self, *a, **k):
        self.xadds.append((a, k))
        return "1-0"


# --- primitive (redis_bus) ---------------------------------------------------


async def test_acquire_then_busy():
    r = LockRedis()
    assert await acquire_conv_lock(r, "biz", "tg:acc:u", "tok1", ttl_s=30) is True
    # a doua tentativă pe aceeași cheie (alt token) → ocupat
    assert await acquire_conv_lock(r, "biz", "tg:acc:u", "tok2", ttl_s=30) is False


async def test_release_only_own_token():
    r = LockRedis()
    await acquire_conv_lock(r, "biz", "k", "tok1", ttl_s=30)
    await release_conv_lock(r, "biz", "k", "WRONG")  # token greșit → NU șterge
    assert "convlock:biz:k" in r.store
    await release_conv_lock(r, "biz", "k", "tok1")  # token corect → șterge
    assert "convlock:biz:k" not in r.store


# --- _requeue_busy -----------------------------------------------------------


async def test_requeue_under_cap_reenqueues():
    r = LockRedis()
    s = SimpleNamespace(conv_lock_requeue_delay_ms=0, conv_lock_max_requeues=10)
    status = await cons._requeue_busy(r, {"body": "x", "_requeues": 2}, s)
    assert r.xadds and "n=3" in status  # re-pus pe stream cu contor incrementat


async def test_requeue_over_cap_drops():
    r = LockRedis()
    s = SimpleNamespace(conv_lock_requeue_delay_ms=0, conv_lock_max_requeues=10)
    status = await cons._requeue_busy(r, {"body": "x", "_requeues": 10}, s)
    assert r.xadds == [] and "dropped" in status  # peste cap → drop, nu re-enqueue


async def test_requeue_admission_never_drops(monkeypatch):
    # NX-161 F0C (fix Codex #207): admission re-queue NU are cap de drop (P6) — spre deosebire de
    # _requeue_busy, re-pune ORICÂT (chiar la contor mare) ca un mesaj de client să nu dispară.
    r = LockRedis()
    s = SimpleNamespace(admission_requeue_delay_ms=0, admission_requeue_warn_every=20)
    status = await cons._requeue_admission(r, {"body": "x", "_admission_requeues": 999}, s)
    assert r.xadds  # re-pus pe stream (NU dropped)
    assert "requeue" in status and "dropped" not in status


# --- integrare process_event -------------------------------------------------


def _patch_pipeline(monkeypatch, calls, *, conv_lock_enabled=True):
    @contextlib.asynccontextmanager
    async def _acm(*a, **k):
        yield object()

    async def fake_resolve(conn, kind, account):
        return {"business_id": "b", "channel_id": "ch"}

    async def fake_load_business(conn, bid):
        return BusinessConfig(id="b", slug="s", name="n")

    async def fake_handle_turn(conn, business, channel_id, event, **k):
        calls.append("handle_turn")

    monkeypatch.setattr(cons, "admin_conn", _acm)
    monkeypatch.setattr(cons, "tenant_conn", _acm)
    monkeypatch.setattr(cons, "resolve_channel", fake_resolve)
    monkeypatch.setattr(cons, "load_business", fake_load_business)
    monkeypatch.setattr(cons, "handle_turn", fake_handle_turn)
    monkeypatch.setattr(
        cons,
        "get_settings",
        lambda: SimpleNamespace(
            conv_lock_enabled=conv_lock_enabled,
            conv_lock_ttl_seconds=30,
            conv_lock_requeue_delay_ms=0,
            conv_lock_max_requeues=10,
            # NX-161 F0C: consumer-ul citește timeout-ul de admission din settings.
            admission_acquire_timeout_ms=2000,
        ),
    )


_EVENT = {
    "channel_kind": "telegram",
    "channel_account_id": "acc",
    "sender_external_id": "u",
    "provider_msg_id": "m",
    "body": "salut",
}


async def test_process_event_acquires_processes_releases(monkeypatch):
    calls: list = []
    _patch_pipeline(monkeypatch, calls)
    r = LockRedis()
    await cons.process_event(object(), r, dict(_EVENT))
    assert calls == ["handle_turn"]  # a procesat
    assert r.store == {}  # lock eliberat în finally
    assert r.xadds == []  # nu s-a re-pus


async def test_process_event_busy_requeues_without_processing(monkeypatch):
    calls: list = []
    _patch_pipeline(monkeypatch, calls)
    r = LockRedis(occupied=True)  # altă replică deține lock-ul
    await cons.process_event(object(), r, dict(_EVENT))
    assert calls == []  # NU a procesat (conversație ocupată)
    assert r.xadds  # re-pus pe stream pentru altă replică


async def test_process_event_lock_disabled_processes(monkeypatch):
    calls: list = []
    _patch_pipeline(monkeypatch, calls, conv_lock_enabled=False)
    r = LockRedis(occupied=True)  # chiar „ocupat", dar lock-ul e dezactivat → ignorăm
    await cons.process_event(object(), r, dict(_EVENT))
    assert calls == ["handle_turn"]  # procesează fără lock (mono-consumer/dev)
