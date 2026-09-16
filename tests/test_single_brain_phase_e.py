"""Faza E pe calea creierului unic: ce făcea `planner.build_plan` și nu mai rula deloc.

Sub `single_brain_enabled`, `agent_stage` cheamă `run_main_brain` și se ÎNTOARCE înainte de
`build_plan`. Asta ștergea tăcut trei comportamente, iar niciunul nu pica vreun test: suita e
dual-path prin design (testele v1 se bazează pe flagul stins, cele v2 îl aprind ele), deci
aprinderea în producție ar fi fost prima măsurătoare.

Fiecare test de aici pinuiește o proprietate pe care numai CODUL o poate garanta, nu modelul:

1. «ceva mai ieftin» = produse strict mai ieftine decât minimul AFIȘAT. Baseline-ul e o
   proprietate a stării conversației; un model care „caută ceva mai ieftin" n-are de unde să-l
   știe. Exact bug-ul din producție pe care calea deterministă a fost scrisă să-l repare: „cea mai
   ieftină 80.99" cu 18.99 în catalog (`tests/test_cheaper_followup.py`).
2. Nimic mai ieftin ⇒ se spune, nu se inventează. Un model pus să găsească ceva mai ieftin când nu
   există va coborî pragul sau va relua setul vechi.
3. Fallback-ul grounded vine CU carduri, fiindcă `displayed_products` se scrie din `reply.products`
   (`worker/processor.py`), nu din `ctx.retrieval` — fără ele, turul următor pierde referința.

ZERO OpenAI / ZERO DB.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.agent import planner as planner_mod
from src.agent.tool_executor import ToolRun
from src.config import get_settings
from src.models import (
    BusinessConfig,
    Contact,
    ConversationState,
    InboundMessage,
    ProductRef,
    Route,
    RouteDecision,
    TurnContext,
)
from src.worker.runner import PipelineDeps
from src.worker.stages import agent as agent_mod
from src.worker.stages.agent import _cheaper_seed, agent_stage

# Ce a văzut clientul: cel mai ieftin AFIȘAT e 80.99.
SHOWN = [
    ProductRef(product_id="p-mid", name="Pure Arc Daily", price=80.99),
    ProductRef(product_id="p-hi", name="Ardent Lab Calm", price=97.99),
]
CHEAPER = {
    "id": "p-cheap",
    "business_id": "b",
    "name": "Rhea Organics Soft",
    "price": 18.99,
    "availability": "in_stock",
    "product_url": "https://shop/cheap",
}


@pytest.fixture(autouse=True)
def _single_brain_on(monkeypatch):
    monkeypatch.setattr(get_settings(), "single_brain_enabled", True)


@pytest.fixture(autouse=True)
def _stub_prompt_inputs(monkeypatch):
    async def _none(conn, business_id, **k):
        return []

    monkeypatch.setattr(agent_mod, "list_category_names", _none)
    monkeypatch.setattr(agent_mod, "list_routing_aliases", _none)


def _ctx(body: str) -> TurnContext:
    ctx = TurnContext(
        turn_id="t1",
        business=BusinessConfig(id="b", slug="demo", name="Demo"),
        contact=Contact(id="c", business_id="b"),
        message=InboundMessage(provider_msg_id="m", body=body),
        conversation_id="conv",
        state=ConversationState(displayed_products=list(SHOWN)),
    )
    ctx.route = RouteDecision(route=Route.SALES)
    return ctx


def _deps() -> PipelineDeps:
    return PipelineDeps(conn=object(), redis=None, llm=SimpleNamespace())


async def _async_value(value):
    return value


async def test_cheaper_seed_carries_the_deterministic_set_to_the_model(monkeypatch):
    """Setul ales de cod ajunge în fața modelului ca rezultat de tool — altfel n-ar exista."""
    captured = {}

    async def fake_cheaper(conn, business_id, ref_ids, max_excl, *, limit=6):
        captured["baseline"] = max_excl
        captured["ref_ids"] = sorted(ref_ids)
        return [dict(CHEAPER)]

    monkeypatch.setattr(planner_mod, "search_cheaper_than", fake_cheaper)
    ctx = _ctx("ceva mai ieftin")
    run = ToolRun(ctx, _deps())

    seed = await _cheaper_seed(ctx, _deps(), run=run, query="ceva mai ieftin", show_more=False)

    # Baseline-ul e minimul AFIȘAT, nu minimul din catalog: asta e proprietatea de stare pe care
    # modelul n-o poate deduce din text.
    assert captured["baseline"] == 80.99
    assert captured["ref_ids"] == ["p-hi", "p-mid"]
    assert seed is not None and len(seed) == 2
    assert seed[0]["tool_calls"][0]["function"]["name"] == "search_products"
    assert "80.99" in seed[0]["tool_calls"][0]["function"]["arguments"]  # price_max = baseline
    assert "Rhea Organics Soft" in seed[1]["content"]  # vederea pe care o citește modelul
    # Produsele intră în evidența turului, altfel validatorul le-ar respinge ca negroundate.
    assert [p["id"] for p in run.retrieved] == ["p-cheap"]


async def test_nothing_cheaper_answers_honestly_instead_of_asking_the_model(monkeypatch):
    """Zero rezultate ⇒ mesaj determinist ȘI niciun seed: turul se încheie, nu se reformulează."""

    async def empty(conn, business_id, ref_ids, max_excl, *, limit=6):
        return []

    monkeypatch.setattr(planner_mod, "search_cheaper_than", empty)
    ctx = _ctx("ceva mai ieftin")
    run = ToolRun(ctx, _deps())

    seed = await _cheaper_seed(ctx, _deps(), run=run, query="ceva mai ieftin", show_more=False)

    assert seed is None
    assert ctx.reply is not None
    assert "cea mai ieftină" in ctx.reply.text.lower()
    assert ctx.reply.cacheable is False  # relativ la setul ACESTUI client (cache poisoning)
    assert not run.retrieved


async def test_agent_stage_stops_the_turn_when_nothing_is_cheaper(monkeypatch):
    """Poarta din `agent_stage`: reply setat ⇒ nu se mai cheamă creierul unic."""

    async def empty(conn, business_id, ref_ids, max_excl, *, limit=6):
        return []

    called = {"brain": 0}

    async def fake_brain(*a, **k):
        called["brain"] += 1

    monkeypatch.setattr(planner_mod, "search_cheaper_than", empty)
    monkeypatch.setattr("src.agent.brain.run_main_brain", fake_brain)
    ctx = _ctx("ceva mai ieftin")

    await agent_stage(ctx, _deps())

    assert called["brain"] == 0
    assert ctx.reply is not None and "cea mai ieftină" in ctx.reply.text.lower()


async def test_show_more_is_not_a_cheaper_followup(monkeypatch):
    """«mai arată-mi» conține „mai", dar e paginare — nu are voie să declanșeze re-căutarea."""
    called = {"n": 0}

    async def spy(conn, business_id, ref_ids, max_excl, *, limit=6):
        called["n"] += 1
        return [dict(CHEAPER)]

    monkeypatch.setattr(planner_mod, "search_cheaper_than", spy)
    ctx = _ctx("mai arată-mi")
    run = ToolRun(ctx, _deps())

    seed = await _cheaper_seed(ctx, _deps(), run=run, query="mai arată-mi", show_more=True)

    assert seed is None and called["n"] == 0


