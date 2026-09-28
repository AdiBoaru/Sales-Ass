"""NX-336 PR D (felia D1) — bucla restrânsă: `delegate`, `faq`, `order`.

Ce dovedește suita:

- fiecare executor oferă modelului DOAR uneltele lui ∩ uneltele tenantului (`DELEGATE_TOOLS` pe
  `delegate`, `faq_lookup` pe `faq`, `check_order` pe `order`), niciodată o citire de catalog sau o
  mutație;
- o unealtă chemată din afara allowlist-ului NU ajunge la `run_tool`: refuz structurat, numărat;
- răspunsul e proza buclei, compusă de calea v1 (`build_plan(kernel=True)` + `render`);
- fără nicio unealtă permisă pe tenant, executorul rămâne `dark`;
- prin `agent_stage` REAL, journey-ul n11 (livrare, comandă, altceva) e servit de kernel.

Zero model real, zero DB (stub-urile din `tests/kernel/stage_harness.py`)."""

from __future__ import annotations

import json
from types import SimpleNamespace as NS

import pytest

from src.agent import kernel_executors as kx
from src.agent.turn_planner import DELEGATE_TOOLS, PlannedTurn
from src.conversation.ambiguity_gate import GateOutcome
from src.conversation.interpretation import AmbiguityDecision, TurnPlan
from src.conversation.state_v2 import ConversationStateV2
from src.tools import base as tools_base
from src.tools.base import ToolResult
from tests.kernel import replay
from tests.kernel import stage_harness as sh

JOURNEYS = {j.journey_id: j for j in replay.load_journeys()}
N11 = "n11-electronics-acts-without-targets"


def _events(ctx, name):
    return [e.properties for e in ctx.events if e.type == name]


def _planned(executor: str) -> PlannedTurn:
    plan = TurnPlan(executor=executor, product_ids=(), search_args=None, depends_on=None)
    return PlannedTurn(plans=(plan,), primary=0)


def _outcome() -> GateOutcome:
    return GateOutcome(decision=AmbiguityDecision(verdict="act", reason="none", question=None))


class LoopLLM(sh.StageLLM):
    """Bucla scriptată care reține schemele oferite și ce a întors fiecare unealtă."""

    def __init__(self, calls=(), final="Răspunsul modelului."):
        super().__init__()
        self.fx = {"final": final, "tool_calls": list(calls)}
        self.offered: list[list[str]] = []
        self.results: list[str] = []

    async def run_tool_loop(self, system, user, tools, execute, **kw):
        self.offered.append([t["function"]["name"] for t in tools])
        for name, args in self.fx["tool_calls"]:
            self.results.append(await execute(name, args))
        return self.fx["final"]


@pytest.fixture
def electronics(monkeypatch):
    cat = sh.catalog("electronics")
    sh.install(monkeypatch, cat, executors=True)
    return cat


@pytest.fixture
def tool_calls(monkeypatch):
    """`run_tool` înregistrat: ce unealtă a ajuns chiar să ruleze."""
    ran: list[str] = []

    async def run_tool(ctx, deps, name, args):
        ran.append(name)
        return ToolResult(ok=True, llm_view="ok")

    monkeypatch.setattr(tools_base, "run_tool", run_tool)
    monkeypatch.setattr("src.agent.tool_executor.run_tool", run_tool, raising=False)
    return ran


def _ctx(cat, text="x"):
    return sh.build_ctx(cat, ConversationStateV2(), text)


def _deps(llm):
    return NS(db=sh.RecordingDb(), llm=llm)


@pytest.mark.parametrize(
    ("executor", "expected"),
    [
        ("delegate", ["faq_lookup", "check_order"]),
        ("faq", ["faq_lookup"]),
        # FAQ întâi, ca setul v1 de comandă (recenzia D1)
        ("order", ["faq_lookup", "check_order"]),
    ],
)
async def test_each_executor_offers_only_its_tools(electronics, executor, expected):
    llm = LoopLLM()
    ctx = _ctx(electronics)
    assert await kx.execute_read_plans(ctx, _deps(llm), _planned(executor), _outcome()) is True
    [offered] = llm.offered
    assert offered == expected, "ordinea și mulțimea uneltelor tenantului, restrânse"
    assert set(offered) <= DELEGATE_TOOLS


async def test_a_tool_outside_the_allowlist_never_runs(electronics, tool_calls):
    """`run_tool` execută orice unealtă înregistrată; allowlist-ul o oprește ÎNAINTE."""
    llm = LoopLLM(calls=[("cart_add", {"product_id": "el-01"}), ("faq_lookup", {"query": "x"})])
    ctx = _ctx(electronics)
    assert await kx.execute_read_plans(ctx, _deps(llm), _planned("faq"), _outcome()) is True
    assert tool_calls == ["faq_lookup"]
    assert json.loads(llm.results[0]) == {"ok": False, "error": "tool_not_allowed"}
    assert {"name": "cart_add", "turn_id": "t0"} in _events(ctx, "delegate_tool_refused")


