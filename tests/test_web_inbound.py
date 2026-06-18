"""NX-20b — marginea de intrare web (`/web/bootstrap` + `/web/messages`) + rate limit.

ZERO rețea/DB reală: fake Redis (incr/expire/set/xadd), fake admin conn (întoarce rândul de canal),
dependențe override-uite. Acoperă: bootstrap valid/necunoscut, envelope corect pe stream, semnătură
invalidă 403, buget input 400, rate limit 429 (IP rotit + visitor), dedupe L1 idempotent, 404 când
canalul e OFF, P12 (token/visitor_id/IP nu apar în loguri).
"""

import logging

import pytest
from fastapi.testclient import TestClient

from src.web import app as web_app
from src.web.limits import web_rate_limited
from src.web.session import get_session_cache, issue_visitor
from src.webhook.app import app

TOKEN = "pub_demo"
SECRET = "session-secret-de-test"


# --- fakes -------------------------------------------------------------------


class FakeRedis:
    def __init__(self):
        self.kv: dict = {}
        self.streams: list = []

    async def incr(self, key):
        self.kv[key] = int(self.kv.get(key, 0)) + 1
        return self.kv[key]

    async def expire(self, key, ttl):
        return True

    async def set(self, key, val, nx=False, ex=None):
        if nx and key in self.kv:
            return None
        self.kv[key] = val
        return True

    async def xadd(self, stream, fields, **kw):
        self.streams.append((stream, fields))
        return "1-0"


class FakeConn:
    """Rândul de canal webchat pt `resolve_web_session` (secret=None → canal inexistent)."""

    def __init__(self, secret):
        self._secret = secret

    async def fetchrow(self, sql, *args):
        if self._secret is None:
            return None
        return {"business_id": "biz-1", "session_secret": self._secret}


@pytest.fixture
def fake_redis():
    return FakeRedis()


@pytest.fixture
def client(fake_redis):
    """TestClient cu dependențele de DB/Redis/kill-switch override-uite. `secret` controlabil per
    test prin `client.app.state._secret` (default = canal valid)."""
    get_session_cache.cache_clear()  # singleton proaspăt → fără cache otrăvit între teste
    state = {"secret": SECRET}

    async def _conn():
        yield FakeConn(state["secret"])

    app.dependency_overrides[web_app.admin_conn_dep] = _conn
    app.dependency_overrides[web_app.redis_dep] = lambda: fake_redis
    app.dependency_overrides[web_app.require_web_enabled] = lambda: None
    c = TestClient(app)
    c._state = state
    yield c
    app.dependency_overrides.clear()


# --- bootstrap ---------------------------------------------------------------


def test_bootstrap_issues_verifiable_session(client):
    r = client.get("/web/bootstrap", params={"token": TOKEN})
    assert r.status_code == 200
    data = r.json()
    assert data["public_token"] == TOKEN
    assert data["visitor_id"].startswith("web_")
    assert TOKEN in data["sse_url"] and data["visitor_id"] in data["sse_url"]


def test_bootstrap_unknown_token_403(client):
    client._state["secret"] = None  # canal inexistent
    r = client.get("/web/bootstrap", params={"token": "pub_x"})
    assert r.status_code == 403


# --- POST /web/messages ------------------------------------------------------


def _valid_session():
    visitor_id, sig = issue_visitor(TOKEN, SECRET)
    return visitor_id, sig


def test_message_valid_enqueues_neutral_envelope(client, fake_redis):
    visitor_id, sig = _valid_session()
    r = client.post(
        "/web/messages",
        json={"token": TOKEN, "visitor_id": visitor_id, "sig": sig, "text": "caut o cremă"},
    )
    assert r.status_code == 200 and r.json()["accepted"] is True
    assert len(fake_redis.streams) == 1
    stream, fields = fake_redis.streams[0]
    assert stream == "inbound"
    import json

    ev = json.loads(fields["data"])
    assert ev["kind"] == "message"
    assert ev["channel_kind"] == "webchat"
    assert ev["channel_account_id"] == TOKEN  # public token = canalul receptor
    assert ev["sender_external_id"] == visitor_id  # vizitatorul = userul pe canal
    assert ev["body"] == "caut o cremă"