def test_grounded_fallback_carries_cards_so_the_next_turn_keeps_the_reference():
    """`displayed_products` se scrie din `reply.products`, deci text fără carduri = referință
    pierdută la turul următor."""
    from src.agent.brain import _serve_exhausted

    ctx = _ctx("care e mai bun?")
    run = ToolRun(ctx, _deps())
    run.retrieved.append(dict(CHEAPER))

    _serve_exhausted(ctx, run)

    assert ctx.reply is not None
    assert "Rhea Organics Soft" in ctx.reply.text
    assert [p["id"] for p in (ctx.reply.products or [])] == ["p-cheap"]


def test_refusal_without_facts_carries_no_cards():
    """Fără produse, refuzul onest rămâne refuz — carduri sub el ar fi un răspuns contradictoriu."""
    from src.agent.brain import _serve_exhausted

    ctx = _ctx("care e mai bun?")
    run = ToolRun(ctx, _deps())

    _serve_exhausted(ctx, run)

    assert ctx.reply is not None
    assert not ctx.reply.products


async def test_cross_sell_runs_after_the_brain_because_it_reacts_to_the_models_tool(monkeypatch):
    """Singura bucată din Faza E care NU poate fi mutată înaintea buclei: depinde de `cart_add`.

    Sub creierul unic nu există substitut: `related_products` e înregistrat, dar nu apare în
    `_SALES_TOOLS` și în niciun profil, deci modelul nu-l poate chema. Fără apelul de după buclă,
    un `cart_add` n-ar propune niciun complement — deloc, nu „mai rar"."""
    from src.agent.planner import maybe_cross_sell

    ctx = _ctx("adaugă în coș")
    run = ToolRun(ctx, _deps())
    run.added_product = {"id": "p-mid", "name": "Pure Arc Daily", "price": 80.99}
    seen = {"n": 0}

    async def fake_followup(ctx_, deps_, added_id, exclude_ids):
        seen["n"] += 1
        assert added_id == "p-mid"
        return [dict(CHEAPER)], None

    monkeypatch.setattr(planner_mod, "_cart_followup_products", fake_followup)
    # Compunerea e a modelului și nu e subiectul testului: o stingem ca să rămână în cadru DOAR
    # declanșatorul determinist — a ajuns cererea la graful de relații sau nu.
    monkeypatch.setattr(
        planner_mod, "_finalize_rich", lambda *a, **k: _async_value(SimpleNamespace(reply=None))
    )

    from src.agent.prompt_builder import PromptInputs
    from src.safety.policy import SafetyPolicy

    await maybe_cross_sell(
        ctx,
        _deps(),
        run=run,
        inp=PromptInputs.build("Demo", "beauty", "ro", [], []),
        history="",
        policy=SafetyPolicy.for_turn(ctx),
    )

    assert seen["n"] == 1
    assert [e for e in ctx.events if e.type == "cross_sell"]


