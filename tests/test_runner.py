"""Teste unit pentru pipeline runner (fără DB/servicii → rulează în CI)."""

import asyncio
from types import SimpleNamespace

from src.models import (
    BusinessConfig,
    Contact,
    InboundMessage,
    TurnContext,
)
from src.worker import runner as rnr
from src.worker.runner import PipelineDeps, fallback_stage, run_pipeline


def _ctx(body: str | None = "salut") -> TurnContext:
    return TurnContext(
        turn_id="t1",
        business=BusinessConfig(id="b1", slug="demo", name="Demo"),
        contact=Contact(id="c1", business_id="b1"),
        message=InboundMessage(provider_msg_id="wamid.1", body=body),
        conversation_id="conv1",
    )


def _deps() -> PipelineDeps:
    return PipelineDeps(conn=None)  # stagiile de test nu ating DB


async def test_early_exit_stops_subsequent_stages():
    calls: list[str] = []

    async def stage_a(ctx, deps):
        calls.append("a")
        ctx.set_reply("gata")

    async def stage_b(ctx, deps):
        calls.append("b")

    ctx = _ctx()
    await run_pipeline(ctx, _deps(), [stage_a, stage_b])

    assert calls == ["a"]  # stage_b nu rulează după early exit
    assert ctx.reply is not None
    assert any(e.type == "pipeline_early_exit" for e in ctx.events)


async def test_all_stages_run_when_no_reply():
    calls: list[str] = []

    async def stage_a(ctx, deps):
        calls.append("a")

    async def stage_b(ctx, deps):
        calls.append("b")

    ctx = _ctx()
    await run_pipeline(ctx, _deps(), [stage_a, stage_b])

    assert calls == ["a", "b"]
    assert ctx.reply is None
    assert any(e.type == "pipeline_complete" for e in ctx.events)
    # un stage_completed per stagiu
    assert sum(e.type == "stage_completed" for e in ctx.events) == 2


async def test_fallback_stage_sets_clarify_reply():
    ctx = _ctx(body="ce preț are X?")
    await fallback_stage(ctx, _deps())
    assert ctx.reply is not None
    assert "n-am înțeles" in ctx.reply.text.lower()


async def test_fallback_stage_handles_empty_body():
    ctx = _ctx(body=None)
    await fallback_stage(ctx, _deps())
    assert ctx.reply is not None  # niciodată tăcere


# --- CONV-COMMERCE P0: buget de latență/cost per tur (turn_over_budget) -------


def _budget_settings(monkeypatch, *, enabled=True, lat_ms=5000, cost=0.01):
    monkeypatch.setattr(
        rnr,
        "get_settings",
        lambda: SimpleNamespace(
            turn_budget_alerts_enabled=enabled,
            turn_latency_budget_ms=lat_ms,
            turn_cost_budget_usd=cost,
        ),
    )


async def test_turn_over_budget_emitted_when_slow(monkeypatch):
    _budget_settings(monkeypatch, lat_ms=0)  # buget 0 → orice tur depășește latența

    async def slow(ctx, deps):
        await asyncio.sleep(0.005)  # garantează latență > 0

    ctx = _ctx()
    await run_pipeline(ctx, _deps(), [slow])
    ev = [e for e in ctx.events if e.type == "turn_over_budget"]
    assert ev, "ar trebui emis turn_over_budget"
    assert ev[0].properties["over_latency"] is True
    assert ev[0].properties["slowest_stage"] == "slow"  # din TOATE stagiile (și non-LLM)


async def test_turn_under_budget_no_event(monkeypatch):
    _budget_settings(monkeypatch, lat_ms=999999)  # buget mare → sub buget

    async def fast(ctx, deps):
        pass

    ctx = _ctx()
    await run_pipeline(ctx, _deps(), [fast])
    assert not any(e.type == "turn_over_budget" for e in ctx.events)


async def test_turn_budget_disabled_no_event(monkeypatch):
    _budget_settings(monkeypatch, enabled=False, lat_ms=0)

    async def s(ctx, deps):
        await asyncio.sleep(0.005)

    ctx = _ctx()
    await run_pipeline(ctx, _deps(), [s])
    assert not any(e.type == "turn_over_budget" for e in ctx.events)


def test_emit_turn_budget_over_cost(monkeypatch):
    # Calea de COST (fără LLM real): helperul direct, latență sub buget dar cost peste.
    _budget_settings(monkeypatch, lat_ms=999999, cost=0.001)
    ctx = _ctx()
    rnr._emit_turn_budget(ctx, latency_ms=10.0, cost_usd=0.05, stage_latencies={"agent": 8.0})
    ev = [e for e in ctx.events if e.type == "turn_over_budget"]
    assert ev and ev[0].properties["over_cost"] is True
    assert ev[0].properties["over_latency"] is False
    assert ev[0].properties["slowest_stage"] == "agent"
