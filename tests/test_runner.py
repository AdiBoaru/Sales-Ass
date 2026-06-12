"""Teste unit pentru pipeline runner (fără DB/servicii → rulează în CI)."""

from src.models import (
    BusinessConfig,
    Contact,
    InboundMessage,
    TurnContext,
)
from src.worker.runner import PipelineDeps, echo_stage, run_pipeline


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


async def test_echo_stage_reflects_body():
    ctx = _ctx(body="ce preț are X?")
    await echo_stage(ctx, _deps())
    assert ctx.reply is not None
    assert "ce preț are X?" in ctx.reply.text


async def test_echo_stage_handles_empty_body():
    ctx = _ctx(body=None)
    await echo_stage(ctx, _deps())
    assert ctx.reply is not None  # niciodată tăcere
