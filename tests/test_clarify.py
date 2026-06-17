"""NX-130 — clarificare din stare (pending_question, slot-filling determinist).

Stage-level: set_clarify (persistă slotul), clarify_resume_stage (reluare fără LLM),
garda din triaj (un singur owner pe `route`). Processor: pending_question scris/curățat în
new_state. ZERO OpenAI/DB real (stub conn + funcții monkeypatch-uite, pattern G8-1)."""

from src.models import (
    BusinessConfig,
    Contact,
    InboundMessage,
    Route,
    RouteDecision,
    TurnContext,
)
from src.worker import processor as proc
from src.worker.processor import handle_turn
from src.worker.runner import PipelineDeps
from src.worker.stages.clarify import clarify_resume_stage
from src.worker.stages.triage import triage_stage


def _ctx(body: str = "x", *, pending=None, route=None) -> TurnContext:
    ctx = TurnContext(
        turn_id="t",
        business=BusinessConfig(id="biz-1", slug="s", name="n"),
        contact=Contact(id="c", business_id="biz-1"),
        message=InboundMessage(provider_msg_id="m", body=body),
        conversation_id="conv",
        route=route,
    )
    ctx.state.pending_question = pending
    return ctx


def _deps(llm=None) -> PipelineDeps:
    return PipelineDeps(conn=object(), redis=None, llm=llm)


def _event(ctx, type_):
    return next(e for e in reversed(ctx.events) if e.type == type_)


# --- set_clarify: persistă slotul pe reply + emite clarify_asked --------------


def test_set_clarify_writes_pending_question():
    ctx = _ctx()
    ctx.set_clarify("Ce buget ai?", field="budget_max", resume_route="sales")
    pq = ctx.reply.pending_question
    assert pq["field"] == "budget_max" and pq["resume_route"] == "sales"
    assert pq["attempts"] == 1 and "asked_at" in pq
    assert ctx.reply.cacheable is False  # clarify = specific contextului, nu cache
    ev = _event(ctx, "clarify_asked")
    assert ev.properties == {"field": "budget_max", "attempts": 1}


def test_set_clarify_attempts_increment_same_slot():
    # Re-clarify pe ACELAȘI slot (state-ul are deja un pending_question pe budget_max).
    ctx = _ctx(pending={"field": "budget_max", "resume_route": "sales", "attempts": 1})
    ctx.set_clarify("Tot nu mi-e clar bugetul?", field="budget_max", resume_route="sales")
    assert ctx.reply.pending_question["attempts"] == 2


def test_set_clarify_attempts_reset_on_different_slot():
    ctx = _ctx(pending={"field": "budget_max", "attempts": 2})
    ctx.set_clarify("Ce tip de ten ai?", field="skin_type", resume_route="sales")
    assert ctx.reply.pending_question["attempts"] == 1


# --- clarify_resume_stage: reluare deterministă fără LLM ----------------------


async def test_resume_fills_slot_and_routes():
    ctx = _ctx("200 lei", pending={"field": "budget_max", "resume_route": "sales"})
    await clarify_resume_stage(ctx, _deps())
    assert ctx.route is not None and ctx.route.route == Route.SALES
    assert ctx.state.constraints["budget_max"] == "200 lei"
    ev = _event(ctx, "clarify_resumed")
    assert ev.properties == {"field": "budget_max"}  # P12 — fără `answer`
    assert "answer" not in ev.properties and "200 lei" not in str(ev.properties)


async def test_resume_noop_without_pending_question():
    ctx = _ctx("orice")
    await clarify_resume_stage(ctx, _deps())
    assert ctx.route is None  # nimic în așteptare → triajul rutează normal mai târziu
    assert ctx.state.constraints == {}


async def test_resume_noop_on_empty_body_keeps_slot():
    pq = {"field": "budget_max", "resume_route": "sales"}
    ctx = _ctx("", pending=pq)
    await clarify_resume_stage(ctx, _deps())
    assert ctx.route is None  # body gol (ex. media) → nu consumăm slotul pe gol
    assert ctx.state.pending_question == pq  # rămâne pentru data viitoare


async def test_resume_invalid_route_defaults_sales():
    ctx = _ctx("da", pending={"field": "x", "resume_route": "bogus"})
    await clarify_resume_stage(ctx, _deps())
    assert ctx.route.route == Route.SALES


