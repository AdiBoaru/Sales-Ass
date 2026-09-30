"""NX-361 — pe `/web/chat` răspunsul nu mai așteaptă aftercare-ul.

Conversația `1848eeba` (`sole-ro`, 2026-09-30): după fiecare răspuns gata, clientul mai aștepta
3,3-4,5 s extracția de profil (`run_aftercare`, un apel de model), fiindcă `/web/chat` o rula
înainte de `return`. Nimic din aftercare nu schimbă răspunsul. Acum rulează în `BackgroundTasks`,
după trimitere; costul pipeline-ului intră în plafonul vizitatorului înainte de răspuns, cel de
aftercare din fundal.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks, FastAPI
from fastapi.testclient import TestClient

from src.config import get_settings
from src.models import Reply
from src.web import app as wa
from src.web.app import WebChatIn
from src.web.session import WebSession
from src.worker.processor import TurnResult
from tests.test_web_gateway import FakeRedis, _Req


@asynccontextmanager
async def _fake_cm(*a, **k):
    yield None


async def _coro(value):
    return value


@pytest.fixture
def wired(monkeypatch):
    """`/web/chat` cu pipeline, DB și Redis falși; înregistrează ORDINEA: răspunsul construit,
    aftercare-ul, costurile adăugate."""
    log: list = []
    redis = FakeRedis()

    async def fake_verify(token, visitor_id, sig):
        return WebSession(business_id="b", token=token, visitor_id=visitor_id)

    async def fake_resolve_channel(conn, kind, token):
        return {"channel_id": "chan", "business_id": "b"}

    async def fake_load_business(conn, business_id):
        return SimpleNamespace(id=business_id, daily_cost_cap_usd=None)

    async def fake_handle_turn(*args, **kwargs):
        reply = Reply(text="Salut!")
        work = SimpleNamespace(ctx=SimpleNamespace(usage=SimpleNamespace(cost_usd=0.003)))
        return TurnResult(
            "c", "ct", "t", reply.text, None, reply=reply, language="ro", aftercare=work
        )

    async def fake_aftercare(db, supplied_redis, work):
        log.append("aftercare")
        return 0.004

    async def fake_add_visitor(supplied_redis, business_id, visitor_id, amount):
        log.append(("cost", round(amount, 6)))

    real_build = wa._build_chat_response

    def spy_build(result):
        log.append("response")
        return real_build(result)

    monkeypatch.setattr(wa, "_verify", fake_verify)
    monkeypatch.setattr(wa, "get_redis", lambda: _coro(redis))
    monkeypatch.setattr(wa, "get_pool", lambda: _coro(None))
    monkeypatch.setattr(wa, "admin_conn", _fake_cm)
    monkeypatch.setattr(wa, "tenant_db", lambda business_id: _fake_cm)
    monkeypatch.setattr(wa, "resolve_channel", fake_resolve_channel)
    monkeypatch.setattr(wa, "load_business", fake_load_business)
    monkeypatch.setattr(wa, "handle_turn", fake_handle_turn)
    monkeypatch.setattr(wa, "run_aftercare", fake_aftercare)
    monkeypatch.setattr(wa, "web_cost_add_visitor", fake_add_visitor)
    monkeypatch.setattr(wa, "_build_chat_response", spy_build)
    return log


def _chat() -> WebChatIn:
    return WebChatIn(token="tok", visitor_id="web_1", sig="s", message="hei")


async def test_the_reply_is_built_before_aftercare(wired) -> None:
    background = BackgroundTasks()
    res = await wa.web_chat(_chat(), _Req(), background)

    assert "Salut!" in res["content"]
    # răspunsul e gata, costul pipeline-ului e deja în plafon, aftercare-ul n-a rulat
    assert wired == [("cost", 0.003), "response"]
    assert len(background.tasks) == 1

    await background()  # ce face serverul DUPĂ ce a trimis răspunsul
    assert wired == [("cost", 0.003), "response", "aftercare", ("cost", 0.004)]


async def test_flag_off_is_the_old_order(wired, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "web_chat_aftercare_detached_enabled", False)
    background = BackgroundTasks()
    await wa.web_chat(_chat(), _Req(), background)
    assert wired == ["aftercare", ("cost", 0.007), "response"]
    assert not background.tasks


async def test_without_background_tasks_aftercare_stays_inline(wired) -> None:
    """Apelurile directe (teste, alte seam-uri) nu primesc `BackgroundTasks`: ordinea veche."""
    await wa.web_chat(_chat(), _Req())
    assert wired == ["aftercare", ("cost", 0.007), "response"]


def test_fastapi_injects_background_tasks(wired, monkeypatch) -> None:
    """Prin aplicația FastAPI: dacă `BackgroundTasks` n-ar fi injectat (parametrul are o valoare
    implicită pentru apelurile directe), ordinea ar fi cea inline și testul ar pica."""
    for guard in ("_enforce_demo_access", "_enforce_session_origin"):
        monkeypatch.setattr(wa, guard, lambda *a, **k: None)
    monkeypatch.setattr(wa, "_enforce_origin", lambda request: None)
    app = FastAPI()
    app.include_router(wa.router)
    with TestClient(app) as client:
        resp = client.post(
            "/web/chat", json={"token": "tok", "visitor_id": "web_1", "sig": "s", "message": "hei"}
        )
    assert resp.status_code == 200 and "Salut!" in resp.json()["content"]
    assert wired == [("cost", 0.003), "response", "aftercare", ("cost", 0.004)]


async def test_detached_aftercare_is_bounded(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "web_aftercare_max_concurrent", 2)
    wa._AFTERCARE_SLOTS.clear()
    running = peak = 0

    async def slow_aftercare(db, redis, work):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.01)
        running -= 1
        return 0.0

    async def no_cost(*a):
        return None

    monkeypatch.setattr(wa, "run_aftercare", slow_aftercare)
    monkeypatch.setattr(wa, "web_cost_add_visitor", no_cost)
    await asyncio.gather(*(wa._detached_aftercare(None, None, None, "b", "v") for _ in range(6)))
    assert peak == 2
    wa._AFTERCARE_SLOTS.clear()


async def test_a_failing_detached_aftercare_does_not_raise(monkeypatch, caplog) -> None:
    async def boom(db, redis, work):
        raise RuntimeError("aftercare stricat")

    monkeypatch.setattr(wa, "run_aftercare", boom)
    await wa._detached_aftercare(None, None, None, "b", "v")  # nu aruncă: răspunsul e livrat
    assert "aftercare detașat a eșuat" in caplog.text
