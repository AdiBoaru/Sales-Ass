"""I10 (P0, 2026-10-08) — o mutație cerută și neservită de kernel nu ajunge niciodată pe calea v1.

Cazul real (`w2_corector_cearcane_acoperire#4`, «îl iau pe ăla cu acoperire mai mare»): `cart` pe o
referință `attribute` cu dimensiunea `unmapped` ⇒ resolverul `denotes_property` ⇒ poarta scoate
actul (I24, verdict `act`/`invalid_target`) ⇒ plannerul `reply_only` ⇒ garda executorului oprea
doar `must_ask` ⇒ turul cădea pe v1, iar modelul a pus în coș un produs ales de el.

Clasa: „turul cere o mutație" se judeca doar pe actele RĂMASE după poartă. Acum: (1) se judecă pe
toate actele interpretate; (2) pe un `reply_only` al unui tur care cere o mutație răspunde
kernelul, cu fraza refuzului; (3) plasa: orice cădere pe v1 a unui astfel de tur rulează fără
unelte care scriu (`ToolRun` le refuză, bucla nu le vede)."""

from __future__ import annotations

import json
from types import SimpleNamespace as NS

import pytest

from src.agent import interpreted_turn as it
from src.agent.tool_executor import ToolRun
from src.conversation.interpretation import TurnInterpretation
from src.conversation.state_v2 import ConversationStateV2, DisplayedRef, References
from tests.kernel import stage_harness as sh


@pytest.fixture
def electronics(monkeypatch):
    cat = sh.catalog("electronics")
    sh.install(monkeypatch, cat, executors=True)
    return cat


def _screen(cat) -> ConversationStateV2:
    ids = list(cat.items)[:3]
    shown = tuple(DisplayedRef(p, cat.items[p]["name"], float(cat.items[p]["price"])) for p in ids)
    return ConversationStateV2(references=References(displayed_products=shown))


def _cart_on_a_property() -> TurnInterpretation:
    """Interpretarea reală, redusă: `cart` pe o țintă care numește o proprietate (I24)."""
    return TurnInterpretation.model_validate(
        {
            "thread": "continue",
            "acts": [{"kind": "cart", "targets": ["r1"], "query": None}],
            "changes": [],
            "references": [
                {
                    "id": "r1",
                    "text": "ala cu acoperire mai mare",
                    "kind": "attribute",
                    "ordinal": None,
                    "name": None,
                    "dimension": "unmapped",
                    "value": "acoperire mare",
                    "direction": None,
                }
            ],
            "ambiguities": [],
            "corrects_previous_turn": False,
        }
    )


class LoopLLM(sh.StageLLM):
    """Ține minte uneltele pe care le vede bucla v1."""

    def __init__(self, interpretation):
        super().__init__(interpretation)
        self.loop_tools: list[list[str]] = []

    async def run_tool_loop(self, system, user, tools, execute, **kw):
        self.loop_tools.append([t.get("function", {}).get("name") for t in tools])
        return await super().run_tool_loop(system, user, tools, execute, **kw)


async def test_a_cart_on_a_property_is_refused_by_the_kernel_not_guessed_on_v1(
    monkeypatch, electronics
):
    llm = LoopLLM(_cart_on_a_property())
    ctx = sh.build_ctx(electronics, _screen(electronics), "il iau pe ala cu acoperire mai mare")
    run = await sh.run_turn(monkeypatch, electronics, ctx, llm)
    assert run.branch_result is True, [e.properties for e in ctx.events if e.type == "kernel_turn"]
    assert llm.loop_tools == [], "bucla v1 (cu `cart_add`) nu rulează"
    assert ctx.reply is not None and ctx.reply.text
    assert not [e for e in ctx.events if e.type == "cart_add"]


async def test_a_mutating_turn_the_kernel_does_not_serve_runs_v1_without_writing_tools(
    monkeypatch, electronics
):
    """Plasa: orice altă cădere (aici executorii nu servesc) lasă v1 fără unelte care scriu."""

    async def dark(ctx, deps, planned, outcome, *_):
        return None

    monkeypatch.setattr(it, "execute_plans", dark)
    llm = LoopLLM(_cart_on_a_property())
    ctx = sh.build_ctx(electronics, _screen(electronics), "il iau pe ala cu acoperire mai mare")
    await sh.run_turn(monkeypatch, electronics, ctx, llm)
    assert ctx.mutations_blocked is True
    assert [e.type for e in ctx.events if e.type == "kernel_mutation_blocked"]
    for names in llm.loop_tools:
        assert "cart_add" not in names and "checkout_link" not in names


async def test_tool_run_refuses_a_mutation_when_blocked(electronics):
    ctx = sh.build_ctx(electronics, ConversationStateV2(), "x")
    ctx.mutations_blocked = True
    run = ToolRun(ctx, NS(db=sh.RecordingDb(), llm=None))
    out = json.loads(await run.execute("cart_add", {"product_id": "el-01"}))
    assert out == {"ok": False, "error": "mutation_not_allowed"}
    assert [e.properties["name"] for e in ctx.events if e.type == "mutation_blocked"] == [
        "cart_add"
    ]


async def test_a_turn_without_a_mutation_keeps_its_tools(monkeypatch, electronics):
    """Fără act de coș, plasa nu se aprinde: v1 rulează exact ca azi."""
    find = TurnInterpretation.model_validate(
        {
            "thread": "continue",
            "acts": [{"kind": "chitchat", "targets": [], "query": None}],
            "changes": [],
            "references": [],
            "ambiguities": [],
            "corrects_previous_turn": False,
        }
    )
    llm = LoopLLM(find)
    ctx = sh.build_ctx(electronics, ConversationStateV2(), "salut")
    await sh.run_turn(monkeypatch, electronics, ctx, llm)
    assert ctx.mutations_blocked is False
