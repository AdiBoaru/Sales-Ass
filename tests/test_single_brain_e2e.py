"""NX-239 — e2e pe pipeline-ul REAL (`run_pipeline` + stagii reale + control plane) cu LLM
scriptat și port de retrieval fals. ZERO OpenAI / ZERO DB.

Verifică cap-coadă: flag OFF = comportamentul de azi (primul reply câștigă, byte-identic);
flag ON = mesajul mixt trece de fast path-urile incomplete și ajunge la MainBrain, care acoperă
TOATE obligațiile; selectorul NX-238 (real) alege current live pe NOT-READY."""

from __future__ import annotations

from src.agent import brain as brain_mod
from src.config import get_settings
from src.models import BusinessConfig, Contact, InboundMessage, TurnContext
from src.worker.runner import PipelineDeps, fallback_stage, run_pipeline
from src.worker.stages import agent as agent_stage_mod
from src.worker.stages import triage as triage_mod
from src.worker.stages.agent import agent_stage
from src.worker.stages.greeting import greeting_stage
from src.worker.stages.triage import triage_stage
from tests.test_single_brain import _FakePort, _plan_dict

_PRODUCT = {
    "id": "p1",
    "business_id": "b1",
    "name": "Ser LumaDerm",
    "price": 89.0,
    "availability": "in_stock",
    "product_url": "https://demo.example/p1",
}


def _ctx(body: str) -> TurnContext:
    return TurnContext(
        turn_id="t1",
        business=BusinessConfig(id="b1", slug="demo", name="Demo", vertical="beauty"),
        contact=Contact(id="c1", business_id="b1"),
        message=InboundMessage(provider_msg_id="m1", body=body),
        conversation_id="conv1",
    )


class _ScriptedLLM:
    """Nano (classify_json) + MainBrain (run_tool_loop_structured) scriptate."""

    model_agent = "model-de-test"

    def __init__(self, *, triage_out: dict, plan: dict | None = None, search: bool = True):
        self.triage_out = triage_out
        self.plan = plan
        self.search = search
        self.captured_user: str | None = None
        self.tool_loop_calls = 0

    async def classify_json(self, system, user, *, model=None):
        return dict(self.triage_out)

    async def run_tool_loop_structured(self, system, user, tools, execute, schema, **kw):
        self.tool_loop_calls += 1
        self.captured_user = user
        rounds = 0
        if self.search:
            await execute("search_products", {"query": "ser ten uscat"})
            rounds = 1
        return dict(self.plan), rounds

    async def complete_schema(self, system, user, schema, **kw):
        raise RuntimeError("repair neprogramat în acest scenariu")

    async def run_tool_loop(self, system, user, tools, execute, **kw):
        raise AssertionError("calea legacy nu trebuie chemată sub single-brain")


async def _faq_hit_stage(ctx, deps):
    """Simulează un hit FAQ canonic (conținut authored din DB), fără DB reală."""
    ctx.set_reply("Livrarea costă 20 lei prin curier.")


_faq_hit_stage.__name__ = "faq_stage"


def _wire(monkeypatch, *, flag: bool, port: _FakePort | None = None) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "single_brain_enabled", flag, raising=False)
    # Scenariul acestui fișier e cel al NX-239: nano ÎNCĂ rulează sincron, iar control plane-ul îi
    # DEMOTEAZĂ reply-ul în semnal. Cu NX-251 aprins triajul nu mai rulează pe drumul sincron, deci
    # nu mai există reply de demotat și `brain_signals` rămâne gol — corect, dar altă arhitectură.
    # Pinuim explicit, ca testul să măsoare mecanismul pe care îl numește, nu `.env`-ul mașinii.
    monkeypatch.setattr(settings, "triage_sync_shadow_enabled", False, raising=False)

    async def _no_categories(conn, business_id):
        return []

    monkeypatch.setattr(triage_mod, "list_category_slugs", _no_categories)
    monkeypatch.setattr(agent_stage_mod, "list_category_names", _no_categories)

    async def _no_aliases(conn, business_id):
        return []

    monkeypatch.setattr(agent_stage_mod, "list_routing_aliases", _no_aliases)
    if port is not None:
        monkeypatch.setattr(brain_mod, "build_port", lambda ctx, deps, sel, **kw: port)


_STAGES = [greeting_stage, _faq_hit_stage, triage_stage, agent_stage, fallback_stage]


async def test_flag_off_first_reply_wins_byte_identical(monkeypatch):
    _wire(monkeypatch, flag=False)
    llm = _ScriptedLLM(triage_out={"route": "sales"}, plan=_plan_dict())
    ctx = _ctx("cât costă livrarea? și vreau un ser pentru ten uscat")
    await run_pipeline(ctx, PipelineDeps(conn=None, llm=llm), _STAGES)
    # azi: FAQ-ul răspunde primul și turul se închide — exact comportamentul păstrat sub OFF
    assert ctx.reply.text == "Livrarea costă 20 lei prin curier."
    assert llm.tool_loop_calls == 0
    assert ctx.brain_signals == []


