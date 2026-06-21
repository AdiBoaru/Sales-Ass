"""NX-120 — DoS hardening: body-size cap, fail-CLOSED pe calea care cheltuie LLM (/web/chat),
cost-cap per business + per vizitator, verificare Origin la bootstrap. Fără rețea/DB reală:
fake redis (poate arunca RedisError) + seam-uri monkeypatch-uite."""

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from redis.exceptions import RedisError

from src.web import app as wa
from src.web.app import WebChatIn, WebMessageIn
from src.web.session import WebSession
from src.webhook.body_limit import enforce_body_cap
from src.worker.processor import TurnResult


async def _coro(value):
    return value


@asynccontextmanager
async def _fake_cm(*a, **k):
    yield None


class _Req:
    def __init__(self, host="1.2.3.4", body=b"{}", headers=None):
        self.client = SimpleNamespace(host=host)
        self._body = body
        self.headers = headers if headers is not None else {"content-length": str(len(body))}

    async def stream(self):
        yield self._body


class _BodyReq:
    def __init__(self, *, content_length, chunks):
        self.headers = {} if content_length is None else {"content-length": str(content_length)}
        self._chunks = chunks

    async def stream(self):
        for c in self._chunks:
            yield c


class FakeRedis:
    async def incr(self, key):
        return 1  # sub orice prag

    async def expire(self, *a):
        return True

    async def get(self, key):
        return None

    async def incrbyfloat(self, key, amount):
        return amount


class RaisingRedis(FakeRedis):
    async def incr(self, key):
        raise RedisError("down")

    async def get(self, key):
        raise RedisError("down")


class OverBudgetRedis(FakeRedis):
    async def get(self, key):
        return "999"  # orice cheie de cost → peste plafon


# --- enforce_body_cap --------------------------------------------------------


async def test_body_cap_content_length_over_max():
    with pytest.raises(HTTPException) as ei:
        await enforce_body_cap(_BodyReq(content_length=100, chunks=[b"x" * 100]), 50)
    assert ei.value.status_code == 413


async def test_body_cap_content_length_absent_rejected():
    with pytest.raises(HTTPException) as ei:
        await enforce_body_cap(_BodyReq(content_length=None, chunks=[b"x"]), 50)
    assert ei.value.status_code == 413


async def test_body_cap_lying_content_length_caught_by_stream():
    # declară 10 (≤ max 20), dar trimite 30 → stream-limit prinde depășirea
    with pytest.raises(HTTPException) as ei:
        await enforce_body_cap(_BodyReq(content_length=10, chunks=[b"x" * 15, b"x" * 15]), 20)
    assert ei.value.status_code == 413


async def test_body_cap_negative_content_length_400():
    with pytest.raises(HTTPException) as ei:
        await enforce_body_cap(_BodyReq(content_length=-1, chunks=[b""]), 1000)
    assert ei.value.status_code == 400  # CL negativ = invalid (RFC), poate desincroniza proxy


async def test_body_cap_ok_returns_bytes():
    assert await enforce_body_cap(_BodyReq(content_length=5, chunks=[b"hello"]), 50) == b"hello"


async def test_body_cap_exactly_at_max_passes():
    assert await enforce_body_cap(_BodyReq(content_length=5, chunks=[b"hello"]), 5) == b"hello"


# --- web_rate_limited fail-closed vs fail-open -------------------------------


async def test_rate_limited_fail_closed_on_redis_error():
    assert await wa.web_rate_limited(RaisingRedis(), "t", "ip", "v", fail_closed=True) is True


async def test_rate_limited_fail_open_on_redis_error():
    assert await wa.web_rate_limited(RaisingRedis(), "t", "ip", "v", fail_closed=False) is False


# --- /web/chat: fail-CLOSED + cost-cap (handle_turn NEapelat) ----------------


def _setup_chat(monkeypatch, redis):
    called = {"handle_turn": False}

    async def fake_verify(token, vid, sig):
        return WebSession(business_id="b", token=token, visitor_id=vid)

    async def fake_resolve_channel(conn, kind, token):
        return {"channel_id": "chan", "business_id": "b"}

    async def fake_load_business(conn, bid):
        return SimpleNamespace(id=bid, daily_cost_cap_usd=None)

    async def fake_handle_turn(*a, **k):
        called["handle_turn"] = True
        return TurnResult("c", "ct", "t", "hi", None, reply=None, language="ro")

    monkeypatch.setattr(wa, "_verify", fake_verify)
    monkeypatch.setattr(wa, "get_redis", lambda: _coro(redis))
    monkeypatch.setattr(wa, "get_pool", lambda: _coro(None))
    monkeypatch.setattr(wa, "admin_conn", _fake_cm)
    monkeypatch.setattr(wa, "tenant_conn", _fake_cm)
    monkeypatch.setattr(wa, "resolve_channel", fake_resolve_channel)
    monkeypatch.setattr(wa, "load_business", fake_load_business)
    monkeypatch.setattr(wa, "handle_turn", fake_handle_turn)
    return called


