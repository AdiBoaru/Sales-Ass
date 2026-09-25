"""NX-326 (kernel v1.0, pasul 0) — argumentele respinse se întorc modelului pe CÂMP.

Înainte, orice `ValidationError` din `SearchArgs(**args)` & co. devenea „Unealta a eșuat.", deci
modelul nu afla ce să corecteze, iar „tool correction rate" (designul kernelului, §H) nu se putea
număra. Vederea numește calea câmpului și constrângerea schemei, NICIODATĂ valoarea trimisă
(poate cita textul clientului, P12). Zero DB, zero LLM."""

import pytest

from src.agent.tool_executor import ToolRun
from src.config import get_settings
from src.models import BusinessConfig, Contact, InboundMessage, TurnContext
from src.tools.base import ARGS_REJECTED, run_tool
from src.worker.runner import PipelineDeps


@pytest.fixture
def flag(monkeypatch):
    def _set(value: bool) -> None:
        monkeypatch.setattr(get_settings(), "tool_field_errors_enabled", value)

    _set(True)
    return _set


def _ctx() -> TurnContext:
    return TurnContext(
        turn_id="t",
        business=BusinessConfig(id="b", slug="d", name="D"),
        contact=Contact(id="c", business_id="b"),
        message=InboundMessage(provider_msg_id="m", body="vreau o cremă"),
        conversation_id="conv",
    )


def _deps() -> PipelineDeps:
    return PipelineDeps(conn=object(), redis=None, llm=None)


async def test_an_out_of_range_field_is_named_with_its_limit(flag):
    ctx = _ctx()
    result = await run_tool(ctx, _deps(), "search_products", {"query": "crema", "limit": 50})

    assert result.ok is False and result.error == ARGS_REJECTED
    assert "limit" in result.llm_view and "le=" in result.llm_view
    event = next(e for e in ctx.events if e.type == "tool_arg_invalid")
    assert event.properties["tool"] == "search_products"
    assert event.properties["fields"] == ["limit"]
    assert event.properties["n"] == 1


async def test_the_rejected_value_never_reaches_the_model_or_the_event(flag):
    ctx = _ctx()
    secret = "sună-mă la 0722123456"
    result = await run_tool(ctx, _deps(), "search_products", {"query": "crema", "limit": secret})

    assert "0722123456" not in result.llm_view
    event = next(e for e in ctx.events if e.type == "tool_arg_invalid")
    assert "0722123456" not in repr(event.properties)


async def test_a_missing_required_field_is_named(flag):
    ctx = _ctx()
    result = await run_tool(ctx, _deps(), "get_product_details", {})

    assert result.error == ARGS_REJECTED
    assert "product_id" in result.llm_view and "missing" in result.llm_view


async def test_other_exceptions_keep_the_generic_message(flag, monkeypatch):
    from src.tools import base

    async def boom(ctx, deps, args):
        raise RuntimeError("db jos")

    monkeypatch.setitem(base.TOOL_REGISTRY, "search_products", boom)
    result = await run_tool(_ctx(), _deps(), "search_products", {"query": "x"})
    assert result.llm_view == "Unealta a eșuat." and result.error == "RuntimeError"


async def test_flag_off_is_the_old_message(flag):
    flag(False)
    ctx = _ctx()
    result = await run_tool(ctx, _deps(), "search_products", {"query": "crema", "limit": 50})

    assert result.llm_view == "Unealta a eșuat."
    assert result.error == "ValidationError"
    assert not any(e.type == "tool_arg_invalid" for e in ctx.events)


async def test_rejected_search_args_are_not_learned(flag):
    run = ToolRun(ctx=_ctx(), deps=_deps())
    await run.execute("search_products", {"query": "crema", "limit": 50})

    assert run.search_args == []
    assert run.called == ["search_products"]


async def test_flag_off_still_learns_rejected_args(flag):
    flag(False)
    run = ToolRun(ctx=_ctx(), deps=_deps())
    await run.execute("search_products", {"query": "crema", "limit": 50})

    assert run.search_args == [{"query": "crema", "limit": 50}]
