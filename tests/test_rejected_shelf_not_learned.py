"""Raftul pe care CĂUTAREA l-a respins ca ghicitură greșită nu devine raftul conversației.

Turul real `a623c53e` (`sole-ro`, 2026-09-23, «vreau o crema de hidratare»): modelul a trimis
`category="fata"` (raftul „Fata" e MACHIAJ), NX-313 l-a judecat contrazis de cerere și l-a scos
(`guessed_filter_rescued`, `dropped_category=true`), iar căutarea a servit creme de față corecte.
Dar `ToolRun` reținea argumentele MODELULUI, deci `observed_category` persista «fata» ca
`search_constraints.category_key`, iar meniul de chips se construia pe subarborele Machiaj:
«Arata-mi ce ai la Machiaj» sub o cerere de cremă hidratantă. Verdictul căutării trebuie să
ajungă până la stare, nu doar până la setul servit. ZERO OpenAI, zero DB."""

from src.agent import tool_executor
from src.agent.tool_executor import ToolRun
from src.conversation.observed_constraints import observed_category
from src.models import BusinessConfig, Contact, InboundMessage, Relevance, TurnContext
from src.tools.base import ToolResult
from src.worker.runner import PipelineDeps


def _ctx() -> TurnContext:
    return TurnContext(
        turn_id="turn-1",
        business=BusinessConfig(id="business-1", slug="demo", name="Demo"),
        contact=Contact(id="contact-1", business_id="business-1"),
        message=InboundMessage(provider_msg_id="m-1", body="vreau o crema de hidratare"),
        conversation_id="conversation-1",
    )


async def _run_search(monkeypatch, relevance: Relevance | None) -> ToolRun:
    async def fake_run_tool(ctx, deps, name, args):
        return ToolResult(ok=True, products=[], relevance=relevance)

    monkeypatch.setattr(tool_executor, "run_tool", fake_run_tool)
    run = ToolRun(_ctx(), PipelineDeps(conn=object(), redis=None, llm=object()))
    await run.execute("search_products", {"category": "fata", "concerns": ["hidratare"]})
    return run


async def test_rejected_guessed_shelf_is_not_learned(monkeypatch):
    run = await _run_search(monkeypatch, Relevance(guessed_category_dropped=True))
    assert run.search_args == [{"concerns": ["hidratare"]}]
    assert observed_category(run.search_args) is None


async def test_kept_shelf_is_still_learned(monkeypatch):
    run = await _run_search(monkeypatch, Relevance())
    assert observed_category(run.search_args) == "fata"


async def test_off_category_signal_is_not_the_same_thing(monkeypatch):
    """`category_dropped` înseamnă altceva (setul e ÎN AFARA categoriei, compose suprimă pick-ul)
    și nu are voie să șteargă raftul din stare: acolo raftul era al clientului, doar gol."""
    run = await _run_search(monkeypatch, Relevance(category_dropped=True))
    assert observed_category(run.search_args) == "fata"


async def test_telemetry_keeps_the_model_argument(monkeypatch):
    """Argumentul modelului rămâne în `tool_call`: e dovada din care se recalibrează NX-313."""
    run = await _run_search(monkeypatch, Relevance(guessed_category_dropped=True))
    call = next(e for e in run.ctx.events if e.type == "tool_call")
    assert call.properties["args"].get("category") == "fata"
