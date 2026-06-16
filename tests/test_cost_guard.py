"""G2c — cost guard în processor (`_llm_within_budget` / `_record_turn_cost`), fără Redis real.

Monkeypatch pe `proc.get_llm` și `proc.cost_over_budget`; fake redis pentru `cost_add`."""

from src.models import BusinessConfig, Contact, Event, InboundMessage, TurnContext
from src.worker import processor as proc
from src.worker.processor import _llm_within_budget, _record_turn_cost


class _FakeRedis:
    def __init__(self):
        self.added = []

    async def incrbyfloat(self, key, amount):
        self.added.append(amount)
        return amount

    async def expire(self, key, ttl):
        pass


def _ctx() -> TurnContext:
    return TurnContext(
        turn_id="t",
        business=BusinessConfig(id="b", slug="s", name="n", daily_cost_cap_usd=5.0),
        contact=Contact(id="c", business_id="b"),
        message=InboundMessage(provider_msg_id="m", body="x"),
        conversation_id="conv",
    )


async def test_over_budget_disables_llm(monkeypatch):
    monkeypatch.setattr(proc, "get_llm", lambda: object())

    async def over(*a, **k):
        return True

    monkeypatch.setattr(proc, "cost_over_budget", over)
    ctx = _ctx()
    llm = await _llm_within_budget(ctx, _FakeRedis(), ctx.business)
    assert llm is None
    assert any(
        e.type == "cost_guard_tripped" and e.properties["cap_usd"] == 5.0 for e in ctx.events
    )


async def test_under_budget_keeps_llm(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(proc, "get_llm", lambda: sentinel)

    async def under(*a, **k):
        return False

    monkeypatch.setattr(proc, "cost_over_budget", under)
    ctx = _ctx()
    llm = await _llm_within_budget(ctx, _FakeRedis(), ctx.business)
    assert llm is sentinel
    assert not any(e.type == "cost_guard_tripped" for e in ctx.events)


async def test_no_redis_guard_off(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(proc, "get_llm", lambda: sentinel)

    async def boom(*a, **k):
        raise AssertionError("fără redis → guard off (nu verifică bugetul)")

    monkeypatch.setattr(proc, "cost_over_budget", boom)
    ctx = _ctx()
    assert await _llm_within_budget(ctx, None, ctx.business) is sentinel


async def test_no_llm_returns_none(monkeypatch):
    monkeypatch.setattr(proc, "get_llm", lambda: None)
    ctx = _ctx()
    assert await _llm_within_budget(ctx, _FakeRedis(), ctx.business) is None


async def test_record_cost_adds_when_llm_used():
    r = _FakeRedis()
    ctx = _ctx()
    ctx.events.extend([Event("intent_detected"), Event("agent_recommended")])
    await _record_turn_cost(r, "b", ctx, llm_used=True)
    assert r.added and r.added[0] > 0


async def test_record_cost_skips_when_llm_unused():
    r = _FakeRedis()
    ctx = _ctx()
    ctx.events.append(Event("intent_detected"))
    await _record_turn_cost(r, "b", ctx, llm_used=False)
    assert r.added == []  # peste buget → nu acumulează