def _chat_req():
    return WebChatIn(token="tok", visitor_id="web_1", sig="s", message="x")


async def test_web_chat_fail_closed_when_redis_down(monkeypatch):
    called = _setup_chat(monkeypatch, RaisingRedis())
    with pytest.raises(HTTPException) as ei:
        await wa.web_chat(_chat_req(), _Req())
    assert ei.value.status_code == 429
    assert called["handle_turn"] is False  # pipeline-ul NU rulează (zero LLM)


async def test_web_chat_over_budget_429(monkeypatch):
    called = _setup_chat(monkeypatch, OverBudgetRedis())
    with pytest.raises(HTTPException) as ei:
        await wa.web_chat(_chat_req(), _Req())
    assert ei.value.status_code == 429
    assert called["handle_turn"] is False  # peste cost-cap → fără handle_turn


async def test_web_chat_under_budget_runs(monkeypatch):
    called = _setup_chat(monkeypatch, FakeRedis())
    await wa.web_chat(_chat_req(), _Req())
    assert called["handle_turn"] is True  # sub praguri + redis OK → pipeline rulează


# --- /web/messages: fail-OPEN păstrat ---------------------------------------


async def test_web_message_fail_open_on_redis_error(monkeypatch):
    async def fake_verify(*a):
        return WebSession(business_id="b", token="t", visitor_id="v")

    async def fake_enqueue(redis, event):
        return "x"

    monkeypatch.setattr(wa, "_verify", fake_verify)
    monkeypatch.setattr(wa, "enqueue_inbound", fake_enqueue)
    monkeypatch.setattr(wa, "get_redis", lambda: _coro(RaisingRedis()))
    res = await wa.web_message(WebMessageIn(token="t", visitor_id="v", sig="s", text="hi"), _Req())
    assert res["accepted"] is True  # fail-OPEN: redis jos nu blochează ingestia ieftină


# --- /web/bootstrap: verificare Origin server-side --------------------------


def _settings_with_origins(origins):
    return SimpleNamespace(web_cors_origins_list=origins)


async def test_bootstrap_origin_not_allowlisted_403(monkeypatch):
    monkeypatch.setattr(wa, "get_settings", lambda: _settings_with_origins(["https://shop.ro"]))
    req = _Req(headers={"origin": "https://evil.example"})
    with pytest.raises(HTTPException) as ei:
        await wa.web_bootstrap("tok", req)
    assert ei.value.status_code == 403  # respins înainte de a emite sesiunea


async def test_bootstrap_origin_allowlisted_ok(monkeypatch):
    monkeypatch.setattr(wa, "get_settings", lambda: _settings_with_origins(["https://shop.ro"]))

    async def fake_resolve(token):
        return {"business_id": "b", "session_secret": "sek"}

    monkeypatch.setattr(wa, "_resolve_token", fake_resolve)
    res = await wa.web_bootstrap("tok", _Req(headers={"origin": "https://shop.ro"}))
    assert res["token"] == "tok"


async def test_bootstrap_no_origin_ok(monkeypatch):
    monkeypatch.setattr(wa, "get_settings", lambda: _settings_with_origins(["https://shop.ro"]))

    async def fake_resolve(token):
        return {"business_id": "b", "session_secret": "sek"}

    monkeypatch.setattr(wa, "_resolve_token", fake_resolve)
    res = await wa.web_bootstrap("tok", _Req(headers={}))  # same-origin / non-browser → permis
    assert res["token"] == "tok"


async def test_bootstrap_cors_disabled_with_origin_403(monkeypatch):
    # NX-120 secure-by-default: allowlist GOL + Origin de browser → 403 (nu permitem orice origin).
    monkeypatch.setattr(wa, "get_settings", lambda: _settings_with_origins([]))
    with pytest.raises(HTTPException) as ei:
        await wa.web_bootstrap("tok", _Req(headers={"origin": "https://evil.example"}))
    assert ei.value.status_code == 403


# --- middleware de body-size (respinge declarat-mare înainte de routing/parsing) ---


def test_request_size_middleware_rejects_oversized():
    from fastapi.testclient import TestClient

    from src.webhook.app import app as webhook_app

    client = TestClient(webhook_app)
    res = client.post("/webhook", content=b"x" * 262145)  # > 256KB cap grosier global
    assert res.status_code == 413  # respins de middleware, înainte de verificarea semnăturii
