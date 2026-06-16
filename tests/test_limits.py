"""G2c — modulul limits (cost guard + rate limit), cu fake redis. ZERO Redis real."""

from src.models import Event
from src.worker.limits import cost_add, cost_over_budget, estimate_turn_cost, rate_limit_count


class _FakeRedis:
    def __init__(self, get_value=None):
        self.store = {}
        self.expires = {}
        self._get_value = get_value

    async def incr(self, key):
        self.store[key] = int(self.store.get(key, 0)) + 1
        return self.store[key]

    async def expire(self, key, ttl):
        self.expires[key] = ttl

    async def get(self, key):
        return self._get_value

    async def incrbyfloat(self, key, amount):
        self.store[key] = float(self.store.get(key, 0.0)) + amount
        return self.store[key]


# --- rate limit --------------------------------------------------------------


async def test_rate_limit_increments_and_expires_on_first():
    r = _FakeRedis()
    assert await rate_limit_count(r, "b", "c", 60) == 1
    assert r.expires["rate:b:c"] == 60  # EXPIRE doar la primul
    assert await rate_limit_count(r, "b", "c", 60) == 2
    assert len(r.expires) == 1  # nu re-setează fereastra


# --- cost guard --------------------------------------------------------------


async def test_cost_over_budget():
    assert await cost_over_budget(_FakeRedis(get_value="6.0"), "b", 5.0) is True
    assert await cost_over_budget(_FakeRedis(get_value="5.0"), "b", 5.0) is True
    assert await cost_over_budget(_FakeRedis(get_value="3.0"), "b", 5.0) is False
    assert await cost_over_budget(_FakeRedis(get_value=None), "b", 5.0) is False


async def test_cost_add_accumulates_and_skips_zero():
    r = _FakeRedis()
    await cost_add(r, "b", 0.01)
    await cost_add(r, "b", 0.02)
    assert abs(sum(r.store.values()) - 0.03) < 1e-9
    assert r.expires  # EXPIRE setat
    await cost_add(r, "b", 0.0)  # no-op
    assert abs(sum(r.store.values()) - 0.03) < 1e-9


# --- estimate_turn_cost ------------------------------------------------------


def test_estimate_rag_turn():
    events = [Event("intent_detected"), Event("agent_recommended")]
    cost = estimate_turn_cost(events, cost_triage_usd=0.0003, cost_agent_usd=0.003)
    assert abs(cost - (0.0003 + 0.003)) < 1e-9


def test_estimate_tool_calling_turn():
    events = [
        Event("intent_detected"),
        Event("tool_call"),
        Event("tool_call"),
        Event("agent_recommended"),
    ]
    cost = estimate_turn_cost(events, cost_triage_usd=0.0003, cost_agent_usd=0.003)
    assert abs(cost - (0.0003 + 0.003 * 3)) < 1e-9  # mini ×(1+2 tool_call)


def test_estimate_no_llm_is_zero():
    assert estimate_turn_cost([], cost_triage_usd=0.0003, cost_agent_usd=0.003) == 0.0
    # doar cache hit (fără LLM) → 0
    assert (
        estimate_turn_cost([Event("cache_lookup")], cost_triage_usd=0.0003, cost_agent_usd=0.003)
        == 0.0
    )
