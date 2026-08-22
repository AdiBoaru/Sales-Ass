"""NX-239 — MainBrain (`brain.run_main_brain`): plan structurat în aceeași buclă, validare,
UN repair bounded, fallback determinist non-gol, portul NX-238 ales de selector (nu de brain).

ZERO OpenAI / ZERO DB: LLM fals + port fals + `ToolRun` peste deps cu conn=None."""

from __future__ import annotations

from types import SimpleNamespace

from src.agent import brain as brain_mod
from src.agent.answer_plan import AnswerPlanV2
from src.agent.answer_plan_runtime import safe_fallback
from src.agent.prompt_builder import PromptInputs
from src.agent.tool_executor import ToolRun
from src.models import BusinessConfig, Contact, InboundMessage, TurnContext
from src.retrieval.port import RetrievalBundle
from src.worker.runner import PipelineDeps


def _ctx(body: str = "vreau un ser pentru ten uscat") -> TurnContext:
    return TurnContext(
        turn_id="t1",
        business=BusinessConfig(id="b1", slug="demo", name="Demo"),
        contact=Contact(id="c1", business_id="b1"),
        message=InboundMessage(provider_msg_id="m1", body=body),
        conversation_id="conv1",
    )


_PRODUCT = {
    "id": "p1",
    "business_id": "b1",
    "name": "Ser LumaDerm",
    "price": 89.0,
    "availability": "in_stock",
    "product_url": "https://demo.example/p1",
}


def _plan_dict(**overrides) -> dict:
    base = {
        "schema_version": 2,
        "business_id": "b1",
        "locale": "ro",
        "intent_summary": "recomandare ser ten uscat",
        "obligations": [{"kind": "recommend", "key": "recommend_0"}],
        "direct_answer": "Pentru ten uscat, serul LumaDerm e potrivit: hidratează intens.",
        "selected_products": [
            {"product_id": "p1", "variant_id": None, "evidence_ids": ["product:p1:identity"]}
        ],
        "claims": [],
        "facts": {"prices": [], "stocks": [], "urls": []},
        "recommendations": [
            {
                "product_id": "p1",
                "variant_id": None,
                "reason": "are acid hialuronic, potrivit pentru ten uscat",
                "evidence_ids": ["product:p1:identity"],
                "need_ids": [],
            }
        ],
        "comparison": None,
        "constraints_applied": [],
        "unknowns": [],
        "relaxations": [],
        "clarification": None,
        "no_results": None,
        "state_update_proposals": [],
        "action_intents": [],
        "disclosures": [],
        "handoff": False,
        "confirmed_actions": [],
        "style_signals": {"tone": "neutral", "verbosity": "short"},
    }
    base.update(overrides)
    return base


class _FakePort:
    """Port fals: întoarce produsele date; opțional aruncă (timeout de provider)."""

    provider_version = "current_live.v1"

    def __init__(self, products=(_PRODUCT,), raise_error: Exception | None = None):
        self.products = list(products)
        self.raise_error = raise_error
        self.last_result = None
        self.calls = 0

    async def retrieve(self, snapshot, spec, active_needs=(), deadline=None):
        self.calls += 1
        if self.raise_error is not None:
            raise self.raise_error
        return RetrievalBundle(
            provider_version=self.provider_version, products=tuple(self.products)
        )


class _FakeLLM:
    """LLM fals: bucla structurată întoarce planul scriptat (opțional după un tool round);
    `complete_schema` = repair-ul."""

    model_agent = "model-de-test"

    def __init__(self, plan=None, *, repair=None, search_args=None, loop_error=None):
        self.plan = plan
        self.repair = repair
        self.search_args = search_args
        self.loop_error = loop_error
        self.captured: dict = {}
        self.repair_calls = 0

    async def run_tool_loop_structured(self, system, user, tools, execute, schema, **kw):
        self.captured = {"system": system, "user": user, "tools": tools}
        rounds = 0
        if self.search_args is not None:
            await execute("search_products", self.search_args)
            rounds = 1
        if self.loop_error is not None:
            raise self.loop_error
        return self.plan, rounds

    async def complete_schema(self, system, user, schema, **kw):
        self.repair_calls += 1
        if isinstance(self.repair, Exception):
            raise self.repair
        if self.repair is None:
            raise RuntimeError("no repair scripted")
        return self.repair


