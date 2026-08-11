"""NX-231 — poarta de admission la MARGINI: ruta web sincronă + workerul.

0C punea frâna doar în worker. Ruta `/web/chat` rulează pipeline-ul IN-PROCESS (cheltuie LLM la
fiecare request), deci un burst pe web ocolea complet plafonul sistemului. Aici verificăm că
ambele margini trec prin ACEEAȘI poartă și că fiecare reacționează corect la refuz:

  • web sincron → 429 ONEST cu `Retry-After` (răspuns terminal, nu un payload gol);
  • worker async → re-queue cu backoff, FĂRĂ drop (P6: mesajul unui client nu dispare);
  • ambele → wait-ul intră pe traiectoria turului (`admission_wait`), respingerile în contoare.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from src.web import app as wa
from src.web.app import WebChatIn
from src.web.session import WebSession
from src.worker import admission as adm
from src.worker import consumer as cons
from src.worker.admission import Admission, AdmissionSlot
from src.worker.processor import TurnResult


async def _coro(value):
    return value


@asynccontextmanager
async def _fake_cm(*a, **k):
    yield None


class _Req:
    def __init__(self):
        self.client = SimpleNamespace(host="1.2.3.4")
        self.headers = {"content-length": "2"}

    async def stream(self):
        yield b"{}"


class _OkRedis:
    async def incr(self, *a, **k):
        return 1

    async def expire(self, *a, **k):
        return True

    async def get(self, *a, **k):
        return None

    async def set(self, *a, **k):
        return True

    async def eval(self, *a, **k):
        return 0

    async def evalsha(self, *a, **k):
        return 0


def _setup_web(monkeypatch, admission):
    """Marginea web cu tot ce e în afara admission-ului deja verde."""
    seen = {"turn": 0, "event": None}

    async def fake_verify(token, vid, sig):
        return WebSession(business_id="biz-1", token=token, visitor_id=vid)

    async def fake_resolve_channel(conn, kind, token):
        return {"channel_id": "chan", "business_id": "biz-1"}

    async def fake_load_business(conn, bid):
        return SimpleNamespace(id=bid, daily_cost_cap_usd=None)

    async def fake_handle_turn(db, business, channel_id, event, **k):
        seen["turn"] += 1
        seen["event"] = event
        return TurnResult("c", "ct", "t", "hi", None, reply=None, language="ro")

    monkeypatch.setattr(wa, "_verify", fake_verify)
    monkeypatch.setattr(wa, "get_redis", lambda: _coro(_OkRedis()))
    monkeypatch.setattr(wa, "get_pool", lambda: _coro(None))
    monkeypatch.setattr(wa, "admin_conn", _fake_cm)
    monkeypatch.setattr(wa, "tenant_db", lambda business_id: _fake_cm)
    monkeypatch.setattr(wa, "resolve_channel", fake_resolve_channel)
    monkeypatch.setattr(wa, "load_business", fake_load_business)
    monkeypatch.setattr(wa, "handle_turn", fake_handle_turn)
    monkeypatch.setattr(wa, "web_rate_limited", lambda *a, **k: _coro(False))
    monkeypatch.setattr(wa, "cost_over_budget", lambda *a, **k: _coro(False))
    monkeypatch.setattr(wa, "web_cost_over_visitor_cap", lambda *a, **k: _coro(False))
    monkeypatch.setattr(wa, "web_cost_add_visitor", lambda *a, **k: _coro(None))
    monkeypatch.setattr(wa, "get_admission", lambda redis=None: admission)
    monkeypatch.setattr(wa, "get_settings", lambda: _web_settings(True))  # poarta web ON by default
    return seen


def _web_settings(admission_on: bool):
    return SimpleNamespace(
        web_max_body_bytes=10_000,
        web_demo_access_enabled=False,
        web_cors_origins_list=[],
        web_session_v2_required=False,
        web_identity_enabled=False,
        web_turn_lock_enabled=False,
        web_admission_enabled=admission_on,
        admission_web_wait_ms=50,
        daily_cost_cap_usd=0.0,
        web_cost_cap_per_visitor_usd=0.0,
        turn_lock_ttl_ms=1000,
        turn_lock_wait_max_ms=100,
    )


def _req():
    return WebChatIn(token="tok", visitor_id="web_1", sig="s", message="salut")


# --------------------------------------------------------------------------- #
# Web sincron
# --------------------------------------------------------------------------- #


async def test_web_chat_passes_through_admission_and_records_wait(monkeypatch):
    a = Admission(max_inflight=4, max_per_business=2)
    seen = _setup_web(monkeypatch, a)
    await wa.web_chat(_req(), _Req())
    assert seen["turn"] == 1
    assert a.inflight == 0  # slotul s-a întors (finally), altfel a doua cerere ar fi respinsă
    await wa.web_chat(_req(), _Req())
    assert seen["turn"] == 2


async def test_web_chat_returns_429_with_retry_after_when_over_capacity(monkeypatch):
    a = Admission(max_inflight=1, max_per_business=0)
    held = await a.acquire("biz-1", 0.05)  # singurul slot e luat de alt request
    assert held.admitted
    seen = _setup_web(monkeypatch, a)
    with pytest.raises(HTTPException) as ei:
        await wa.web_chat(_req(), _Req())
    assert ei.value.status_code == 429
    assert ei.value.headers.get("Retry-After") == "1"
    assert seen["turn"] == 0  # ZERO LLM cheltuit pe o cerere respinsă


async def test_web_chat_rejection_does_not_leak_the_slot(monkeypatch):
    a = Admission(max_inflight=1, max_per_business=0)
    held = await a.acquire("biz-1", 0.05)
    _setup_web(monkeypatch, a)
    with pytest.raises(HTTPException):
        await wa.web_chat(_req(), _Req())
    await a.release(held)
    assert a.inflight == 0
    # după eliberare, capacitatea e din nou 1 (respinsul n-a consumat nimic)
    again = await a.acquire("biz-1", 0.05)
    assert again.admitted


async def test_web_chat_tenant_cap_protects_the_other_tenant(monkeypatch):
    a = Admission(max_inflight=4, max_per_business=1)
    mine = await a.acquire("biz-1", 0.05)
    assert mine.admitted
    _setup_web(monkeypatch, a)
    with pytest.raises(HTTPException) as ei:
        await wa.web_chat(_req(), _Req())
    assert ei.value.status_code == 429
    other = await a.acquire("alt-tenant", 0.05)
    assert other.admitted  # burst-ul lui biz-1 nu i-a mâncat capacitatea


async def test_web_chat_flag_off_keeps_previous_behaviour(monkeypatch):
    a = Admission(max_inflight=1, max_per_business=0)
    await a.acquire("biz-1", 0.05)  # saturat
    seen = _setup_web(monkeypatch, a)
    monkeypatch.setattr(wa, "get_settings", lambda: _web_settings(False))
    await wa.web_chat(_req(), _Req())  # OFF → poarta nu există, ca înainte de NX-231
    assert seen["turn"] == 1


async def test_web_chat_marks_degraded_admission_on_the_event(monkeypatch):
    class _Degraded(Admission):
        async def acquire(self, business_id, timeout_s):
            return AdmissionSlot(
                admitted=True,
                wait_ms=12.0,
                token="x",
                backend="local_fallback",
                business_id=business_id,
                degraded=True,
            )

    seen = _setup_web(monkeypatch, _Degraded(4, 2))
    await wa.web_chat(_req(), _Req())
    assert seen["event"]["admission_wait_ms"] == 12.0
    assert seen["event"]["admission_degraded"] is True


# --------------------------------------------------------------------------- #
# Worker async
# --------------------------------------------------------------------------- #


def _setup_worker(monkeypatch, admission, calls):
    @asynccontextmanager
    async def _acm(*a, **k):
        yield object()

    async def fake_resolve(conn, kind, account):
        return {"business_id": "biz-1", "channel_id": "ch"}

    async def fake_load_business(conn, bid):
        return SimpleNamespace(id=bid, default_locale="ro")

    async def fake_handle_turn(db, business, channel_id, event, **k):
        calls.append(("turn", event.get("admission_wait_ms")))
        return None

    monkeypatch.setattr(cons, "admin_conn", _acm)
    monkeypatch.setattr(cons, "tenant_db", lambda business_id: _acm)
    monkeypatch.setattr(cons, "resolve_channel", fake_resolve)
    monkeypatch.setattr(cons, "load_business", fake_load_business)
    monkeypatch.setattr(cons, "handle_turn", fake_handle_turn)
    monkeypatch.setattr(cons, "get_admission", lambda redis=None: admission)
    monkeypatch.setattr(
        cons,
        "get_settings",
        lambda: SimpleNamespace(
            conv_lock_enabled=False,
            conv_lock_ttl_seconds=30,
            conv_lock_requeue_delay_ms=0,
            conv_lock_max_requeues=10,
            admission_acquire_timeout_ms=20,
            admission_requeue_delay_ms=0,
            admission_requeue_warn_every=20,
        ),
    )


class _StreamRedis:
    def __init__(self):
        self.xadds: list = []

    async def xadd(self, stream, fields, **k):
        self.xadds.append((stream, fields))
        return "1-1"


_EVENT = {
    "channel_kind": "telegram",
    "channel_account_id": "acc",
    "sender_external_id": "u",
    "provider_msg_id": "m",
    "body": "salut",
}


async def test_worker_requeues_instead_of_dropping_when_over_capacity(monkeypatch):
    a = Admission(max_inflight=1, max_per_business=0)
    await a.acquire("biz-1", 0.05)  # capacitate ocupată
    calls: list = []
    _setup_worker(monkeypatch, a, calls)
    redis = _StreamRedis()
    await cons.process_event(object(), redis, dict(_EVENT))
    assert calls == []  # turul NU a rulat
    assert len(redis.xadds) == 1  # dar mesajul a fost RE-PUS, nu pierdut (P6)


async def test_worker_admission_wait_travels_on_the_turn(monkeypatch):
    class _Waited(Admission):
        async def acquire(self, business_id, timeout_s):
            return AdmissionSlot(
                admitted=True, wait_ms=120.0, token="x", backend="redis", business_id=business_id
            )

    calls: list = []
    _setup_worker(monkeypatch, _Waited(4, 2), calls)
    await cons.process_event(object(), _StreamRedis(), dict(_EVENT))
    assert calls == [("turn", 120.0)]  # P10: wait-ul e pe traiectoria turului, nu doar în log


async def test_worker_releases_the_slot_even_if_the_turn_raises(monkeypatch):
    a = Admission(max_inflight=1, max_per_business=0)
    calls: list = []
    _setup_worker(monkeypatch, a, calls)

    async def boom(*args, **kwargs):
        raise RuntimeError("tur crăpat")

    monkeypatch.setattr(cons, "handle_turn", boom)
    with pytest.raises(RuntimeError):
        await cons.process_event(object(), _StreamRedis(), dict(_EVENT))
    assert a.inflight == 0  # altfel un tur care crapă ar consuma permanent capacitate


@pytest.fixture(autouse=True)
def _reset_admission_singleton():
    adm.reset_admission()
    yield
    adm.reset_admission()
