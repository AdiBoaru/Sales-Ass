"""NX-241 — clasificarea tool-urilor, admission-ul lor și poarta read/mutation.

Miza: „rulăm tool-urile concurent ca să tăiem latența" (ce face bucla de azi) e o promisiune că
nicio MUTAȚIE nu se lansează speculativ și că două mutații nu se suprapun. Testele astea o verifică
mecanic — inclusiv completitudinea registrului, ca un tool nou să nu moștenească tăcut „read".
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from src.agent import tool_executor as te
from src.agent.tool_budget import (
    REFUSAL_BUDGET,
    REFUSAL_DEADLINE,
    ToolGate,
    ToolKind,
    admit,
    assert_registry_complete,
    cap_result,
    spec_for,
)
from src.agent.tool_definitions import TOOL_NAMES
from src.agent.tool_executor import ToolRun
from src.runtime import deadline, turn_budget
from src.runtime.deadline import TurnDeadline
from src.runtime.turn_budget import BudgetLedger, TurnClass, build_manifest


def _budget(turn_class=TurnClass.RECOMMENDATION):
    return build_manifest(hard_cap_ms=15_000, cost_ceiling_usd=0.01)[turn_class]


def _ledger(turn_class=TurnClass.RECOMMENDATION, *, enforced=True):
    return BudgetLedger(_budget(turn_class), enforced=enforced)


# ── registrul de clasificare ───────────────────────────────────────────────────────────────
def test_every_model_callable_tool_is_classified():
    assert_registry_complete()  # nu ridică → registrul acoperă exact `TOOL_NAMES`
    assert set(TOOL_NAMES) == {name for name in TOOL_NAMES if spec_for(name).name == name}


def test_commerce_tools_are_mutations_and_not_parallel_safe():
    for name in ("cart_add", "checkout_link", "subscribe_back_in_stock", "request_human"):
        spec = spec_for(name)
        assert spec.kind is ToolKind.MUTATION
        assert spec.parallel_safe is False
        assert spec.idempotent is True  # receipts NX-237 / UNIQUE / handoff_until absolut


def test_catalog_reads_are_parallel_safe():
    for name in ("search_products", "get_product_details", "compare_products", "faq_lookup"):
        spec = spec_for(name)
        assert spec.kind is ToolKind.READ and spec.parallel_safe


def test_unknown_tool_is_treated_conservatively():
    """Un nume necunoscut nu poate veni de la model (schemele sunt închise), dar dacă apare, e
    tratat ca mutație neparalelizabilă — conservator, nu permisiv."""
    spec = spec_for("tool_inventat")
    assert spec.is_mutation and not spec.parallel_safe


# ── admission ──────────────────────────────────────────────────────────────────────────────
def test_admit_without_ledger_or_deadline_always_allows():
    assert admit("search_products")


def test_admit_refuses_past_the_tool_cap_with_a_typed_message():
    ledger = _ledger()
    for _ in range(int(ledger.budget.max_tool_calls)):
        assert admit("search_products", ledger=ledger)
    decision = admit("search_products", ledger=ledger)
    assert not decision and decision.reason == "tool_calls"
    assert decision.refusal == REFUSAL_BUDGET


def test_admit_refuses_a_mutation_on_a_read_only_class_without_burning_a_tool_call():
    ledger = _ledger(TurnClass.EXACT)
    decision = admit("cart_add", ledger=ledger)
    assert not decision and decision.reason == "mutations"
    assert ledger.spent.get("tool_calls", 0) == 0  # rezervarea s-a ELIBERAT, nu s-a pierdut


def test_admit_refuses_everything_once_the_deadline_is_gone():
    d = TurnDeadline(total_ms=100, terminal_reserve_ms=50, clock=lambda: 0.0)
    d.cancel()
    decision = admit("search_products", ledger=_ledger(), deadline=d)
    assert not decision and decision.refusal == REFUSAL_DEADLINE


def test_deadline_is_checked_before_the_budget():
    """Ordinea contează: o mutație pornită după deadline scrie ceva despre care clientul nu mai
    află. Timpul e verificat primul, iar bugetul nici nu se atinge."""
    clock = SimpleNamespace(t=0.0)
    d = TurnDeadline(total_ms=100, terminal_reserve_ms=50, clock=lambda: clock.t)
    clock.t = 10.0  # au trecut 10s peste un buget de 100ms
    ledger = _ledger()
    assert not admit("cart_add", ledger=ledger, deadline=d)
    assert ledger.spent == {}


# ── plafon de rezultat ─────────────────────────────────────────────────────────────────────
def test_cap_result_truncates_with_explicit_coverage():
    view, dropped = cap_result("x" * 5_000, 1_000)
    assert dropped == 4_000
    assert "trunchiat" in view and "5000" in view


def test_cap_result_leaves_small_results_untouched():
    assert cap_result("scurt", 1_000) == ("scurt", 0)


def test_cap_result_counts_bytes_not_characters():
    """Diacriticele românești sunt 2 octeți: un plafon „pe caractere" ar minți exact pe RO."""
    text = "ș" * 100  # 200 de octeți
    view, dropped = cap_result(text, 150)
    assert dropped == 50
    assert len(view.encode("utf-8")) < 250


def test_cap_result_disabled_by_zero():
    assert cap_result("x" * 100, 0) == ("x" * 100, 0)


# ── poarta read/mutation ───────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_reads_run_in_parallel_up_to_the_cap():
    gate = ToolGate(max_parallel_reads=3)
    active, peak = 0, 0

    async def read():
        nonlocal active, peak
        async with gate.hold("search_products"):
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1

    await asyncio.gather(*(read() for _ in range(6)))
    assert peak == 3 and gate.peak_readers == 3


