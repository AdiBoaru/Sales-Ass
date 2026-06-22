"""G2c + NX-125 — cost guard în processor + primitivele din limits, fără Redis/DB real.

`_llm_within_budget`/`_record_turn_cost` cu fake redis dict-backed; primitivele
(`cost_add_and_total`, `spend_over_cap`, `spend_capped`, `seed_daily_cost`) testate direct.
"""

from types import SimpleNamespace

from src.models import BusinessConfig, Contact, InboundMessage, TurnContext, TurnUsage
from src.worker import processor as proc
from src.worker.limits import (
    CONTACT_COST_WINDOW_S,
    _today,
    contact_scope_key,
    cost_add_and_total,
    seed_daily_cost,
    spend_capped,
    spend_over_cap,
)
from src.worker.processor import _llm_within_budget, _record_turn_cost


class _FakeRedis:
    """Fake Redis dict-backed: incrbyfloat cumulativ + get/set/expire/ttl (cost guard)."""

    def __init__(self):
        self.store: dict = {}
        self.ttls: dict = {}

    async def incrbyfloat(self, key, amount):
        self.store[key] = float(self.store.get(key, 0.0)) + float(amount)
        return self.store[key]

    async def get(self, key):
        v = self.store.get(key)
        return None if v is None else str(v)

    async def set(self, key, value, *, ex=None):
        self.store[key] = value
        if ex is not None:
            self.ttls[key] = ex

    async def expire(self, key, ttl):
        self.ttls[key] = ttl

    async def ttl(self, key):
        if key not in self.store:
            return -2
        return self.ttls.get(key, -1)


class _BoomRedis:
    """Fake care ridică din orice op Redis → testează fail-open / best-effort (P6)."""

    async def incrbyfloat(self, *a, **k):
        raise RuntimeError("redis down")

    async def get(self, *a, **k):
        raise RuntimeError("redis down")

    async def set(self, *a, **k):
        raise RuntimeError("redis down")

    async def expire(self, *a, **k):
        raise RuntimeError("redis down")

    async def ttl(self, *a, **k):
        raise RuntimeError("redis down")


