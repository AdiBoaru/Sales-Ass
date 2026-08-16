"""NX-241 — manifestul de bugete: versionat, validat la boot, impus ATOMIC.

Întrebarea la care răspund testele astea nu e „numără corect", ci „poate modelul să obțină mai
mult decât îi dă codul?". Răspunsul trebuie să fie nu, inclusiv când cere zece tool-uri deodată.
"""

from __future__ import annotations

import asyncio

import pytest

from src.config import Settings
from src.runtime import turn_budget
from src.runtime.turn_budget import (
    BUDGET_MANIFEST_VERSION,
    DIMENSIONS,
    BudgetLedger,
    TurnClass,
    build_manifest,
)


def _manifest(**kw):
    kw.setdefault("hard_cap_ms", 15_000)
    kw.setdefault("cost_ceiling_usd", 0.01)
    return build_manifest(**kw)


def _ledger(turn_class=TurnClass.RECOMMENDATION, *, enforced=True) -> BudgetLedger:
    return BudgetLedger(_manifest()[turn_class], enforced=enforced)


# ── manifest ───────────────────────────────────────────────────────────────────────────────
def test_manifest_covers_every_turn_class_and_is_versioned():
    manifest = _manifest()
    assert set(manifest) == set(TurnClass)
    assert all(b.version == BUDGET_MANIFEST_VERSION for b in manifest.values())


def test_every_class_reserves_time_for_the_terminal_commit():
    for budget in _manifest().values():
        assert 0 < budget.terminal_reserve_ms < budget.total_ms


def test_slo_ordering_matches_the_card():
    """Bugetele nu sunt cifre la întâmplare: fapt exact < recomandare < complex (SLO Stage 1)."""
    m = _manifest()
    assert (
        m[TurnClass.EXACT].total_ms
        < m[TurnClass.RECOMMENDATION].total_ms
        < m[TurnClass.COMPLEX].total_ms
    )
    assert m[TurnClass.EXACT].max_mutations == 0  # un fapt exact nu scrie nimic


def test_totals_are_capped_by_the_hard_deadline():
    m = _manifest(totals_ms={TurnClass.COMPLEX: 60_000}, hard_cap_ms=15_000)
    assert m[TurnClass.COMPLEX].total_ms == 15_000
    assert m[TurnClass.COMPLEX].terminal_reserve_ms > 0  # scalarea păstrează rezerva


def test_scaling_a_total_scales_the_phase_caps():
    doubled = _manifest(totals_ms={TurnClass.EXACT: 6_000})[TurnClass.EXACT]
    base = _manifest()[TurnClass.EXACT]
    assert doubled.model_ms == base.model_ms * 2
    assert doubled.terminal_reserve_ms == base.terminal_reserve_ms * 2


def test_invalid_total_fails_fast():
    with pytest.raises(ValueError, match="total_ms"):
        _manifest(totals_ms={TurnClass.EXACT: 0})


def test_every_dimension_has_a_cap():
    budget = _manifest()[TurnClass.COMPLEX]
    for dimension in DIMENSIONS:
        assert budget.cap_for(dimension) > 0 or dimension == "mutations"


def test_unknown_dimension_is_a_bug_not_a_silent_pass():
    with pytest.raises(KeyError):
        _manifest()[TurnClass.EXACT].cap_for("inventat")


# ── poarta de boot (Settings) ──────────────────────────────────────────────────────────────
def test_enforcing_budgets_without_a_deadline_refuses_to_boot():
    with pytest.raises(ValueError, match="TURN_BUDGET_ENFORCED"):
        Settings(TURN_BUDGET_ENFORCED=True, TURN_DEADLINE_ENABLED=False)


def test_parallel_reads_without_a_deadline_refuses_to_boot():
    with pytest.raises(ValueError, match="TURN_PARALLEL_READS_ENABLED"):
        Settings(TURN_PARALLEL_READS_ENABLED=True, TURN_DEADLINE_ENABLED=False)


def test_a_single_call_cannot_be_allowed_to_eat_the_whole_turn():
    with pytest.raises(ValueError, match="LLM_CALL_CAP_MS"):
        Settings(LLM_CALL_CAP_MS=30_000, TURN_HARD_DEADLINE_MS=15_000)


def test_default_settings_are_dark():
    s = Settings()
    assert s.turn_deadline_enabled is False
    assert s.turn_budget_enforced is False
    assert s.turn_parallel_reads_enabled is False


# ── ledger ─────────────────────────────────────────────────────────────────────────────────
def test_reserve_refuses_past_the_cap_when_enforced():
    ledger = _ledger()
    cap = int(ledger.budget.max_tool_calls)
    for _ in range(cap):
        assert ledger.reserve("tool_calls")
    decision = ledger.reserve("tool_calls")
    assert not decision and decision.reason == "tool_calls"
    assert ledger.rejections["tool_calls"] == 1