@pytest.mark.asyncio
async def test_gate_with_cap_one_is_todays_serialization():
    gate = ToolGate(max_parallel_reads=1)
    active, peak = 0, 0

    async def read():
        nonlocal active, peak
        async with gate.hold("search_products"):
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0)
            active -= 1

    await asyncio.gather(*(read() for _ in range(4)))
    assert peak == 1


@pytest.mark.asyncio
async def test_mutations_never_overlap_with_each_other_or_with_reads():
    """Invariantul care ține un coș să nu se dubleze: cât timp o mutație rulează, nimic altceva
    nu atinge starea."""
    gate = ToolGate(max_parallel_reads=3)
    timeline: list[str] = []
    in_mutation = False
    overlaps: list[str] = []

    async def read():
        async with gate.hold("search_products"):
            if in_mutation:
                overlaps.append("read_during_mutation")
            timeline.append("r")
            await asyncio.sleep(0.005)

    async def mutate():
        nonlocal in_mutation
        async with gate.hold("cart_add"):
            if in_mutation:
                overlaps.append("mutation_during_mutation")
            in_mutation = True
            timeline.append("m")
            await asyncio.sleep(0.005)
            in_mutation = False

    await asyncio.gather(read(), mutate(), read(), mutate(), read())
    assert overlaps == []
    assert timeline.count("m") == 2


@pytest.mark.asyncio
async def test_gate_releases_on_exception():
    gate = ToolGate(max_parallel_reads=1)
    with pytest.raises(RuntimeError):
        async with gate.hold("cart_add"):
            raise RuntimeError("tool crăpat")
    async with gate.hold("search_products"):  # nu s-a blocat nimic
        pass


# ── integrare cu `ToolRun` ─────────────────────────────────────────────────────────────────
def _result(**kw):
    base = dict(
        products=[],
        ok=True,
        links=[],
        prices=set(),
        relevance=None,
        state_patch=None,
        llm_view="view",
        error=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _ctx():
    events: list[tuple] = []
    return SimpleNamespace(
        emit=lambda event_type, **kw: events.append((event_type, kw)),
        state_patch={},
        business=SimpleNamespace(id="biz-1"),
        _events=events,
    )


@pytest.mark.asyncio
async def test_tool_run_returns_a_typed_refusal_when_the_budget_is_gone(monkeypatch):
    calls: list[str] = []

    async def fake_run_tool(ctx, deps, name, args):  # noqa: ARG001
        calls.append(name)
        return _result()

    monkeypatch.setattr(te, "run_tool", fake_run_tool)
    ledger = _ledger(TurnClass.EXACT)  # max_tool_calls = 2
    token = turn_budget.push(ledger)
    try:
        run = ToolRun(_ctx(), SimpleNamespace(db=None))
        assert await run.execute("search_products", {}) == "view"
        assert await run.execute("search_products", {}) == "view"
        refused = await run.execute("search_products", {})
    finally:
        turn_budget.pop(token)
    assert refused == REFUSAL_BUDGET
    assert calls == ["search_products", "search_products"]  # al treilea NU a rulat
    rejected = {"name": "search_products", "outcome": "rejected", "reason": "tool_calls"}
    assert ("tool_budget", rejected) in list(run.ctx._events)


@pytest.mark.asyncio
async def test_tool_run_truncates_an_oversized_result(monkeypatch):
    async def fake_run_tool(ctx, deps, name, args):  # noqa: ARG001
        return _result(llm_view="x" * 100_000)

    monkeypatch.setattr(te, "run_tool", fake_run_tool)
    ledger = _ledger()
    token = turn_budget.push(ledger)
    try:
        run = ToolRun(_ctx(), SimpleNamespace(db=None))
        view = await run.execute("search_products", {})
    finally:
        turn_budget.pop(token)
    assert len(view.encode("utf-8")) < 100_000
    assert "trunchiat" in view
    assert ledger.spent["result_bytes"] > 0


@pytest.mark.asyncio
async def test_tool_run_without_budget_or_deadline_is_unchanged(monkeypatch):
    """Flag stins: nici admission, nici poartă, nici trunchiere — exact calea de azi."""
    seen: list[str] = []

    async def fake_run_tool(ctx, deps, name, args):  # noqa: ARG001
        seen.append(name)
        return _result(llm_view="y" * 50_000)

    monkeypatch.setattr(te, "run_tool", fake_run_tool)
    assert turn_budget.current() is None and deadline.current() is None
    run = ToolRun(_ctx(), SimpleNamespace(db=None))
    for _ in range(9):
        view = await run.execute("search_products", {})
    assert len(seen) == 9
    assert view == "y" * 50_000  # netrunchiat


@pytest.mark.asyncio
async def test_parallel_reads_stay_serialized_on_a_shared_connection(monkeypatch):
    """Un `static_db` dă ACEEAȘI conexiune tuturor: acolo paralelismul rămâne 1, oricât ar spune
    configul. Altfel două query-uri simultane ar rupe conexiunea asyncpg partajată."""
    from src.db.provider import static_db

    monkeypatch.setattr(
        te, "get_settings", lambda: SimpleNamespace(turn_parallel_reads_enabled=True)
    )
    ledger = _ledger(TurnClass.COMPLEX)
    token = turn_budget.push(ledger)
    try:
        shared = ToolRun(_ctx(), SimpleNamespace(db=static_db(object())))
        assert shared._max_parallel_reads() == 1

        def per_op(operation=None):  # provider „conn-per-op" (marcat de `tenant_db`)
            raise AssertionError("nu se apelează în test")

        per_op.shared_connection = False
        real = ToolRun(_ctx(), SimpleNamespace(db=per_op))
        assert real._max_parallel_reads() == ledger.budget.max_parallel_reads
    finally:
        turn_budget.pop(token)
