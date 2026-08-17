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
        self.kv: dict = {}  # NX-120: get/incrbyfloat (cost guard)
        self.published: list = []
        self.lists: dict = {}
        self.expires: list = []
        self._pubsub = None

    async def incr(self, key):
        if self._incr_value is not None:
            return self._incr_value
        self.counters[key] = self.counters.get(key, 0) + 1
        return self.counters[key]

    async def get(self, key):
        v = self.kv.get(key)
        return str(v) if v is not None else None

    async def incrbyfloat(self, key, amount):
        self.kv[key] = float(self.kv.get(key, 0)) + float(amount)
        return self.kv[key]

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

    def pipeline(self, transaction=True):
        # NX-127: backlog atomic — buffer-uiește rpush/ltrim/expire, le aplică pe execute().
        return _GwPipe(self)

    def pubsub(self):
        return self._pubsub


class _GwPipe:
    def __init__(self, r):
        self._r = r
        self._q: list = []

    def rpush(self, *a):
        self._q.append(("rpush", a))
        return self

    def ltrim(self, *a):
        self._q.append(("ltrim", a))
        return self

    def expire(self, *a):
        self._q.append(("expire", a))
        return self

    async def execute(self):
        return [await getattr(self._r, op)(*args) for op, args in self._q]


class _Req:
    def __init__(self, host="1.2.3.4", body=b"{}", headers=None):
        self.client = SimpleNamespace(host=host)
        self._body = body
        # NX-120: enforce_body_cap citește Content-Length + request.stream().
        self.headers = headers if headers is not None else {"content-length": str(len(body))}

    async def stream(self):
        yield self._body


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


class _BootRedis:
    """Redis minimal pentru limita de bootstrap (NX-229)."""

    def __init__(self) -> None:
        self.counts: dict[str, int] = {}

    async def incr(self, key):
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]

    async def expire(self, key, ttl):
        return True


async def test_bootstrap_issues_verifiable_session(monkeypatch):
    async def fake_resolve(token):
        return {"business_id": "b", "session_secret": "sek"}

    monkeypatch.setattr(wa, "_resolve_token", fake_resolve)
    monkeypatch.setattr(wa, "get_redis", lambda: _coro(_BootRedis()))
    res = await wa.web_bootstrap("tok", _Req())
    assert res["token"] == "tok" and res["visitor_id"].startswith("web_")
    assert verify_sig("tok", res["visitor_id"], res["sig"], "sek")  # semnătura e validă


async def test_bootstrap_unknown_token_403(monkeypatch):
    async def none_resolve(token):
        return None

    monkeypatch.setattr(wa, "_resolve_token", none_resolve)
    with pytest.raises(wa.HTTPException) as ei:
        await wa.web_bootstrap("nope", _Req())
    assert ei.value.status_code == 403


# --- NX-244: copy-ul de shell la bootstrap -----------------------------------
# Widgetul v2 are nevoie de eticheta launcherului și de placeholderul composerului ÎNAINTE să
# existe vreun view. Fără ele le-ar inventa în browser, iar boundary-ul „frontend pasiv" cade.


async def _bootstrap(monkeypatch, *, locale="ro", v2=True):
    async def fake_resolve(token):
        return {"business_id": "b", "session_secret": "sek", "default_locale": locale}

    monkeypatch.setattr(wa, "_resolve_token", fake_resolve)
    monkeypatch.setattr(wa, "get_redis", lambda: _coro(_BootRedis()))
    monkeypatch.setattr(wa.get_settings(), "web_turn_v2_enabled", v2)
    return await wa.web_bootstrap("tok", _Req())


async def test_bootstrap_without_v2_is_byte_identic(monkeypatch):
    """Flagul stins ⇒ exact aceleași chei ca înainte. Calea v1 nu vede nimic nou."""
    res = await _bootstrap(monkeypatch, v2=False)
    assert set(res) == {"token", "visitor_id", "sig", "sse_url"}


async def test_bootstrap_serves_shell_copy_for_v2(monkeypatch):
    res = await _bootstrap(monkeypatch)
    copy = res["view_copy"]
    assert set(copy) == {"composer", "chrome", "a11y"}
    # Composerul e activ: la bootstrap nu există turn în lucru care să blocheze inputul.
    assert copy["composer"]["enabled"] is True
    # Fiecare nume accesibil e prezent ȘI non-blank — un label gol e la fel de rău ca unul lipsă,
    # fiindcă FE-ul ar „umple golul" exact cum interzice cardul.
    for key in (
        "launcher_label",
        "dialog_title",
        "dialog_description",
        "close_label",
        "new_chat_label",
    ):
        assert copy["chrome"][key].strip()
    for key in ("label", "placeholder", "send_label"):
        assert copy["composer"][key].strip()
    # Un anunț pentru FIECARE status de sârmă: un status fără anunț e tăcere pentru cine nu vede
    # ecranul (live region-ul NX-245 le citește direct).
    assert set(copy["a11y"]["announcements"]) == {
        "accepted",
        "working",
        "validating",
        "completed",
        "failed",
        "cancelled",
    }