def test_observe_only_counts_the_rejection_but_allows_the_work():
    """Modul din rollout: contoarele sunt IDENTICE cu cele din enforce, ca un raport „observe" să
    poată prezice ce ar fi respins „enforce" — fără să schimbe nimic pentru client."""
    ledger = _ledger(enforced=False)
    for _ in range(int(ledger.budget.max_tool_calls) + 3):
        assert ledger.reserve("tool_calls")
    assert ledger.rejections["tool_calls"] == 3


@pytest.mark.asyncio
async def test_reserve_is_atomic_under_a_tool_storm():
    """Zece tool calls lansate în ACEEAȘI rundă nu pot trece toate de ultimul slot: rezervarea se
    face fără `await` între verificare și increment, iar asyncio e single-thread."""
    ledger = _ledger(TurnClass.COMPLEX)
    cap = int(ledger.budget.max_tool_calls)

    async def call():
        await asyncio.sleep(0)  # dă drumul buclei — dacă ar fi o cursă, aici s-ar vedea
        return bool(ledger.reserve("tool_calls"))

    results = await asyncio.gather(*(call() for _ in range(cap + 6)))
    assert sum(results) == cap


def test_release_frees_only_work_that_never_ran():
    ledger = _ledger()
    ledger.reserve("parallel_reads")
    ledger.reserve("parallel_reads")
    assert ledger.spent["parallel_reads"] == 2
    ledger.release("parallel_reads")
    assert ledger.spent["parallel_reads"] == 1
    assert ledger.peak_parallel_reads == 2  # vârful rămâne în raport


def test_rebind_keeps_the_counters():
    """Clasa se știe abia după triaj. Un tur nu-și șterge istoria fiindcă s-a reclasificat."""
    ledger = _ledger(TurnClass.EXACT)
    ledger.reserve("tool_calls")
    ledger.consume("tokens", 1_200)
    ledger.rebind(_manifest()[TurnClass.COMPLEX])
    assert ledger.budget.turn_class is TurnClass.COMPLEX
    assert ledger.spent["tool_calls"] == 1
    assert ledger.spent["tokens"] == 1_200


def test_mutations_are_refused_on_a_read_only_class():
    ledger = _ledger(TurnClass.EXACT)
    assert not ledger.reserve("mutations")


def test_repair_is_capped_at_one():
    for budget in _manifest().values():
        assert budget.max_repair_calls <= 1


def test_cost_ceiling_is_consumed_post_factum():
    ledger = _ledger()
    ledger.consume("cost_usd", 0.009)
    assert not ledger.exhausted("cost_usd")
    ledger.consume("cost_usd", 0.005)
    assert ledger.exhausted("cost_usd")


def test_event_props_are_low_cardinality():
    ledger = _ledger()
    ledger.reserve("tool_calls")
    ledger.consume("tokens", 900)
    props = ledger.as_event_props()
    assert props["budget_version"] == BUDGET_MANIFEST_VERSION
    assert props["turn_class"] == "recommendation"
    assert props["tool_calls"] == 1 and props["tokens"] == 900
    assert all(isinstance(v, (int, float, str, bool, dict)) for v in props.values())


# ── clasificare ────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("route", "kwargs", "expected"),
    [
        ("simple", {}, TurnClass.EXACT),
        ("order", {}, TurnClass.EXACT),
        ("clarify", {}, TurnClass.EXACT),
        ("sales", {}, TurnClass.RECOMMENDATION),
        (None, {}, TurnClass.RECOMMENDATION),
        ("sales", {"compare": True}, TurnClass.COMPLEX),
        ("simple", {"has_action": True}, TurnClass.MUTATION),
        ("sales", {"purchase_intent": True}, TurnClass.MUTATION),
        # o mutație rămâne mutație chiar dacă mesajul arată ca o comparație
        ("sales", {"has_action": True, "compare": True}, TurnClass.MUTATION),
    ],
)
def test_classify(route, kwargs, expected):
    assert turn_budget.classify(route, **kwargs) is expected


def test_count_bucket_is_bounded():
    buckets = {turn_budget.count_bucket(n) for n in range(0, 100)}
    assert buckets == {"0", "1", "2", "3-4", "5-8", "9+"}


# ── scurtături fără ledger (flag stins) ───────────────────────────────────────────────────
def test_module_level_reserve_without_a_ledger_always_allows():
    assert turn_budget.current() is None
    assert turn_budget.reserve("tool_calls")
    turn_budget.consume("tokens", 10)  # no-op, fără să crape


def test_manifest_from_settings_is_cached_and_validated():
    s = Settings()
    assert turn_budget.manifest_from_settings(s) is turn_budget.manifest_from_settings(s)
    assert turn_budget.budget_for(TurnClass.EXACT, s).total_ms == s.turn_budget_exact_ms