async def test_resume_missing_route_defaults_sales_and_intent_field():
    ctx = _ctx("da", pending={})  # fără field, fără resume_route
    await clarify_resume_stage(ctx, _deps())
    assert ctx.route.route == Route.SALES
    assert ctx.state.constraints["intent"] == "da"  # field absent → slot generic „intent"


async def test_resume_corrupt_pending_is_noop():
    ctx = _ctx("da", pending=["nu", "e", "dict"])  # state corupt
    await clarify_resume_stage(ctx, _deps())
    assert ctx.route is None  # .get defensiv → no-op, nu crapă turul


# --- garda din triaj: un singur owner pe `route` (P3) ------------------------


async def test_triage_noop_when_route_already_set():
    class _SpyLLM:
        async def classify_json(self, *a, **k):
            raise AssertionError("triajul NU trebuie să cheme nano când ruta e deja setată")

    ctx = _ctx("200 lei", route=RouteDecision(route=Route.SALES))
    await triage_stage(ctx, _deps(_SpyLLM()))  # garda întoarce înainte de orice apel LLM
    assert ctx.route.route == Route.SALES  # neschimbat


# --- processor: pending_question scris / curățat în new_state ----------------


class _FakeTx:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *a):
        return False


class _FakeConn:
    def transaction(self):
        return _FakeTx()


async def _run_handle_turn(monkeypatch, stage):
    """Rulează handle_turn cu DB stubbed; întoarce new_state-ul dat lui patch_conversation_state."""
    captured: dict = {}

    async def fake_conv(*a, **k):
        return {
            "id": "conv",
            "state": {"constraints": {"x": 1}},  # state pre-existent (verificăm păstrarea)
            "state_version": 0,
            "locale": "ro",
            "bot_active": True,
            "handoff_until": None,
        }

    async def fake_patch(conn, business_id, conv_id, new_state, version, **k):
        captured["new_state"] = new_state

    async def anoop(*a, **k):
        return None

    async def fake_contact(*a, **k):
        return Contact(id="c", business_id="biz-1")

    async def fake_claim(*a, **k):
        return True

    async def fake_insert_msg(*a, **k):
        return "msg-id"

    async def fake_outbox(*a, **k):
        return "outbox-1"

    async def fake_budget(*a, **k):
        return None  # llm=None; folosim `stages` custom oricum

    monkeypatch.setattr(proc, "claim_inbound", fake_claim)
    monkeypatch.setattr(proc, "get_or_create_contact", fake_contact)
    monkeypatch.setattr(proc, "get_or_create_conversation", fake_conv)
    monkeypatch.setattr(proc, "insert_message", fake_insert_msg)
    monkeypatch.setattr(proc, "touch_last_inbound", anoop)
    monkeypatch.setattr(proc, "get_recent_messages", anoop)
    monkeypatch.setattr(proc, "get_summary_for_context", anoop)
    monkeypatch.setattr(proc, "enqueue_outbox", fake_outbox)
    monkeypatch.setattr(proc, "patch_conversation_state", fake_patch)
    monkeypatch.setattr(proc, "_persist_events", anoop)
    monkeypatch.setattr(proc, "_record_turn_cost", anoop)
    monkeypatch.setattr(proc, "_llm_within_budget", fake_budget)
    monkeypatch.setattr(proc, "_cache_writeback", anoop)
    monkeypatch.setattr(proc, "_summarize_if_needed", anoop)

    business = BusinessConfig(id="biz-1", slug="s", name="n")
    event = {
        "channel_kind": "telegram",
        "sender_external_id": "u1",
        "provider_msg_id": "m1",
        "content_type": "text",
        "body": "salut",
    }
    await handle_turn(_FakeConn(), business, "chan-1", event, stages=[stage])
    return captured["new_state"]


async def test_processor_persists_pending_question(monkeypatch):
    async def clarify_stage(ctx, deps):
        ctx.set_clarify("Ce buget ai?", field="budget_max", resume_route="sales")

    new_state = await _run_handle_turn(monkeypatch, clarify_stage)
    assert new_state["pending_question"]["field"] == "budget_max"
    assert new_state["constraints"] == {"x": 1}  # cheile pre-existente se păstrează


async def test_processor_clears_pending_question_on_normal_reply(monkeypatch):
    async def reply_stage(ctx, deps):
        ctx.set_reply("Iată ce am găsit")

    new_state = await _run_handle_turn(monkeypatch, reply_stage)
    # Reply non-clarify → pending_question explicit None (slot zombi curățat), nu absent.
    assert "pending_question" in new_state and new_state["pending_question"] is None