def test_message_bad_sig_403_no_enqueue(client, fake_redis):
    r = client.post(
        "/web/messages",
        json={"token": TOKEN, "visitor_id": "web_oarecare", "sig": "deadbeef", "text": "hi"},
    )
    assert r.status_code == 403
    assert fake_redis.streams == []


def test_message_empty_text_400(client, fake_redis):
    visitor_id, sig = _valid_session()
    r = client.post(
        "/web/messages",
        json={"token": TOKEN, "visitor_id": visitor_id, "sig": sig, "text": "   "},
    )
    assert r.status_code == 400
    assert fake_redis.streams == []


def test_message_too_long_400(client, fake_redis):
    visitor_id, sig = _valid_session()
    r = client.post(
        "/web/messages",
        json={"token": TOKEN, "visitor_id": visitor_id, "sig": sig, "text": "x" * 2001},
    )
    assert r.status_code == 400
    assert fake_redis.streams == []


def test_message_dedupes_on_client_msg_id(client, fake_redis):
    visitor_id, sig = _valid_session()
    payload = {
        "token": TOKEN,
        "visitor_id": visitor_id,
        "sig": sig,
        "text": "salut",
        "client_msg_id": "c-1",
    }
    r1 = client.post("/web/messages", json=payload)
    r2 = client.post("/web/messages", json=payload)
    assert r1.status_code == r2.status_code == 200
    assert r2.json().get("deduped") is True
    assert len(fake_redis.streams) == 1  # al doilea nu re-enqueue


# --- kill-switch -------------------------------------------------------------


def test_disabled_channel_404(fake_redis):
    """Fără override pe require_web_enabled → settings.web_enabled=False (default) → 404."""
    get_session_cache.cache_clear()

    async def _conn():
        yield FakeConn(SECRET)

    app.dependency_overrides[web_app.admin_conn_dep] = _conn
    app.dependency_overrides[web_app.redis_dep] = lambda: fake_redis
    try:
        r = TestClient(app).get("/web/bootstrap", params={"token": TOKEN})
        assert r.status_code == 404
    finally:
        app.dependency_overrides.clear()


# --- rate limit (unit, fără HTTP) --------------------------------------------


async def test_rate_limit_ip_catches_visitor_rotation(fake_redis):
    # 20 mesaje, visitor_id rotit la fiecare → contorul de visitor nu sare, dar cel de IP da
    tripped = None
    for i in range(20):
        tripped = await web_rate_limited(
            fake_redis, TOKEN, "1.2.3.4", f"web_{i}", max_ip=15, max_visitor=15, window_s=60
        )
        if tripped:
            break
    assert tripped == "ip"
    assert fake_redis.kv[f"webrl:ip:{TOKEN}:1.2.3.4"] == 16  # a sărit la al 16-lea (>15)


async def test_rate_limit_visitor_catches_single_browser(fake_redis):
    tripped = None
    for _ in range(20):
        tripped = await web_rate_limited(
            fake_redis, TOKEN, "", "web_same", max_ip=999, max_visitor=10, window_s=60
        )
        if tripped:
            break
    assert tripped == "visitor"


async def test_rate_limit_under_threshold_passes(fake_redis):
    out = await web_rate_limited(
        fake_redis, TOKEN, "9.9.9.9", "web_1", max_ip=40, max_visitor=15, window_s=60
    )
    assert out is None


# --- P12: fără PII în loguri -------------------------------------------------


def test_no_pii_in_logs(client, fake_redis, caplog):
    visitor_id, sig = _valid_session()
    with caplog.at_level(logging.DEBUG):
        client.post(
            "/web/messages",
            json={"token": TOKEN, "visitor_id": visitor_id, "sig": sig, "text": "ceva"},
        )
    blob = "\n".join(rec.getMessage() for rec in caplog.records)
    assert TOKEN not in blob
    assert visitor_id not in blob
