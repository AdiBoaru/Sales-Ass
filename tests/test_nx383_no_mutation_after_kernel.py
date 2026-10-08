"""NX-383 — un tur interpretat de kernel nu scrie niciodată prin calea v1.

Turul real `w2_corector_cearcane_acoperire#4` (setul wide-2026-10-07): «il iau pe ala cu acoperire
mai mare» ⇒ `cart` pe o referință care numea o proprietate. Poarta a scos actul (regula 0, I24) cu
verdictul `act`, garda executorului oprea doar `must_ask`, deci turul a căzut pe v1, unde modelul a
pus în coș un produs ales de el. Pe același set, singura altă mutație de pe calea v1 după kernel a
fost o abonare la stoc pe «ok pa».

Ce dovedește suita:

- un tur al cărui singur act era o mutație scoasă de poartă e un tur care a cerut o scriere, deci
  `reply_only` primește fraza pachetului pe ORICE verdict, nu doar `must_ask`;
- o mutație scoasă lângă o cerere de citire lasă citirea să fie servită;
- `ToolRun(mutations_allowed=False)` refuză orice mutație (și un nume necunoscut) ÎNAINTE de
  `run_tool`, iar citirile rulează;
- prin `agent_stage` REAL: turul din producție primește fraza, fără nicio scriere; un tur care cade
  pe v1 în modul servit nu oferă modelului mutații, iar una cerută totuși nu rulează; în modul dark
  calea v1 rămâne neatinsă.

Zero model real, zero DB (stub-urile din `tests/kernel/stage_harness.py`)."""

from __future__ import annotations

import json
from types import SimpleNamespace as NS

import pytest

from src.agent import kernel_executors as kx
from src.agent.interpreted_turn import _mutating_turn
from src.agent.tool_executor import ToolRun
from src.agent.turn_planner import PlannedTurn
from src.conversation.ambiguity_gate import GateOutcome
from src.conversation.interpretation import (
    Act,
    AmbiguityDecision,
    Reference,
    TurnInterpretation,
    TurnPlan,
)
from src.tools.base import ToolResult
from src.worker.stages.agent import _without_mutations
from tests.kernel import replay
from tests.kernel import stage_harness as sh

JOURNEYS = {j.journey_id: j for j in replay.load_journeys()}
MUTATIONS = {"cart_add", "checkout_link", "subscribe_back_in_stock"}


def _events(ctx, name):
    return [e.properties for e in ctx.events if e.type == name]


def _chain(kinds, skipped=(), verdict="act"):
    return NS(
        interpreted=NS(interpretation=NS(acts=[NS(kind=k) for k in kinds])),
        outcome=GateOutcome(
            decision=AmbiguityDecision(verdict=verdict, reason="invalid_target", question=None),
            skipped_acts=tuple(skipped),
        ),
    )


@pytest.mark.parametrize(
    ("kinds", "skipped", "expected"),
    [
        (["cart"], (), True),
        (["find", "cart"], (), True),
        # turul real: singurul act, coșul, scos de poartă
        (["cart"], (0,), True),
        (["chitchat", "cart"], (1,), True),
        # o cerere de citire rămasă se servește
        (["cart", "find"], (0,), False),
        (["cart", "detail"], (0,), False),
        (["chitchat"], (), False),
        (["find"], (), False),
    ],
)
def test_a_turn_asked_to_write_when_its_only_request_was_a_dropped_mutation(
    kinds, skipped, expected
):
    assert _mutating_turn(_chain(kinds, skipped)) is expected


@pytest.fixture
def electronics(monkeypatch):
    cat = sh.catalog("electronics")
    sh.install(monkeypatch, cat, executors=True)
    return cat


def _reply_only():
    plan = TurnPlan(executor="reply_only", product_ids=[], search_args=None, depends_on=None)
    return PlannedTurn(plans=(plan,), primary=0)


