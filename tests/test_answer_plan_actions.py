from src.agent import tool_executor
from src.agent.tool_executor import ToolRun
from src.models import BusinessConfig, Contact, InboundMessage, TurnContext
from src.tools.base import ToolResult
from src.worker.runner import PipelineDeps


def _ctx():
    return TurnContext(
        turn_id="turn-1",
        business=BusinessConfig(id="business-1", slug="demo", name="Demo"),
        contact=Contact(id="contact-1", business_id="business-1"),
        message=InboundMessage(provider_msg_id="message-1", body="adauga in cos"),
        conversation_id="conversation-1",
    )


async def test_tool_run_records_only_successful_mutations(monkeypatch):
    results = [ToolResult(ok=False, error="failed"), ToolResult(ok=True)]

    async def fake_run_tool(ctx, deps, name, args):
        return results.pop(0)

    monkeypatch.setattr(tool_executor, "run_tool", fake_run_tool)
    run = ToolRun(_ctx(), PipelineDeps(conn=object(), redis=None, llm=object()))

    await run.execute("cart_add", {"product_id": "product-1"})
    assert run.successful_action_ids == set()

    await run.execute("cart_add", {"product_id": "product-1"})
    assert run.successful_action_ids == {"cart_add:1"}


async def test_non_mutating_tool_never_creates_action_confirmation(monkeypatch):
    async def fake_run_tool(ctx, deps, name, args):
        return ToolResult(ok=True)

    monkeypatch.setattr(tool_executor, "run_tool", fake_run_tool)
    run = ToolRun(_ctx(), PipelineDeps(conn=object(), redis=None, llm=object()))

    await run.execute("search_products", {"category": "seruri"})

    assert run.successful_action_ids == set()
