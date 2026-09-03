"""NX-241 — faze măsurate în runner, oprire la deadline cu fallback ONEST, projector pur.

Trei întrebări:
  1. Se vede unde s-a dus timpul? (`turn_latency`, un event per tur, low-cardinality)
  2. Ce se întâmplă când timpul se termină? (răspuns determinist, niciodată tăcere — P6)
  3. Rămâne projectorul pur? (NX-240: măsurăm din AFARA lui, nu îi băgăm un ceas înăuntru)
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from src.agent import fallbacks
from src.config import Settings
from src.models import (
    BusinessConfig,
    Contact,
    InboundMessage,
    RetrievalResult,
    TurnContext,
)
from src.observability import turn_latency
from src.observability.turn_latency import OUTCOME_ERROR, OUTCOME_OK, ms_bucket, span
from src.runtime import deadline
from src.runtime.deadline import PHASES, TurnDeadline
from src.web import turn_executor as tex
from src.worker import runner as rnr
from src.worker.runner import PipelineDeps, run_pipeline


def _ctx() -> TurnContext:
    return TurnContext(
        turn_id="t1",
        business=BusinessConfig(id="b1", slug="demo", name="Demo"),
        contact=Contact(id="c1", business_id="b1"),
        message=InboundMessage(provider_msg_id="m1", body="caut o cremă"),
        conversation_id="conv1",
    )


def _settings(**kw) -> Settings:
    base = dict(TURN_LATENCY_SPANS_ENABLED=True, TURN_DEADLINE_ENABLED=False)
    base.update(kw)
    return Settings(**base)


def _events(ctx: TurnContext, type_: str) -> list[dict]:
    return [e.properties for e in ctx.events if e.type == type_]


# ── acumulatorul de faze ───────────────────────────────────────────────────────────────────
def test_span_records_duration_and_outcome():
    acc, token = turn_latency.push()
    try:
        with span("retrieval"):
            pass
        with span("model") as s:
            s.outcome = "degraded"
    finally:
        turn_latency.pop(token)
    assert acc.by_phase["retrieval"].outcomes == {OUTCOME_OK: 1}
    assert acc.by_phase["model"].outcomes == {"degraded": 1}


def test_span_marks_error_and_reraises():
    acc, token = turn_latency.push()
    try:
        with pytest.raises(ValueError), span("tools"):
            raise ValueError("boom")
    finally:
        turn_latency.pop(token)
    assert acc.by_phase["tools"].outcomes == {OUTCOME_ERROR: 1}


def test_unknown_phase_is_counted_not_named():
    """O etichetă din afara vocabularului ar fi o scurgere de cardinalitate (P12): o numărăm."""
    acc, token = turn_latency.push()
    try:
        turn_latency.record("faza_inventată", 12.0)
    finally:
        turn_latency.pop(token)
    assert acc.unknown_phases == 1 and acc.by_phase == {}


def test_recording_outside_a_turn_is_a_noop():
    assert turn_latency.current() is None
    turn_latency.record("model", 5.0)
    turn_latency.degrade("orice")


def test_ms_bucket_is_bounded():
    """O histogramă de latență are voie la câteva benzi, nu la 60.000 de valori distincte."""
    buckets = {ms_bucket(ms) for ms in range(0, 60_000, 7)}
    assert len(buckets) == 9
    assert ms_bucket(0) == "0-100" and ms_bucket(60_000) == "15000+"


def test_phase_vocabulary_matches_the_deadline_module():
    assert "queue" in PHASES and "aftercare" in PHASES


# ── runner: un event per tur ───────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_runner_emits_one_turn_latency_event_with_phase_breakdown(monkeypatch):
    monkeypatch.setattr(rnr, "get_settings", _settings)
    ctx = _ctx()

    async def gates_stage(ctx, deps):  # noqa: ARG001 — numele contează (mapare fază)
        await asyncio.sleep(0)

    async def triage_stage(ctx, deps):  # noqa: ARG001
        ctx.set_reply("gata")

    await run_pipeline(ctx, PipelineDeps(), [gates_stage, triage_stage])
    events = _events(ctx, "turn_latency")
    assert len(events) == 1
    props = events[0]
    assert set(props["phases"]) == {"gates", "model"}
    assert props["e2e_bucket"] == ms_bucket(props["e2e_ms"])


@pytest.mark.asyncio
async def test_spans_flag_off_emits_nothing(monkeypatch):
    monkeypatch.setattr(rnr, "get_settings", lambda: _settings(TURN_LATENCY_SPANS_ENABLED=False))
    ctx = _ctx()

    async def gates_stage(ctx, deps):  # noqa: ARG001
        ctx.set_reply("gata")

    await run_pipeline(ctx, PipelineDeps(), [gates_stage])
    assert _events(ctx, "turn_latency") == []


@pytest.mark.asyncio
async def test_budget_props_ride_along_when_the_deadline_is_on(monkeypatch):
    monkeypatch.setattr(rnr, "get_settings", lambda: _settings(TURN_DEADLINE_ENABLED=True))
    ctx = _ctx()

    async def gates_stage(ctx, deps):  # noqa: ARG001
        ctx.set_reply("gata")

    await run_pipeline(ctx, PipelineDeps(), [gates_stage])
    props = _events(ctx, "turn_latency")[0]
    assert props["turn_class"] == "recommendation"
    assert props["budget_enforced"] is False  # enforce e separat de deadline
    assert props["deadline_total_ms"] > 0
    assert props["model_calls_bucket"] == "0"


@pytest.mark.asyncio
async def test_query_count_comes_from_the_nx231_accounting(monkeypatch):
    """Un contor paralel de query-uri ar diverge de `db_ops`. Îl citim de la sursă."""
    from src.db import op_metrics

    monkeypatch.setattr(rnr, "get_settings", lambda: _settings(TURN_DEADLINE_ENABLED=True))
    ctx = _ctx()

    async def gates_stage(ctx, deps):  # noqa: ARG001
        op_metrics.record("fake_op", checkout_ms=1.0, hold_ms=2.0)
        op_metrics.record("fake_op", checkout_ms=1.0, hold_ms=2.0)
        ctx.set_reply("gata")

    db_acc, db_token = op_metrics.push()
    try:
        await run_pipeline(ctx, PipelineDeps(), [gates_stage])
    finally:
        op_metrics.pop(db_token)
    assert db_acc.checkouts == 2
    assert _events(ctx, "turn_latency")[0]["query_count_bucket"] == "2"


# ── runner: oprirea la deadline ────────────────────────────────────────────────────────────
def _spent_deadline() -> TurnDeadline:
    """Deadline care se epuizează DUPĂ ce a rulat gates (îl consumă chiar stagiul)."""
    return TurnDeadline(total_ms=1_000, terminal_reserve_ms=200, clock=lambda: 10_000.0)


@pytest.mark.asyncio
async def test_exhausted_deadline_stops_the_pipeline_with_an_honest_reply(monkeypatch):
    monkeypatch.setattr(rnr, "get_settings", lambda: _settings(TURN_DEADLINE_ENABLED=True))
    ctx = _ctx()
    ran: list[str] = []
    d = _spent_deadline()

    async def gates_stage(ctx, deps):  # noqa: ARG001
        ran.append("gates")
        d.elapsed_before_ms = 5_000  # timpul se termină ÎN gates

    async def agent_stage(ctx, deps):  # noqa: ARG001
        ran.append("agent")  # NU trebuie să ruleze

    token = deadline.push(d)
    try:
        await run_pipeline(ctx, PipelineDeps(), [gates_stage, agent_stage])
    finally:
        deadline.pop(token)
    assert ran == ["gates"]  # agentul nu mai pornește: n-ar avea timp să termine
    assert ctx.reply is not None
    assert ctx.reply.text == fallbacks._deadline_msg("ro", partial=False)
    assert ctx.reply.cacheable is False  # un răspuns de epuizare nu otrăvește cache-ul
    exhausted = _events(ctx, "turn_deadline_exhausted")
    assert exhausted and exhausted[0]["reason"] == "expired"


@pytest.mark.asyncio
async def test_exhausted_deadline_shows_already_validated_products(monkeypatch):
    """„Fallback onest pe facts validate": dacă avem deja produse retrievate, le ARĂTĂM. Textul nu
    conține cifre/nume — nu poate inventa nimic și nu poate pica validatorul."""
    monkeypatch.setattr(rnr, "get_settings", lambda: _settings(TURN_DEADLINE_ENABLED=True))
    ctx = _ctx()
    d = _spent_deadline()

    async def gates_stage(ctx, deps):  # noqa: ARG001
        ctx.retrieval = RetrievalResult(products=[{"id": "p1"}, {"id": "p2"}])
        d.elapsed_before_ms = 5_000

    async def agent_stage(ctx, deps):  # noqa: ARG001
        raise AssertionError("nu mai avem timp de agent")

    token = deadline.push(d)
    try:
        await run_pipeline(ctx, PipelineDeps(), [gates_stage, agent_stage])
    finally:
        deadline.pop(token)
    assert ctx.reply.text == fallbacks._deadline_msg("ro", partial=True)
    assert len(ctx.reply.products) == 2


@pytest.mark.asyncio
async def test_deadline_never_speaks_before_the_authority_gate_ran(monkeypatch):
    """Invariantul care contează mai mult decât P6: tăcerea aparține Gates (bot oprit / om a
    preluat). Un deadline consumat ÎNAINTE de gates nu are voie să bage un mesaj de bot într-o
    conversație pe care poate n-o mai deține botul — marginea web o terminalizează onest."""
    monkeypatch.setattr(rnr, "get_settings", lambda: _settings(TURN_DEADLINE_ENABLED=True))
    ctx = _ctx()
    ran: list[str] = []

    async def gates_stage(ctx, deps):  # noqa: ARG001
        ran.append("gates")

    d = _spent_deadline()
    d.elapsed_before_ms = 5_000  # epuizat ÎNAINTE de primul stagiu
    token = deadline.push(d)
    try:
        await run_pipeline(ctx, PipelineDeps(), [gates_stage])
    finally:
        deadline.pop(token)
    assert ran == []
    assert ctx.reply is None  # tăcere, nu un mesaj de bot pe autoritate necunoscută
    assert _events(ctx, "turn_deadline_exhausted")  # dar VIZIBIL, nu tăcut


@pytest.mark.asyncio
async def test_deadline_respects_an_intentional_halt(monkeypatch):
    """Gates a decis tăcere (bot oprit). Deadline-ul nu o transformă într-un mesaj de bot."""
    monkeypatch.setattr(rnr, "get_settings", lambda: _settings(TURN_DEADLINE_ENABLED=True))
    ctx = _ctx()
    d = _spent_deadline()

    async def gates_stage(ctx, deps):  # noqa: ARG001
        ctx.halt_silent("bot_inactive")
        d.elapsed_before_ms = 5_000

    async def agent_stage(ctx, deps):  # noqa: ARG001
        raise AssertionError("halt oprește pipeline-ul")

    token = deadline.push(d)
    try:
        await run_pipeline(ctx, PipelineDeps(), [gates_stage, agent_stage])
    finally:
        deadline.pop(token)
    assert ctx.reply is None and ctx.halt is True


@pytest.mark.asyncio
async def test_an_existing_reply_is_never_overwritten_by_the_deadline(monkeypatch):
    monkeypatch.setattr(rnr, "get_settings", lambda: _settings(TURN_DEADLINE_ENABLED=True))
    ctx = _ctx()
    slow = TurnDeadline(total_ms=200, terminal_reserve_ms=50, clock=lambda: 0.0)

    async def gates_stage(ctx, deps):  # noqa: ARG001
        ctx.set_reply("răspuns bun")
        slow.elapsed_before_ms = 10_000  # timpul se termină DUPĂ ce avem răspuns

    token = deadline.push(slow)
    try:
        await run_pipeline(ctx, PipelineDeps(), [gates_stage])
    finally:
        deadline.pop(token)
    assert ctx.reply.text == "răspuns bun"


@pytest.mark.asyncio
async def test_deadline_flag_off_never_stops_a_turn(monkeypatch):
    monkeypatch.setattr(rnr, "get_settings", _settings)
    ctx = _ctx()
    ran: list[str] = []

    async def gates_stage(ctx, deps):  # noqa: ARG001
        ran.append("gates")

    async def agent_stage(ctx, deps):  # noqa: ARG001
        ran.append("agent")
        ctx.set_reply("ok")

    await run_pipeline(ctx, PipelineDeps(), [gates_stage, agent_stage])
    assert ran == ["gates", "agent"]
    assert _events(ctx, "turn_deadline_exhausted") == []


# ── executorul web: bugetul se naște din ledger ────────────────────────────────────────────
def _row(**kw):
    now = datetime(2026, 8, 16, 12, 0, 0, tzinfo=UTC)
    base = dict(accepted_at=now, deadline_at=now + timedelta(seconds=9))
    base.update(kw)
    return SimpleNamespace(**base)


def test_executor_builds_no_deadline_with_the_flag_off():
    """Flag stins → `None`, NU un deadline nelimitat: prezența unui deadline ar activa căile noi
    (admission de tool, poartă) fără să limiteze nimic."""
    now = datetime(2026, 8, 16, 12, 0, 0, tzinfo=UTC)
    assert tex._build_turn_deadline(_row(), now, _settings()) is None


def test_executor_keeps_the_nx233_wait_when_the_flag_is_off():
    now = datetime(2026, 8, 16, 12, 0, 0, tzinfo=UTC)
    s = _settings()
    assert tex._legacy_deadline_left_s(_row(), now, s) == pytest.approx(9.0)
    assert tex._legacy_deadline_left_s(_row(deadline_at=None), now, s) == float(
        s.web_turn_deadline_s
    )


def test_executor_deadline_keeps_a_terminal_reserve():
    now = datetime(2026, 8, 16, 12, 0, 0, tzinfo=UTC)
    d = tex._build_turn_deadline(_row(), now, _settings(TURN_DEADLINE_ENABLED=True))
    assert d is not None
    assert d.total_ms == 9_000
    assert d.terminal_reserve_ms > 0
    assert d.remaining_ms() < d.remaining_ms(reserve=False)


def test_executor_deadline_subtracts_queue_wait_without_a_ledger_deadline():
    now = datetime(2026, 8, 16, 12, 0, 5, tzinfo=UTC)
    row = _row(accepted_at=datetime(2026, 8, 16, 12, 0, 0, tzinfo=UTC), deadline_at=None)
    d = tex._build_turn_deadline(row, now, _settings(TURN_DEADLINE_ENABLED=True))
    assert d.elapsed_before_ms == 5_000


# ── projectorul rămâne pur ─────────────────────────────────────────────────────────────────
def test_projection_is_measured_from_outside_the_projector():
    """NX-240 interzice orice ceas în `render_v2` (`test_projector_source_contains_no_clock...`).
    NX-241 măsoară proiecția — deci spanul trebuie să fie la APELANT, nu în projector."""
    import inspect

    import src.channels.web.render_v2 as projector
    import src.web.turn_events as caller

    assert "turn_latency" not in inspect.getsource(projector)
    assert 'span("projection")' in inspect.getsource(caller)
