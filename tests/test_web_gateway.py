"""NX-20 — gateway web (SSE). Fără rețea/DB reală: fake redis (incr/pubsub/list) + seam-uri
(`_verify`/`_resolve_token`/`get_redis`/`enqueue_inbound`) monkeypatch-uite. Acoperă: envelope
webchat pe stream, rate limit IP+visitor, bootstrap HMAC, WebSender publish+backlog, replay
Last-Event-ID, formatul SSE, un eveniment livrat pe stream, build_registry."""

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pydantic
import pytest

from src.channels.web.sender import WebSender
from src.web import app as wa
from src.web.app import WebChatIn, WebMessageIn
from src.web.session import WebSession, verify_sig
from src.worker.processor import TurnResult


class FakeRedis:
    def __init__(self, incr_value=None):
        self._incr_value = incr_value
        self.counters: dict = {}
        self.published: list = []
        self.lists: dict = {}
        self.expires: list = []
        self._pubsub = None

    async def incr(self, key):
        if self._incr_value is not None:
            return self._incr_value
        self.counters[key] = self.counters.get(key, 0) + 1
        return self.counters[key]

    async def expire(self, key, ttl):
        self.expires.append((key, ttl))
        return True

    async def publish(self, channel, data):
        self.published.append((channel, data))
        return 1

    async def rpush(self, key, val):
        self.lists.setdefault(key, []).append(val)
        return len(self.lists[key])

    async def ltrim(self, key, start, stop):
        return True

    async def lrange(self, key, start, stop):
        return list(self.lists.get(key, []))

    def pubsub(self):
        return self._pubsub


class _Req:
    def __init__(self, host="1.2.3.4"):
        self.client = SimpleNamespace(host=host)


# --- WebMessageIn (buget de input dur) ---------------------------------------


def test_message_text_too_long_rejected():
    with pytest.raises(pydantic.ValidationError):
        WebMessageIn(token="t", visitor_id="v", sig="s", text="x" * 2001)


def test_message_text_empty_rejected():
    with pytest.raises(pydantic.ValidationError):
        WebMessageIn(token="t", visitor_id="v", sig="s", text="")


# --- POST /web/messages ------------------------------------------------------


async def test_message_enqueues_webchat_envelope(monkeypatch):
    captured = {}

    async def fake_verify(token, vid, sig):
        return WebSession(business_id="b", token=token, visitor_id=vid)

    async def fake_enqueue(redis, event):
        captured["event"] = event
        return "x"

    monkeypatch.setattr(wa, "_verify", fake_verify)
    monkeypatch.setattr(wa, "enqueue_inbound", fake_enqueue)
    fr = FakeRedis()
    monkeypatch.setattr(wa, "get_redis", lambda: _coro(fr))

    req = WebMessageIn(token="tok", visitor_id="web_1", sig="s", text="  salut  ")
    res = await wa.web_message(req, _Req())

    assert res["accepted"] is True
    ev = captured["event"]
    assert ev["kind"] == "message" and ev["channel_kind"] == "webchat"
    assert ev["channel_account_id"] == "tok"  # public token
    assert ev["sender_external_id"] == "web_1"  # visitor_id
    assert ev["body"] == "salut"  # trim


async def test_message_invalid_session_403(monkeypatch):
    async def none_verify(*a):
        return None

    monkeypatch.setattr(wa, "_verify", none_verify)
    with pytest.raises(wa.HTTPException) as ei:
        await wa.web_message(WebMessageIn(token="t", visitor_id="v", sig="bad", text="x"), _Req())
    assert ei.value.status_code == 403


async def test_message_rate_limited_429(monkeypatch):
    async def fake_verify(*a):
        return WebSession(business_id="b", token="t", visitor_id="v")

    monkeypatch.setattr(wa, "_verify", fake_verify)
    monkeypatch.setattr(wa, "get_redis", lambda: _coro(FakeRedis(incr_value=999)))
    with pytest.raises(wa.HTTPException) as ei:
        await wa.web_message(WebMessageIn(token="t", visitor_id="v", sig="s", text="x"), _Req())
    assert ei.value.status_code == 429


# --- GET /web/bootstrap ------------------------------------------------------


async def test_bootstrap_issues_verifiable_session(monkeypatch):
    async def fake_resolve(token):
        return {"business_id": "b", "session_secret": "sek"}

    monkeypatch.setattr(wa, "_resolve_token", fake_resolve)
    res = await wa.web_bootstrap("tok")
    assert res["token"] == "tok" and res["visitor_id"].startswith("web_")
    assert verify_sig("tok", res["visitor_id"], res["sig"], "sek")  # semnătura e validă


async def test_bootstrap_unknown_token_403(monkeypatch):
    async def none_resolve(token):
        return None

    monkeypatch.setattr(wa, "_resolve_token", none_resolve)
    with pytest.raises(wa.HTTPException) as ei:
        await wa.web_bootstrap("nope")
    assert ei.value.status_code == 403


# --- WebSender (Pub/Sub + backlog) -------------------------------------------