@pytest.mark.parametrize("verdict", ["act", "must_ask"])
async def test_a_refused_mutation_is_answered_on_any_verdict(electronics, verdict):
    ctx = sh.build_ctx(electronics, sh.ConversationStateV2(), "x")
    outcome = GateOutcome(
        decision=AmbiguityDecision(verdict=verdict, reason="invalid_target", question=None)
    )
    deps = NS(db=sh.RecordingDb(), llm=sh.StageLLM())
    assert await kx.execute_read_plans(ctx, deps, _reply_only(), outcome, None, True) is True
    assert ctx.reply.text == kx.kernel_sentence(electronics.pack, "ro", "mutation_not_exact")


# --- ToolRun: refuzul mutației ------------------------------------------------------------------


@pytest.fixture
def ran(monkeypatch):
    names: list[str] = []

    async def run_tool(ctx, deps, name, args):
        names.append(name)
        return ToolResult(ok=True, llm_view="ok")

    monkeypatch.setattr("src.agent.tool_executor.run_tool", run_tool, raising=False)
    return names


@pytest.mark.parametrize("name", sorted(MUTATIONS))
async def test_a_run_without_write_rights_refuses_every_mutation(electronics, ran, name):
    ctx = sh.build_ctx(electronics, sh.ConversationStateV2(), "x")
    run = ToolRun(ctx, NS(db=sh.RecordingDb(), llm=None), mutations_allowed=False)
    out = await run.execute(name, {"product_id": "el-01"})
    assert json.loads(out) == {"ok": False, "error": "tool_not_allowed"}
    assert ran == [] and run.called == []
    assert {"name": name, "turn_id": "t0"} in _events(ctx, "mutation_tool_refused")


async def test_an_unknown_tool_name_is_a_mutation_and_is_refused(electronics, ran):
    ctx = sh.build_ctx(electronics, sh.ConversationStateV2(), "x")
    run = ToolRun(ctx, NS(db=sh.RecordingDb(), llm=None), mutations_allowed=False)
    await run.execute("drop_everything", {})
    assert ran == []
    assert {"name": "unknown", "turn_id": "t0"} in _events(ctx, "mutation_tool_refused")


async def test_a_run_without_write_rights_still_reads(electronics, ran):
    ctx = sh.build_ctx(electronics, sh.ConversationStateV2(), "x")
    run = ToolRun(ctx, NS(db=sh.RecordingDb(), llm=None), mutations_allowed=False)
    await run.execute("faq_lookup", {"query": "livrare"})
    assert ran == ["faq_lookup"]


async def test_a_default_run_keeps_its_write_rights(electronics, ran):
    """Executorul de coș al kernelului construiește `ToolRun(ctx, deps)`: dreptul e implicit."""
    ctx = sh.build_ctx(electronics, sh.ConversationStateV2(), "x")
    run = ToolRun(ctx, NS(db=sh.RecordingDb(), llm=None))
    await run.execute("cart_add", {"product_id": "el-01"})
    assert ran == ["cart_add"]


def test_without_mutations_keeps_only_the_reads():
    schemas = [{"function": {"name": n}} for n in ["search_products", *sorted(MUTATIONS), "zzz"]]
    assert [s["function"]["name"] for s in _without_mutations(schemas)] == ["search_products"]


# --- prin agent_stage REAL ------------------------------------------------------------------------


class OfferLLM(sh.StageLLM):
    """`StageLLM` care reține uneltele oferite buclei v1 și cheamă `cart_add` pe un id din ecran."""

    def __init__(self, interpretation, calls=()):
        super().__init__(interpretation)
        self.fx["tool_calls"] = list(calls)
        self.fx["final"] = "Gata."
        self.offered: list[list[str]] = []

    async def run_tool_loop(self, system, user, tools, execute, **kw):
        self.offered.append([t["function"]["name"] for t in tools])
        return await super().run_tool_loop(system, user, tools, execute, **kw)