async def test_bootstrap_shell_copy_is_locale_aware(monkeypatch):
    """D3/P11: limba e configurație, nu constantă. Un tenant `en` primește copy `en`."""
    ro = (await _bootstrap(monkeypatch, locale="ro"))["view_copy"]
    en = (await _bootstrap(monkeypatch, locale="en"))["view_copy"]
    assert ro["chrome"]["dialog_title"] != en["chrome"]["dialog_title"]
    # Locale absent/necunoscut nu e o eroare: cade pe pilot, nu pe un shell fără nume.
    for missing in (None, "kl-KL"):
        fallback = (await _bootstrap(monkeypatch, locale=missing))["view_copy"]
        assert fallback["chrome"]["dialog_title"] == ro["chrome"]["dialog_title"]


async def test_bootstrap_shell_copy_matches_view_envelope(monkeypatch):
    """UN singur vocabular. Dacă `view_copy` și `chrome`-ul unui view ar putea diverge, frontendul
    ar arăta un titlu la deschidere și altul după primul răspuns."""
    from src.channels.web.render_v2 import TurnIdentity, _envelope

    res = await _bootstrap(monkeypatch, locale="ro")
    view = _envelope(
        TurnIdentity(
            conversation_id="c",
            conversation_revision=1,
            turn_id="t",
            client_turn_id="3f2504e0-4f89-41d3-9a0c-0305e82c3301",
            status="completed",
        ),
        "ro",
        [{"id": "b1", "type": "text", "text": "ok"}],
    )
    assert res["view_copy"]["chrome"] == view["chrome"]
    assert res["view_copy"]["composer"] == view["composer"]
    assert res["view_copy"]["a11y"] == view["a11y"]


# --- WebSender (Pub/Sub + backlog) -------------------------------------------


async def test_websender_publishes_and_backlogs():
    fr = FakeRedis()
    sender = WebSender(fr, backlog_size=20, backlog_ttl_s=300)
    mid = await sender.send_text("tok", "web_1", "salut")

    assert mid.startswith("web_out_")
    assert fr.published[0][0] == "web:out:tok:web_1"  # NX-120: prefix tenant (token)
    evt = json.loads(fr.published[0][1])
    assert evt == {"id": mid, "type": "text", "text": "salut"}
    assert fr.lists["web:backlog:tok:web_1"]  # scris în backlog pt reconectare (prefix tenant)
    assert ("web:backlog:tok:web_1", 300) in fr.expires


# --- reconectare + format SSE ------------------------------------------------


async def test_replay_after_returns_events_after_id():
    fr = FakeRedis()
    fr.lists["web:backlog:tok:web_1"] = [
        json.dumps({"id": f"web_out_{i}", "type": "text", "text": str(i)}) for i in (3, 4, 5)
    ]
    out = await wa._replay_after(fr, "tok", "web_1", "web_out_3")
    assert [e["text"] for e in out] == ["4", "5"]  # DOAR după id-ul confirmat


async def test_replay_after_empty_without_last_id():
    assert await wa._replay_after(FakeRedis(), "tok", "web_1", None) == []


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
        # NX-229: poarta de origin se aplică și pe SSE, deci stub-ul are nevoie de headere.
        self.headers = {}

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
    assert pubsub.unsubscribed == ["web:out:t:web_1"]  # cleanup în finally (prefix tenant=token)


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
        disclaimer=None,  # disclaimer OFF default (#2) — nu mai face parte din framing
    )
    reply = Reply(text="1. Ser X — 89.00 lei\n\nÎți recomand Ser X.", rich=rich)
    res = wa._build_chat_response(
        TurnResult("c", "ct", "t", reply.text, None, reply=reply, language="ro")
    )
    content = res["content"]
    # Widget (#4): framing UȘOR — intro + coaching de final (IZI: `education` revine pe widget);
    # la UN produs FĂRĂ „Recomandarea mea", fără disclaimer (default off). Prețul îl fac cardurile.
    assert "Pentru ten gras:" in content  # intro
    assert "Recomandarea mea" not in content  # un singur produs → fără pick separat
    assert "curăță delicat" in content  # IZI: coaching de final randat pe widget (era omis)
    assert "inteligență" not in content  # disclaimer OFF default
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
        return SimpleNamespace(id=bid, daily_cost_cap_usd=None)  # NX-120: cap citit la admitere

    async def fake_handle_turn(
        conn, business, channel_id, event, *, redis=None, deliver=True, defer_aftercare=False
    ):
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
    monkeypatch.setattr(wa, "tenant_db", lambda business_id: _fake_cm)
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


async def test_web_chat_uses_real_pipeline_and_aftercare_cost(monkeypatch):
    from src.models import Reply

    captured = {}
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
        assert supplied_redis is redis
        assert work.ctx.usage.cost_usd == 0.003
        return 0.004

    async def fake_add_visitor(supplied_redis, business_id, visitor_id, amount):
        captured["args"] = (supplied_redis, business_id, visitor_id, amount)

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

    await wa.web_chat(WebChatIn(token="tok", visitor_id="web_1", sig="s", message="hei"), _Req())

    assert captured["args"] == (redis, "b", "web_1", pytest.approx(0.007))