def _inp() -> PromptInputs:
    return PromptInputs.build("Demo", "beauty", "ro", [], [])


async def _run(ctx, llm, port, monkeypatch) -> ToolRun:
    monkeypatch.setattr(
        brain_mod,
        "select_provider",
        lambda **kw: SimpleNamespace(
            provider_version=port.provider_version,
            reason="kill_switch_off",
            pipeline_version="retrieval.v1",
            blocking_code="candidate_flag_off",
        ),
    )
    monkeypatch.setattr(brain_mod, "build_port", lambda ctx, deps, sel, **kw: port)
    deps = PipelineDeps(conn=None, llm=llm)
    run = ToolRun(ctx, deps)
    await brain_mod.run_main_brain(
        ctx,
        deps,
        run=run,
        inp=_inp(),
        tools=[{"type": "function", "function": {"name": "search_products"}}],
        system="SYSTEM-DE-BAZA",
        user="Mesaj client: vreau un ser pentru ten uscat",
        query="vreau un ser pentru ten uscat",
    )
    return run


def _events(ctx, type_):
    return [e for e in ctx.events if e.type == type_]


# --- happy path -----------------------------------------------------------------


async def test_valid_plan_becomes_reply(monkeypatch):
    ctx = _ctx()
    llm = _FakeLLM(plan=_plan_dict(), search_args={"query": "ser ten uscat"})
    port = _FakePort()
    run = await _run(ctx, llm, port, monkeypatch)

    assert ctx.reply is not None
    assert "LumaDerm" in ctx.reply.text
    assert ctx.reply.cacheable is False
    assert isinstance(ctx.answer_plan, AnswerPlanV2)
    assert run.retrieved and run.retrieved[0]["id"] == "p1"
    assert port.calls == 1  # căutarea a trecut prin PORT, nu direct prin tool
    final = _events(ctx, "main_brain_call")[-1]
    assert final.properties["outcome"] == "ok"
    assert final.properties["prompt_version"] == brain_mod.BRAIN_PROMPT_VERSION
    assert _events(ctx, "answer_plan_validation")[0].properties["outcome"] == "ok"
    assert _events(ctx, "conversation_quality")  # checkurile obiective au rulat


async def test_plan_final_in_same_loop_with_structured_schema(monkeypatch):
    ctx = _ctx()
    llm = _FakeLLM(plan=_plan_dict())
    await _run(ctx, llm, _FakePort(), monkeypatch)
    # promptul de plan e ADĂUGAT system-ului generat din DB, nu îl înlocuiește
    assert llm.captured["system"].startswith("SYSTEM-DE-BAZA")
    assert "AnswerPlanV2" in llm.captured["system"]
    # brain-ul nu știe providerul: numele lui nu apare nicăieri în prompt
    assert "current_live" not in llm.captured["system"]
    assert "current_live" not in llm.captured["user"]
    # obligațiile deterministe intră în USER
    assert "recommend" in llm.captured["user"]


async def test_demoted_signals_reach_brain_prompt(monkeypatch):
    from src.agent.brain_models import BrainSignal

    ctx = _ctx("cât costă livrarea? și vreau un ser")
    ctx.brain_signals.append(
        BrainSignal(stage="faq_stage", kind="stage_reply", text="Livrarea costă 20 lei.")
    )
    llm = _FakeLLM(plan=_plan_dict())
    await _run(ctx, llm, _FakePort(), monkeypatch)
    assert "[context faq_stage] Livrarea costă 20 lei." in llm.captured["user"]


# --- repair + fallback ----------------------------------------------------------


async def test_invalid_loop_then_repair_recovers(monkeypatch):
    ctx = _ctx()
    llm = _FakeLLM(
        loop_error=ValueError("json invalid"), repair=_plan_dict(), search_args={"query": "ser"}
    )
    await _run(ctx, llm, _FakePort(), monkeypatch)
    assert ctx.reply is not None and "LumaDerm" in ctx.reply.text
    assert llm.repair_calls == 1
    assert _events(ctx, "repair")[0].properties["outcome"] == "ok"