async def test_websender_publishes_and_backlogs():
    fr = FakeRedis()
    sender = WebSender(fr, backlog_size=20, backlog_ttl_s=300)
    mid = await sender.send_text("tok", "web_1", "salut")

    assert mid.startswith("web_out_")
    assert fr.published[0][0] == "web:out:web_1"
    evt = json.loads(fr.published[0][1])
    assert evt == {"id": mid, "type": "text", "text": "salut"}
    assert fr.lists["web:backlog:web_1"]  # scris în backlog pt reconectare
    assert ("web:backlog:web_1", 300) in fr.expires


# --- reconectare + format SSE ------------------------------------------------


async def test_replay_after_returns_events_after_id():
    fr = FakeRedis()
    fr.lists["web:backlog:web_1"] = [
        json.dumps({"id": f"web_out_{i}", "type": "text", "text": str(i)}) for i in (3, 4, 5)
    ]
    out = await wa._replay_after(fr, "web_1", "web_out_3")
    assert [e["text"] for e in out] == ["4", "5"]  # DOAR după id-ul confirmat


async def test_replay_after_empty_without_last_id():
    assert await wa._replay_after(FakeRedis(), "web_1", None) == []


def test_sse_frame_format():
    frame = wa._sse({"id": "web_out_x", "type": "text", "text": "hi"})
    assert frame.startswith("id: web_out_x\ndata: ") and frame.endswith("\n\n")
    assert "hi" in frame


# --- GET /web/stream (un eveniment livrat) -----------------------------------


class _FakePubSub:
    def __init__(self, messages):
        self._messages = list(messages)
        self.unsubscribed: list = []

    async def subscribe(self, ch):
        pass

    async def unsubscribe(self, ch):
        self.unsubscribed.append(ch)

    async def get_message(self, timeout=None, ignore_subscribe_messages=True):
        return self._messages.pop(0) if self._messages else None


class _StreamReq:
    def __init__(self, disconnect_after):
        self._n = 0
        self._after = disconnect_after
        self.client = SimpleNamespace(host="1.1.1.1")

    async def is_disconnected(self):
        self._n += 1
        return self._n > self._after


async def test_stream_emits_published_event(monkeypatch):
    async def fake_verify(*a):
        return WebSession(business_id="b", token="t", visitor_id="web_1")

    monkeypatch.setattr(wa, "_verify", fake_verify)
    evt = {"id": "web_out_1", "type": "text", "text": "hi"}
    pubsub = _FakePubSub([{"data": json.dumps(evt)}])
    fr = FakeRedis()
    fr._pubsub = pubsub
    monkeypatch.setattr(wa, "get_redis", lambda: _coro(fr))

    resp = await wa.web_stream(
        "t", "web_1", "s", _StreamReq(disconnect_after=1), last_event_id=None
    )
    chunks = [c async for c in resp.body_iterator]

    assert any("web_out_1" in c and "hi" in c for c in chunks)
    assert pubsub.unsubscribed == ["web:out:web_1"]  # cleanup în finally (fără leak de subscriber)


# --- build_registry (NX-20: webchat doar cu redis + web_enabled) -------------