async def test_an_invented_tool_name_is_counted_without_its_text(electronics, tool_calls):
    llm = LoopLLM(calls=[("drop_tables", {})])
    ctx = _ctx(electronics)
    await kx.execute_read_plans(ctx, _deps(llm), _planned("delegate"), _outcome())
    assert tool_calls == []
    assert {"name": "unknown", "turn_id": "t0"} in _events(ctx, "delegate_tool_refused")


async def test_the_reply_is_the_loop_prose(electronics):
    # fără cifre și fără afirmații de livrare nesusținute: validatorul căii v1 le respinge, ca azi
    llm = LoopLLM(final="Salut, cu ce te pot ajuta?")
    ctx = _ctx(electronics)
    assert await kx.execute_read_plans(ctx, _deps(llm), _planned("faq"), _outcome()) is True
    assert ctx.reply.text == "Salut, cu ce te pot ajuta?"


async def test_no_allowed_tool_on_the_tenant_stays_dark(monkeypatch, electronics):
    from src.worker.stages import agent as agent_mod

    monkeypatch.setattr(
        agent_mod, "tool_loop_tools", lambda business, route, **kw: (["search_products"], [])
    )
    llm = LoopLLM()
    ctx = _ctx(electronics)
    assert await kx.execute_read_plans(ctx, _deps(llm), _planned("order"), _outcome()) is None
    assert llm.offered == []


@pytest.mark.parametrize("index", [0, 1, 3])
async def test_the_n11_turns_are_served_through_the_real_stage(monkeypatch, electronics, index):
    """Livrarea, comanda și „altceva" din n11, prin `agent_stage` REAL: kernelul servește turul pe
    bucla restrânsă, iar executorul din trace e cel al planului."""
    step = next(ct for ct in sh.chain(JOURNEYS[N11], electronics) if ct.index == index)
    ctx = sh.build_ctx(
        electronics, step.state_before, step.turn.user_input, step.previous, turn_id=f"t{index}"
    )
    llm = sh.StageLLM(step.turn.expect["interpretation"])
    llm.fx["final"] = "Răspuns."
    run = await sh.run_turn(monkeypatch, electronics, ctx, llm)
    assert run.branch_result is True, _events(ctx, "kernel_turn")
    [turn] = _events(ctx, "kernel_turn")
    assert turn["executor"] in kx.DELEGATED_TOOLS and turn["served"] is True


async def test_a_failed_loop_serves_the_v1_fallback_without_a_second_loop(monkeypatch, electronics):
    """Recenzia D1: o eroare de furnizor în bucla restrânsă NU cade pe v1 (care ar rula bucla a
    doua oară, exact când furnizorul e bolnav): kernelul servește fallback-ul căii v1."""
    step = next(ct for ct in sh.chain(JOURNEYS[N11], electronics) if ct.index == 0)
    ctx = sh.build_ctx(electronics, step.state_before, step.turn.user_input, step.previous)
    llm = sh.StageLLM(step.turn.expect["interpretation"])
    loops = []

    async def broken(system, user, tools, execute, **kw):
        loops.append(1)
        raise TimeoutError("furnizor")

    monkeypatch.setattr(llm, "run_tool_loop", broken)
    run = await sh.run_turn(monkeypatch, electronics, ctx, llm)
    assert loops == [1], "o singură buclă pe tur"
    assert run.branch_result is True and ctx.reply is not None
    assert {"error": "TimeoutError", "turn_id": "t0"} in _events(ctx, "delegate_loop_failed")


async def test_an_order_view_is_answered_by_the_order_branch(monkeypatch, electronics):
    """Recenzia D1: o vedere de comandă adusă de `check_order` trece prin ramura ORDER a lui
    `render` (vederea grounded), nu prin validatorul de proză de vânzare, care respingea o sumă
    sau un AWB reale."""
    view = "Comanda 1234: livrata, 129 lei, AWB 5566778899."

    async def run_tool(ctx, deps, name, args):
        return ToolResult(ok=True, llm_view=view, prices=[129.0])

    monkeypatch.setattr("src.agent.tool_executor.run_tool", run_tool, raising=False)
    llm = LoopLLM(
        calls=[("check_order", {"order_number": "1234"})],
        final="Comanda ta 1234, de 129 lei, a fost livrata. AWB 5566778899.",
    )
    ctx = _ctx(electronics)
    assert await kx.execute_read_plans(ctx, _deps(llm), _planned("order"), _outcome()) is True
    assert "n-am găsit produse" not in ctx.reply.text
    assert "1234" in ctx.reply.text


async def test_the_delegate_turn_reports_its_prompt_and_validator(electronics):
    llm = LoopLLM(final="Salut, cu ce te pot ajuta?")
    ctx = _ctx(electronics)
    await kx.execute_read_plans(ctx, _deps(llm), _planned("faq"), _outcome())
    [event] = _events(ctx, "agent_prompt")
    assert event["validator_ok"] is True, "verdictul validatorului ajunge în trace (P10)"


def test_the_refusal_counter_survives_a_fallen_turn():
    assert "delegate_tool_refused" in kx.KERNEL_EXECUTOR_EVENTS
    assert "delegate_loop_failed" in kx.KERNEL_EXECUTOR_EVENTS