def _settings(**over):
    base = dict(
        cost_guard_enabled=True,
        daily_cost_cap_usd=5.0,
        contact_daily_cost_cap_usd=0.0,
        cost_triage_usd=0.0003,
        cost_agent_usd=0.003,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _ctx(*, cost_usd=None, biz_cap=5.0) -> TurnContext:
    ctx = TurnContext(
        turn_id="t",
        business=BusinessConfig(id="b", slug="s", name="n", daily_cost_cap_usd=biz_cap),
        contact=Contact(id="c", business_id="b"),
        message=InboundMessage(provider_msg_id="m", body="x"),
        conversation_id="conv",
    )
    if cost_usd is not None:
        ctx.usage = TurnUsage(cost_usd=cost_usd, calls=1)
    return ctx


# --- _llm_within_budget (pre-check business + per-contact) -------------------


async def test_over_budget_disables_llm(monkeypatch):
    monkeypatch.setattr(proc, "get_llm", lambda: object())

    async def over(*a, **k):
        return True

    monkeypatch.setattr(proc, "cost_over_budget", over)
    ctx = _ctx()
    llm = await _llm_within_budget(ctx, _FakeRedis(), ctx.business, channel_kind="whatsapp")
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
    llm = await _llm_within_budget(ctx, _FakeRedis(), ctx.business, channel_kind="whatsapp")
    assert llm is sentinel
    assert not any(e.type == "cost_guard_tripped" for e in ctx.events)


async def test_no_redis_guard_off(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(proc, "get_llm", lambda: sentinel)

    async def boom(*a, **k):
        raise AssertionError("fără redis → guard off (nu verifică bugetul)")

    monkeypatch.setattr(proc, "cost_over_budget", boom)
    ctx = _ctx()
    assert await _llm_within_budget(ctx, None, ctx.business, channel_kind="whatsapp") is sentinel


async def test_no_llm_returns_none(monkeypatch):
    monkeypatch.setattr(proc, "get_llm", lambda: None)
    ctx = _ctx()
    assert (
        await _llm_within_budget(ctx, _FakeRedis(), ctx.business, channel_kind="whatsapp") is None
    )


async def test_contact_cap_pre_check_disables_llm(monkeypatch):
    monkeypatch.setattr(proc, "get_llm", lambda: object())
    monkeypatch.setattr(proc, "get_settings", lambda: _settings(contact_daily_cost_cap_usd=0.05))

    async def under(*a, **k):
        return False  # business sub plafon

    monkeypatch.setattr(proc, "cost_over_budget", under)
    r = _FakeRedis()
    # contactul a depășit deja plafonul per-contact în fereastră
    r.store[f"spend:{contact_scope_key('b', 'c')}"] = 0.06
    ctx = _ctx()
    llm = await _llm_within_budget(ctx, r, ctx.business, channel_kind="whatsapp")
    assert llm is None
    assert any(e.type == "contact_spend_capped" for e in ctx.events)


async def test_contact_cap_with_business_guard_off(monkeypatch):
    # business cap OFF (0) + contact cap ON → contactul peste plafon blochează LLM-ul fără să
    # depindă de cost_over_budget (nemonkeypatch-uit aici).
    monkeypatch.setattr(proc, "get_llm", lambda: object())
    monkeypatch.setattr(
        proc,
        "get_settings",
        lambda: _settings(daily_cost_cap_usd=0.0, contact_daily_cost_cap_usd=0.05),
    )
    r = _FakeRedis()
    r.store[f"spend:{contact_scope_key('b', 'c')}"] = 0.06
    ctx = _ctx(biz_cap=0.0)
    llm = await _llm_within_budget(ctx, r, ctx.business, channel_kind="whatsapp")
    assert llm is None
    assert any(e.type == "contact_spend_capped" for e in ctx.events)


async def test_llm_within_budget_failopen_on_check_error(monkeypatch):
    # Redis PREZENT dar check-ul aruncă → fail-open (LLM normal), nu blochează traficul (P6).
    sentinel = object()
    monkeypatch.setattr(proc, "get_llm", lambda: sentinel)

    async def boom(*a, **k):
        raise RuntimeError("redis down")

    monkeypatch.setattr(proc, "cost_over_budget", boom)
    ctx = _ctx()
    got = await _llm_within_budget(ctx, _FakeRedis(), ctx.business, channel_kind="whatsapp")
    assert got is sentinel


async def test_record_cost_failopen_on_redis_error(monkeypatch):
    # incrbyfloat aruncă în add-ul de business ȘI în spend-ul per-contact → ambele înghit
    # eroarea (turul a răspuns deja); niciun event de cap, nicio excepție propagată (P6).
    monkeypatch.setattr(proc, "get_settings", lambda: _settings(contact_daily_cost_cap_usd=0.05))
    ctx = _ctx(cost_usd=0.06)
    await _record_turn_cost(_BoomRedis(), ctx.business, ctx, llm_used=True, channel_kind="whatsapp")
    assert not any(e.type in ("cost_guard_tripped", "contact_spend_capped") for e in ctx.events)


async def test_contact_cap_skipped_on_web(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(proc, "get_llm", lambda: sentinel)
    monkeypatch.setattr(proc, "get_settings", lambda: _settings(contact_daily_cost_cap_usd=0.05))

    async def under(*a, **k):
        return False

    monkeypatch.setattr(proc, "cost_over_budget", under)
    r = _FakeRedis()
    r.store[f"spend:{contact_scope_key('b', 'c')}"] = (
        99.0  # peste, dar pe web NU se aplică (NX-120)
    )
    ctx = _ctx()
    llm = await _llm_within_budget(ctx, r, ctx.business, channel_kind="webchat")
    assert llm is sentinel
    assert not any(e.type == "contact_spend_capped" for e in ctx.events)


# --- _record_turn_cost (cost EXACT + atomic + per-contact) -------------------


async def test_record_cost_uses_exact_usage(monkeypatch):
    monkeypatch.setattr(proc, "get_settings", lambda: _settings())
    r = _FakeRedis()
    ctx = _ctx(cost_usd=0.0042)
    await _record_turn_cost(r, ctx.business, ctx, llm_used=True, channel_kind="whatsapp")
    # contorul zilnic = cifra EXACTĂ din tokeni, nu euristica
    assert float(r.store[f"cost:b:{_today()}"]) == 0.0042
    assert not any(e.type == "cost_guard_tripped" for e in ctx.events)


async def test_record_cost_skips_when_llm_unused(monkeypatch):
    monkeypatch.setattr(proc, "get_settings", lambda: _settings())
    r = _FakeRedis()
    ctx = _ctx(cost_usd=0.01)
    await _record_turn_cost(r, ctx.business, ctx, llm_used=False, channel_kind="whatsapp")
    assert r.store == {}  # peste buget / fără LLM → nu acumulează


async def test_record_cost_zero_usage_no_write(monkeypatch):
    monkeypatch.setattr(proc, "get_settings", lambda: _settings())
    r = _FakeRedis()
    ctx = _ctx(cost_usd=None)  # tur fără apeluri LLM (cache L1/gates)
    await _record_turn_cost(r, ctx.business, ctx, llm_used=True, channel_kind="whatsapp")
    assert r.store == {}


async def test_record_cost_post_increment_trips_business(monkeypatch):
    monkeypatch.setattr(proc, "get_settings", lambda: _settings(daily_cost_cap_usd=0.05))
    r = _FakeRedis()
    ctx = _ctx(cost_usd=0.06, biz_cap=0.05)
    await _record_turn_cost(r, ctx.business, ctx, llm_used=True, channel_kind="whatsapp")
    # noul total ≥ plafon → emite cost_guard_tripped (turul URMĂTOR blocat determinist)
    ev = next(e for e in ctx.events if e.type == "cost_guard_tripped")
    assert ev.properties["cap_usd"] == 0.05 and ev.properties["total_usd"] == 0.06


async def test_record_cost_per_contact_capped(monkeypatch):
    monkeypatch.setattr(proc, "get_settings", lambda: _settings(contact_daily_cost_cap_usd=0.05))
    r = _FakeRedis()
    ctx = _ctx(cost_usd=0.06)
    await _record_turn_cost(r, ctx.business, ctx, llm_used=True, channel_kind="whatsapp")
    assert any(e.type == "contact_spend_capped" for e in ctx.events)
    assert float(r.store[f"spend:{contact_scope_key('b', 'c')}"]) == 0.06


async def test_record_cost_per_contact_not_applied_on_web(monkeypatch):
    monkeypatch.setattr(proc, "get_settings", lambda: _settings(contact_daily_cost_cap_usd=0.001))
    r = _FakeRedis()
    ctx = _ctx(cost_usd=0.06)
    await _record_turn_cost(r, ctx.business, ctx, llm_used=True, channel_kind="webchat")
    # web = NX-120 (calea sincronă) → niciun spend per-contact aici
    assert not any(e.type == "contact_spend_capped" for e in ctx.events)
    assert f"spend:{contact_scope_key('b', 'c')}" not in r.store


# --- primitive limits.py ----------------------------------------------------


async def test_cost_add_and_total_atomic():
    r = _FakeRedis()
    assert await cost_add_and_total(r, "b", 0.01) == 0.01
    assert await cost_add_and_total(r, "b", 0.02) == 0.03  # total cumulativ corect


async def test_cost_add_and_total_zero_returns_current():
    r = _FakeRedis()
    await cost_add_and_total(r, "b", 0.01)
    assert await cost_add_and_total(r, "b", 0.0) == 0.01  # ≤0 → nu scrie, întoarce curentul


async def test_spend_over_cap_under_then_over():
    r = _FakeRedis()
    scope = contact_scope_key("b", "c")
    assert await spend_over_cap(r, scope, 0.5, 1.0, CONTACT_COST_WINDOW_S) is False
    # EXPIRE setat la primul increment (fereastră fixă)
    assert r.ttls[f"spend:{scope}"] == CONTACT_COST_WINDOW_S
    assert await spend_over_cap(r, scope, 0.6, 1.0, CONTACT_COST_WINDOW_S) is True  # 1.1 ≥ 1.0


async def test_spend_over_cap_disabled_no_write():
    r = _FakeRedis()
    scope = contact_scope_key("b", "c")
    assert await spend_over_cap(r, scope, 0.5, 0.0, CONTACT_COST_WINDOW_S) is False
    assert f"spend:{scope}" not in r.store  # cap dezactivat → nimic scris


async def test_spend_capped_readonly():
    r = _FakeRedis()
    scope = contact_scope_key("b", "c")
    assert await spend_capped(r, scope, 1.0) is False
    r.store[f"spend:{scope}"] = 1.2
    assert await spend_capped(r, scope, 1.0) is True
    assert await spend_capped(r, scope, 0.0) is False  # cap 0 → dezactivat


# --- seed_daily_cost (reseed la pierderea Redis) ----------------------------


class _FakeConn:
    def __init__(self, cost=None):
        self.cost = cost
        self.calls = 0

    async def fetchval(self, q, *a):
        self.calls += 1
        return self.cost


async def test_seed_daily_cost_seeds_then_noop():
    r = _FakeRedis()
    conn = _FakeConn(cost=1.5)
    await seed_daily_cost(conn, r, "b")
    assert float(r.store[f"cost:b:{_today()}"]) == 1.5  # seedat din usage_daily
    assert r.store[f"cost_seeded:b:{_today()}"] == "1"  # santinelă setată
    await seed_daily_cost(conn, r, "b")  # al doilea apel = no-op (santinelă)
    assert conn.calls == 1


async def test_seed_skips_when_counter_present():
    r = _FakeRedis()
    r.store[f"cost:b:{_today()}"] = 2.0  # contor viu → NU-l clobberăm
    conn = _FakeConn(cost=1.5)
    await seed_daily_cost(conn, r, "b")
    assert float(r.store[f"cost:b:{_today()}"]) == 2.0
    assert conn.calls == 0  # nu citește usage_daily dacă contorul există


async def test_seed_best_effort_on_db_error():
    r = _FakeRedis()

    class _BoomConn:
        async def fetchval(self, q, *a):
            raise RuntimeError("db down")

    await seed_daily_cost(_BoomConn(), r, "b")  # nu ridică (best-effort)
    assert f"cost:b:{_today()}" not in r.store  # nimic seedat, dar nu crapă