async def test_repair_exhausted_falls_back_deterministically(monkeypatch):
    ctx = _ctx()
    llm = _FakeLLM(loop_error=ValueError("json invalid"), repair=RuntimeError("tot invalid"))
    await _run(ctx, llm, _FakePort(), monkeypatch)
    assert ctx.reply is not None
    assert ctx.reply.text == safe_fallback("ro")  # non-gol, determinist (P6)
    assert llm.repair_calls == 1  # UN singur repair, nu o buclă
    assert _events(ctx, "repair")[0].properties["outcome"] == "exhausted"
    assert _events(ctx, "answer_plan_validation")[0].properties["outcome"] == "fallback"


async def test_invented_product_rejected_then_fallback(monkeypatch):
    bad = _plan_dict(
        selected_products=[
            {"product_id": "fantoma", "variant_id": None, "evidence_ids": ["e-fals"]}
        ]
    )
    ctx = _ctx()
    llm = _FakeLLM(plan=bad, repair=bad, search_args={"query": "ser"})
    await _run(ctx, llm, _FakePort(), monkeypatch)
    # Produsul inventat e respins — dar respingerea NU e un motiv să ascundem catalogul real.
    # Testul cerea înainte exact `safe_fallback`, adică fixa în piatră degradarea „nu pot
    # confirma" cu produsul valid în retrieval; acum cerem paritatea cu v1: faptul real, servit.
    assert "fantoma" not in ctx.reply.text
    assert "LumaDerm" in ctx.reply.text
    assert "89.00" in ctx.reply.text


async def test_invented_action_intent_rejected(monkeypatch):
    bad = _plan_dict(action_intents=["launch_rocket"])
    ctx = _ctx()
    llm = _FakeLLM(plan=bad, repair=bad)
    await _run(ctx, llm, _FakePort(), monkeypatch)
    assert ctx.reply.text == safe_fallback("ro")


# --- provider timeout / degradare -----------------------------------------------


async def test_port_failure_visible_and_honest(monkeypatch):
    honest = _plan_dict(
        selected_products=[],
        recommendations=[],
        direct_answer="",
        no_results={
            "reason_class": "dependency_unavailable",
            "criteria": [],
            "alternatives": [],
        },
    )
    ctx = _ctx()
    llm = _FakeLLM(plan=honest, search_args={"query": "ser"})
    port = _FakePort(raise_error=TimeoutError("provider down"))
    await _run(ctx, llm, port, monkeypatch)
    assert ctx.reply is not None and ctx.reply.text.strip()  # fără tăcere
    assert "nu e disponibilă" in ctx.reply.text or "încearcă din nou" in ctx.reply.text
    assert _events(ctx, "no_results")[0].properties["reason_class"] == "dependency_unavailable"
    tool_events = _events(ctx, "tool_call")
    assert tool_events and tool_events[0].properties["ok"] is False


# --- state proposals + clarificare ----------------------------------------------


async def test_state_proposals_go_through_reducer_channel(monkeypatch):
    plan = _plan_dict(state_update_proposals=[{"op": "set_need", "key": "budget_max", "value": 70}])
    ctx = _ctx()
    await _run(ctx, _FakeLLM(plan=plan, search_args={"query": "ser"}), _FakePort(), monkeypatch)
    ops = [(p.op, p.key, p.source) for p in ctx.state_proposals]
    assert ("set_need", "budget_max", "model_inferred") in ops


async def test_clarification_rendered_at_most_once(monkeypatch):
    plan = _plan_dict(
        clarification={
            "question": "Pentru ce tip de ten cauți serul?",
            "target_need": "concerns",
            "reason": "îngustează setul",
            "options": [],
        }
    )
    ctx = _ctx()
    await _run(ctx, _FakeLLM(plan=plan, search_args={"query": "ser"}), _FakePort(), monkeypatch)
    assert ctx.reply.text.count("?") == 1
    assert ctx.reply.text.endswith("?")


async def test_retrieval_gate_event_names_selected_provider(monkeypatch):
    ctx = _ctx()
    await _run(
        ctx, _FakeLLM(plan=_plan_dict(), search_args={"query": "ser"}), _FakePort(), monkeypatch
    )
    gate = _events(ctx, "retrieval_gate")[0]
    assert gate.properties["decision"] == "current_live.v1"