def _settings(**kw):
    base = dict(
        meta_access_token="",
        telegram_bot_token="",
        web_enabled=True,
        web_backlog_size=20,
        web_backlog_ttl_s=300,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_build_registry_registers_webchat_with_redis():
    from src.worker.dispatcher import build_registry

    reg = build_registry(None, _settings(), FakeRedis())
    assert reg.get("webchat") is not None


def test_build_registry_no_webchat_without_redis():
    from src.worker.dispatcher import build_registry

    reg = build_registry(None, _settings(), None)
    assert reg.get("webchat") is None


def test_build_registry_no_webchat_when_disabled():
    from src.worker.dispatcher import build_registry

    reg = build_registry(None, _settings(web_enabled=False), FakeRedis())
    assert reg.get("webchat") is None


# --- POST /web/chat (NX-25b — sincron request/response) ----------------------


@asynccontextmanager
async def _fake_cm(*a, **k):
    """admin_conn / tenant_conn fake (yield None) — handle_turn e oricum monkeypatch-uit."""
    yield None


def test_chat_in_message_too_long_rejected():
    with pytest.raises(pydantic.ValidationError):
        WebChatIn(token="t", visitor_id="v", sig="s", message="x" * 2001)


def test_chat_in_ignores_extra_history():
    # frontendul trimite și `history` → ignorat (serverul e sursa de adevăr pe visitor_id)
    req = WebChatIn(token="t", visitor_id="v", sig="s", message="hei", history=[{"role": "user"}])
    assert req.message == "hei"


def test_build_chat_response_maps_rich_items_and_chips():
    from src.models import Chip, Reply, RichItem, RichReply

    rich = RichReply(
        intro="Pentru ten gras:",
        items=[
            RichItem(
                product_id="p1",
                name="Ser X",
                price=89.0,
                image="http://img/1.jpg",
                url="http://shop/p1",
                rating=4.8,
                reason="bun la sebum",
            )
        ],
        pick=("p1", "cel mai bun fit"),
        education="curăță delicat",
        chips=[Chip(label="Mai ieftin", payload="chip:cheaper")],
        disclaimer="Funcționez cu inteligență artificială, așa că pot greși uneori.",
    )
    reply = Reply(text="1. Ser X — 89.00 lei\n\nÎți recomand Ser X.", rich=rich)
    res = wa._build_chat_response(
        TurnResult("c", "ct", "t", reply.text, None, reply=reply, language="ro")
    )
    content = res["content"]
    # Widget: content = DOAR framing (intro + pick + educație), NU enumerarea cu preț.
    assert "Pentru ten gras:" in content  # intro
    assert "Recomandarea mea: Ser X" in content  # pick numește produsul
    assert "curăță delicat" in content  # educație
    assert "89" not in content  # FĂRĂ enumerarea cu preț (o fac cardurile)
    assert "1. Ser X" not in content  # FĂRĂ lista numerotată (flatten complet)
    card = res["products"][0]
    assert card["name"] == "Ser X" and card["price"] == 89.0  # prețul e pe CARD
    assert card["image_url"] == "http://img/1.jpg" and card["rating"] == 4.8
    assert card["reason"] == "bun la sebum"
    assert res["suggestions"] == ["Mai ieftin"]


def test_build_chat_response_maps_simple_products():
    from src.models import Reply

    reply = Reply(
        text="Uite ceva.",
        products=[
            {
                "product_id": "p1",
                "name": "Cremă",
                "price": 49.0,
                "url": "http://shop/p1",
                "image": "http://img/p1.jpg",
            }
        ],
    )
    res = wa._build_chat_response(
        TurnResult("c", "ct", "t", reply.text, None, reply=reply, language="ro")
    )
    assert res["products"][0] == {
        "product_id": "p1",
        "name": "Cremă",
        "price": 49.0,
        "image_url": "http://img/p1.jpg",
        "url": "http://shop/p1",
    }
    assert res["suggestions"] == []


def test_build_chat_response_no_reply_is_empty():
    res = wa._build_chat_response(TurnResult("c", "ct", "t", None, None, reply=None))
    assert res == {"content": "", "products": [], "suggestions": []}


async def test_web_chat_returns_reply_synchronously(monkeypatch):
    from src.models import Reply

    captured = {}

    async def fake_verify(token, vid, sig):
        return WebSession(business_id="b", token=token, visitor_id=vid)

    async def fake_resolve_channel(conn, kind, token):
        assert kind == "webchat"
        return {"channel_id": "chan", "business_id": "b"}

    async def fake_load_business(conn, bid):
        return SimpleNamespace(id=bid)

    async def fake_handle_turn(conn, business, channel_id, event, *, redis=None, deliver=True):
        captured["deliver"] = deliver
        captured["event"] = event
        reply = Reply(
            text="Salut!",
            products=[{"product_id": "p1", "name": "X", "price": 10.0, "url": None, "image": None}],
        )
        return TurnResult("c", "ct", "t", reply.text, None, reply=reply, language="ro")

    monkeypatch.setattr(wa, "_verify", fake_verify)
    monkeypatch.setattr(wa, "get_redis", lambda: _coro(FakeRedis()))
    monkeypatch.setattr(wa, "get_pool", lambda: _coro(None))
    monkeypatch.setattr(wa, "admin_conn", _fake_cm)
    monkeypatch.setattr(wa, "tenant_conn", _fake_cm)
    monkeypatch.setattr(wa, "resolve_channel", fake_resolve_channel)
    monkeypatch.setattr(wa, "load_business", fake_load_business)
    monkeypatch.setattr(wa, "handle_turn", fake_handle_turn)

    res = await wa.web_chat(
        WebChatIn(token="tok", visitor_id="web_1", sig="s", message="  hei  "), _Req()
    )

    assert captured["deliver"] is False  # sync: NU prin outbox
    assert captured["event"]["body"] == "hei"  # trim
    assert captured["event"]["channel_account_id"] == "tok"
    assert "Salut!" in res["content"]
    assert res["products"][0]["name"] == "X"


async def test_web_chat_invalid_session_403(monkeypatch):
    async def none_verify(*a):
        return None

    monkeypatch.setattr(wa, "_verify", none_verify)
    with pytest.raises(wa.HTTPException) as ei:
        await wa.web_chat(WebChatIn(token="t", visitor_id="v", sig="bad", message="x"), _Req())
    assert ei.value.status_code == 403


async def test_web_chat_rate_limited_429(monkeypatch):
    async def fake_verify(*a):
        return WebSession(business_id="b", token="t", visitor_id="v")

    monkeypatch.setattr(wa, "_verify", fake_verify)
    monkeypatch.setattr(wa, "get_redis", lambda: _coro(FakeRedis(incr_value=999)))
    with pytest.raises(wa.HTTPException) as ei:
        await wa.web_chat(WebChatIn(token="t", visitor_id="v", sig="s", message="x"), _Req())
    assert ei.value.status_code == 429


# --- helper -----------------------------------------------------------------


async def _coro(value):
    return value