async def test_flag_on_mixed_faq_plus_recommendation_reaches_brain(monkeypatch):
    port = _FakePort(products=(_PRODUCT,))
    _wire(monkeypatch, flag=True, port=port)
    plan = _plan_dict(
        obligations=[
            {"kind": "answer", "key": "question_0"},
            {"kind": "recommend", "key": "recommend_1"},
        ],
        # fără cifra de livrare: prețul din FAQ nu e evidence de retrieval, iar validatorul
        # de proză (stagiul 8, păstrat cu drept de veto) ar respinge-o — exact contractul D8.
        direct_answer=("Comanda ajunge prin curier. Pentru ten uscat, serul LumaDerm e potrivit."),
    )
    llm = _ScriptedLLM(triage_out={"route": "sales", "confidence": "high"}, plan=plan)
    ctx = _ctx("cât costă livrarea? și vreau un ser pentru ten uscat")
    await run_pipeline(ctx, PipelineDeps(conn=None, llm=llm), _STAGES)

    # FAQ-ul NU a închis turul: reply-ul lui a devenit semnal pentru brain
    assert len(ctx.brain_signals) == 1
    assert "Livrarea costă 20 lei" in ctx.brain_signals[0].text
    assert "[context faq_stage]" in llm.captured_user
    # brain-ul a acoperit AMBELE obligații într-un singur răspuns
    assert "Comanda ajunge prin curier" in ctx.reply.text
    assert "LumaDerm" in ctx.reply.text
    assert llm.tool_loop_calls == 1
    # căutarea a mers prin portul NX-238 (fals aici), nu direct prin tool
    assert port.calls == 1
    demote = [e for e in ctx.events if e.type == "control_plane_decision"]
    assert any(e.properties["path"] == "faq_stage" and not e.properties["complete"] for e in demote)


async def test_flag_on_pure_greeting_stays_fast_path(monkeypatch):
    _wire(monkeypatch, flag=True, port=_FakePort())
    llm = _ScriptedLLM(triage_out={"route": "simple"}, plan=_plan_dict())
    ctx = _ctx("Bună ziua!")
    await run_pipeline(ctx, PipelineDeps(conn=None, llm=llm), [greeting_stage, agent_stage])
    assert ctx.reply is not None and "Demo" in ctx.reply.text  # welcome-ul determinist
    assert llm.tool_loop_calls == 0  # brain-ul nici nu a rulat — $0 inferență


async def test_flag_on_simple_route_served_by_brain_not_nano(monkeypatch):
    port = _FakePort(products=(_PRODUCT,))
    _wire(monkeypatch, flag=True, port=port)
    plan = _plan_dict(
        obligations=[{"kind": "answer", "key": "question_0"}],
        selected_products=[],
        recommendations=[],
        direct_answer="Cu drag! Dacă mai ai nevoie de ceva pentru ten, scrie-mi oricând.",
    )
    llm = _ScriptedLLM(
        triage_out={"route": "simple", "reply": "Cu plăcere! 😊"}, plan=plan, search=False
    )
    ctx = _ctx("mulțumesc mult, super")
    await run_pipeline(
        ctx, PipelineDeps(conn=None, llm=llm), [triage_stage, agent_stage, fallback_stage]
    )
    # reply-ul nano a fost demote-uit (writer LLM concurent) → brain-ul a răspuns
    assert ctx.reply.text.startswith("Cu drag!")
    assert len(ctx.brain_signals) == 1
    assert ctx.brain_signals[0].kind == "triage_reply"


async def test_flag_on_not_ready_selects_current_live_via_real_selector(monkeypatch):
    """Selectorul NX-238 REAL (fără GO semnat, flag candidat OFF) alege current live; brain-ul
    funcționează normal — NO-GO search nu blochează cardul."""
    port = _FakePort(products=(_PRODUCT,))
    _wire(monkeypatch, flag=True, port=port)  # build_port fals; select_provider REAL
    llm = _ScriptedLLM(triage_out={"route": "sales"}, plan=_plan_dict())
    ctx = _ctx("vreau un ser pentru ten uscat")
    await run_pipeline(
        ctx, PipelineDeps(conn=None, llm=llm), [triage_stage, agent_stage, fallback_stage]
    )
    gate = next(e for e in ctx.events if e.type == "retrieval_gate")
    assert gate.properties["decision"] == "current_live.v1"
    assert gate.properties["blocking_code"] == "candidate_flag_off"
    assert ctx.reply is not None and "LumaDerm" in ctx.reply.text


async def test_flag_on_fallback_still_never_silent(monkeypatch):
    """Brain-ul pică total (plan + repair invalide) → reply determinist non-gol (P6)."""
    port = _FakePort(products=(_PRODUCT,))
    _wire(monkeypatch, flag=True, port=port)
    llm = _ScriptedLLM(
        triage_out={"route": "sales"},
        plan={"schema_version": 2},  # incomplet → invalid
    )
    ctx = _ctx("vreau un ser pentru ten uscat")
    await run_pipeline(
        ctx, PipelineDeps(conn=None, llm=llm), [triage_stage, agent_stage, fallback_stage]
    )
    assert ctx.reply is not None and ctx.reply.text.strip()
    assert ctx.reply.cacheable is False