def _cart_step(cat):
    """Turul 2 din g01 (un telefon pe ecran), cu interpretarea turului real: coșul pe o referință
    `attribute` care numește o proprietate, nu un produs."""
    journey = JOURNEYS["g01-electronics-cart-on-an-exact-target"]
    step = next(ct for ct in sh.chain(journey, cat) if ct.index == 1)
    interp = TurnInterpretation(
        thread="continue",
        acts=[Act(kind="cart", targets=["r1"], query=None)],
        changes=[],
        references=[
            Reference(
                id="r1",
                text="ala cu acoperire mai mare",
                kind="attribute",
                ordinal=None,
                name=None,
                dimension="unmapped",
                value="acoperire mai mare",
                direction=None,
            )
        ],
        ambiguities=[],
        corrects_previous_turn=False,
    )
    ctx = sh.build_ctx(
        cat, step.state_before, "il iau pe ala cu acoperire mai mare", step.previous, turn_id="t1"
    )
    return interp, ctx


@pytest.fixture
def writes(monkeypatch):
    calls: list[str] = []

    async def run_tool(ctx, deps, name, args):
        calls.append(name)
        return ToolResult(ok=True, llm_view="ok")

    monkeypatch.setattr("src.agent.tool_executor.run_tool", run_tool, raising=False)
    return calls


async def test_the_real_turn_says_it_cannot_tell_and_writes_nothing(
    monkeypatch, electronics, writes
):
    interp, ctx = _cart_step(electronics)
    llm = OfferLLM(interp, calls=[("cart_add", {"product_id": "el-01"})])
    run = await sh.run_turn(monkeypatch, electronics, ctx, llm)
    decision = [e for e in _events(ctx, "ambiguity_decision")]
    assert decision and decision[-1]["reason"] == "invalid_target", decision
    assert run.branch_result is True, _events(ctx, "kernel_turn")
    assert ctx.reply.text == kx.kernel_sentence(electronics.pack, "ro", "mutation_not_exact")
    assert writes == [] and llm.offered == [], "bucla v1 nu rulează deloc"


def _act_interp(kind):
    return TurnInterpretation(
        thread="aside" if kind == "chitchat" else "continue",
        acts=[Act(kind=kind, targets=[], query=None)],
        changes=[],
        references=[],
        ambiguities=[],
        corrects_previous_turn=False,
    )


async def test_a_turn_that_falls_to_v1_gets_no_mutation_tools(monkeypatch, electronics, writes):
    """«ok pa» ⇒ `chitchat` ⇒ `reply_only` rămâne pe v1. Modelul nu vede nicio mutație, iar una
    cerută totuși (abonarea din setul wide) nu rulează."""
    _, ctx = _cart_step(electronics)
    ctx.message.body = "ok pa"
    llm = OfferLLM(
        _act_interp("chitchat"), calls=[("subscribe_back_in_stock", {"product_id": "el-01"})]
    )
    run = await sh.run_turn(monkeypatch, electronics, ctx, llm)
    assert run.branch_result is False
    assert llm.offered and not MUTATIONS & set(llm.offered[-1])
    assert writes == []
    assert {"name": "subscribe_back_in_stock", "turn_id": "t1"} in _events(
        ctx, "mutation_tool_refused"
    )


async def test_in_dark_mode_the_v1_loop_keeps_its_tools(monkeypatch, electronics, writes):
    """Dark (NX-353) = turul cu flagul stins: calea v1 rămâne byte-identică, cu mutațiile ei."""
    from src.config import get_settings

    monkeypatch.setattr(get_settings(), "interpreted_turn_enabled", False)
    monkeypatch.setattr(get_settings(), "interpreted_turn_dark_enabled", True)
    _, ctx = _cart_step(electronics)
    ctx.message.body = "ok pa"
    llm = OfferLLM(_act_interp("chitchat"), calls=[("cart_add", {"product_id": "el-01"})])
    await sh.run_turn(monkeypatch, electronics, ctx, llm)
    assert llm.offered and "cart_add" in llm.offered[-1]
    assert writes == ["cart_add"]
