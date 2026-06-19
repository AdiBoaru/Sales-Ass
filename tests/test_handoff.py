"""NX-123 / NX-82 — handoff: stagiul HANDOFF + tool request_human + enablement per business.

Fără DB/rețea: set_handoff / notify_operator / request_human monkeypatch-uite. Acoperă: R5
(Route.HANDOFF consumat, nu fallback), tool-ul opt-in per business, escaladarea fără PII (P12),
și degradarea grațioasă (escaladare eșuată → tot răspundem, P6).
"""

from types import SimpleNamespace

import src.tools.handoff_tools  # noqa: F401 — înregistrează request_human în TOOL_REGISTRY
from src.models import BusinessConfig, Contact, InboundMessage, Route, RouteDecision, TurnContext
from src.tools import handoff_tools as ht
from src.tools.base import enabled_tools, run_tool
from src.worker.runner import PipelineDeps
from src.worker.stages import handoff as hs
from src.worker.stages.handoff import handoff_stage


def _ctx(route=Route.HANDOFF) -> TurnContext:
    ctx = TurnContext(
        turn_id="t",
        business=BusinessConfig(id="b", slug="d", name="D"),
        contact=Contact(id="c", business_id="b"),
        message=InboundMessage(provider_msg_id="m", body="vreau să vorbesc cu un om"),
        conversation_id="conv",
    )
    if route is not None:
        ctx.route = RouteDecision(route=route)
    return ctx


def _deps() -> PipelineDeps:
    return PipelineDeps(conn=object(), redis=None, llm=None)


# --- enablement per business (NX-82) -----------------------------------------


def test_request_human_opt_in():
    biz = SimpleNamespace(settings={"tools": {"request_human": True}})
    assert "request_human" in enabled_tools(biz, "sales")


def test_request_human_off_by_default():
    assert "request_human" not in enabled_tools(SimpleNamespace(settings={}), "sales")
    assert "request_human" not in enabled_tools(None, "sales")  # backward-compatible


def test_request_human_not_offered_on_order_route():
    biz = SimpleNamespace(settings={"tools": {"request_human": True}})
    assert "request_human" not in enabled_tools(biz, "order")


def test_disabled_tool_removed_from_set():
    biz = SimpleNamespace(settings={"tools": {"disabled": ["checkout_link"]}})
    tools = enabled_tools(biz, "sales")
    assert "checkout_link" not in tools and "search_products" in tools


# --- stagiul handoff (R5) ----------------------------------------------------


async def test_handoff_stage_escalates_and_replies(monkeypatch):
    calls = []

    async def fake_rh(conn, ctx, reason, *, source, assigned_user_id=None):
        calls.append(("escalate", reason, source))

    async def fake_notify(ctx, reason):
        calls.append(("notify", reason))

    monkeypatch.setattr(hs, "request_human", fake_rh)
    monkeypatch.setattr(hs, "notify_operator", fake_notify)
    ctx = _ctx(route=Route.HANDOFF)
    await handoff_stage(ctx, _deps())

    assert ctx.reply is not None and "coleg" in ctx.reply.text  # NU tăcere (P6)
    assert ctx.reply.cacheable is False
    assert ("escalate", "user_request", "triage") in calls
    assert ("notify", "user_request") in calls


async def test_handoff_stage_noop_on_other_routes(monkeypatch):
    monkeypatch.setattr(hs, "request_human", _unreachable)
    ctx = _ctx(route=Route.SALES)
    await handoff_stage(ctx, _deps())
    assert ctx.reply is None  # nu atinge alte rute


async def test_handoff_stage_replies_even_if_escalation_fails(monkeypatch):
    async def boom(conn, ctx, reason, *, source, assigned_user_id=None):
        raise RuntimeError("db down")

    async def fake_notify(ctx, reason):
        pass

    monkeypatch.setattr(hs, "request_human", boom)
    monkeypatch.setattr(hs, "notify_operator", fake_notify)
    ctx = _ctx(route=Route.HANDOFF)
    await handoff_stage(ctx, _deps())
    assert ctx.reply is not None  # P6: escaladare eșuată → tot confirmăm clientului


async def _unreachable(*a, **k):
    raise AssertionError("nu trebuie chemat")


# --- tool request_human (NX-82) ----------------------------------------------


async def test_request_human_tool_sets_handoff_and_emits(monkeypatch):
    calls = {}

    async def fake_set_handoff(
        conn, business_id, conversation_id, *, window_minutes, risk_flag, assigned_user_id=None
    ):
        calls.update(biz=business_id, conv=conversation_id, flag=risk_flag)

    async def fake_notify(ctx, reason):
        calls["notify"] = reason

    monkeypatch.setattr(ht, "set_handoff", fake_set_handoff)
    monkeypatch.setattr(ht, "notify_operator", fake_notify)
    ctx = _ctx(route=Route.SALES)
    res = await run_tool(ctx, _deps(), "request_human", {"reason": "client nemulțumit de livrare"})

    assert res.ok and "coleg" in res.llm_view.lower()
    assert calls["biz"] == "b" and calls["conv"] == "conv" and calls["flag"] == "agent_request"
    # operatorul primește motivul REAL; eventul de analytics primește un token FIX (P12)
    assert calls["notify"] == "client nemulțumit de livrare"
    ev = next(e for e in ctx.events if e.type == "handoff_requested")
    assert ev.properties == {"reason": "agent_request", "source": "agent"}


async def test_request_human_tool_invalid_args_graceful():
    res = await run_tool(_ctx(), _deps(), "request_human", {"reason": ""})  # min_length=1
    assert res.ok is False


# --- notify_operator (P12: fără PII) -----------------------------------------


class _FakeHTTP:
    def __init__(self, sink):
        self.sink = sink

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None):
        self.sink.append((url, json))


async def test_notify_operator_posts_without_pii(monkeypatch):
    sink = []
    monkeypatch.setattr(
        ht, "get_settings", lambda: SimpleNamespace(operator_alert_webhook="https://op/hook")
    )
    monkeypatch.setattr(ht.httpx, "AsyncClient", lambda **k: _FakeHTTP(sink))
    await ht.notify_operator(_ctx(), "user_request")
    assert sink == [
        ("https://op/hook", {"business": "d", "conversation_id": "conv", "reason": "user_request"})
    ]  # slug + conv_id + motiv; ZERO telefon/nume/corp mesaj


async def test_notify_operator_noop_without_webhook(monkeypatch):
    called = False

    def _boom(**k):
        nonlocal called
        called = True
        raise AssertionError("nu trebuie să construim client fără webhook")

    monkeypatch.setattr(ht, "get_settings", lambda: SimpleNamespace(operator_alert_webhook=""))
    monkeypatch.setattr(ht.httpx, "AsyncClient", _boom)
    await ht.notify_operator(_ctx(), "user_request")  # no-op
    assert called is False