async def test_brain_rehydrates_displayed_products_when_the_loop_found_nothing(monkeypatch):
    """R3: follow-up neclasificat, niciun tool chemat ⇒ răspundem despre ce e pe ecran, nu «n-am
    găsit». Fără plasa asta, planul care citează produsul afișat pică pe `unknown_product` și turul
    iese cu refuzul generic — degradarea pe care v1 n-o avea."""
    from src.agent import brain as brain_mod
    from src.agent.prompt_builder import PromptInputs

    hydrated = [dict(CHEAPER, id="p-mid", name="Pure Arc Daily", price=80.99)]

    async def fake_by_ids(conn, business_id, ids, **kwargs):
        assert sorted(ids) == ["p-hi", "p-mid"]  # exact ce a văzut clientul
        return hydrated

    monkeypatch.setattr(planner_mod, "get_products_by_ids", fake_by_ids)
    monkeypatch.setattr(
        brain_mod,
        "select_provider",
        lambda **kw: SimpleNamespace(
            provider_version="current_live",
            reason="kill_switch_off",
            pipeline_version="retrieval.v1",
            blocking_code="candidate_flag_off",
        ),
    )
    monkeypatch.setattr(brain_mod, "build_port", lambda ctx, deps, sel, **kw: SimpleNamespace())

    class _NoToolLLM:
        model_agent = "model-de-test"

        async def run_tool_loop_structured(self, system, user, tools, execute, schema, **kw):
            return None, 0  # modelul n-a chemat niciun tool și n-a produs plan valid

        async def complete_schema(self, system, user, schema, **kw):
            raise RuntimeError("fără repair scriptat")

    ctx = _ctx("care e mai bun?")
    deps = PipelineDeps(conn=None, llm=_NoToolLLM())
    run = ToolRun(ctx, deps)

    await brain_mod.run_main_brain(
        ctx,
        deps,
        run=run,
        inp=PromptInputs.build("Demo", "beauty", "ro", [], []),
        tools=[{"type": "function", "function": {"name": "search_products"}}],
        system="SYSTEM",
        user="Mesaj client: care e mai bun?",
        query="care e mai bun?",
    )

    assert [p["id"] for p in run.retrieved] == ["p-mid"]
    assert [e for e in ctx.events if e.type == "brain_rehydrated_displayed"]
    # Și consecința vizibilă: reply-ul vorbește despre produsul real, nu „nu pot confirma".
    assert ctx.reply is not None and "Pure Arc Daily" in ctx.reply.text
