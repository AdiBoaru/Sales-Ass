"""NX-239 — control plane: `FastPathDecision` + poarta de early-exit din runner.

Completitudinea e dovedită determinist (obligații extrase din cod). Cu flag OFF, runner-ul nu
cheamă deloc poarta → byte-identic (verificat aici pe `run_pipeline`)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from src.agent import control_plane
from src.models import BusinessConfig, Contact, InboundMessage, TurnContext
from src.worker import runner as rnr
from src.worker.runner import PipelineDeps, run_pipeline

_FIXTURES = json.loads(
    (Path(__file__).parent / "fixtures" / "single_brain" / "turn_types.json").read_text(
        encoding="utf-8"
    )
)


def _ctx(body: str = "salut") -> TurnContext:
    return TurnContext(
        turn_id="t1",
        business=BusinessConfig(id="b1", slug="demo", name="Demo"),
        contact=Contact(id="c1", business_id="b1"),
        message=InboundMessage(provider_msg_id="m1", body=body),
        conversation_id="conv1",
    )


# --- decide(): completitudine per stagiu, condusă de fixtures -------------------


def test_fixture_fast_path_completeness():
    for case in _FIXTURES["cases"]:
        if case.get("safety_active") or case.get("has_action"):
            continue  # semnalele de safety/action se verifică pe căile lor, mai jos
        ctx = _ctx(case["message"])
        for stage, expected in case.get("fast_path", {}).items():
            decision = control_plane.decide(ctx, stage)
            assert decision.complete is expected, (case["name"], stage, decision)


def test_greeting_complete_only_on_pure_greeting():
    assert control_plane.decide(_ctx("bună ziua"), "greeting_stage").complete
    mixed = control_plane.decide(_ctx("salut! aveți livrare în cluj?"), "greeting_stage")
    assert not mixed.complete
    assert mixed.reason == "mixed_intent"
    assert mixed.uncovered  # obligația de întrebare rămâne pe listă


def test_triage_never_finalizes_under_single_brain():
    decision = control_plane.decide(_ctx("mulțumesc frumos"), "triage_stage")
    assert not decision.complete
    assert decision.reason == "competing_llm_writer"


def test_gates_and_terminal_stages_always_finalize():
    for stage in ("gates_stage", "handoff_stage", "fallback_stage", "agent_stage"):
        assert control_plane.decide(_ctx("orice mesaj"), stage).complete


def test_faq_incomplete_on_safety_context():
    ctx = _ctx("cât costă livrarea?")
    ctx.state.safety = {"contexts": ["pregnancy"]}
    decision = control_plane.decide(ctx, "faq_stage")
    assert not decision.complete
    assert decision.reason in ("safety_context", "mixed_intent")


def test_decision_carries_version():
    decision = control_plane.decide(_ctx(), "faq_stage")
    assert decision.version == control_plane.CONTROL_PLANE_VERSION


# --- gate_early_exit(): demote → semnal + evenimente ----------------------------


def test_gate_demotes_incomplete_reply_to_signal():
    ctx = _ctx("cât costă livrarea? și vreau o cremă hidratantă")
    ctx.set_reply("Livrarea costă 20 lei.", cacheable=False)
    decision = control_plane.gate_early_exit(ctx, "faq_stage")
    assert not decision.complete
    assert ctx.reply is None  # reply-ul nu iese la client
    assert len(ctx.brain_signals) == 1
    assert ctx.brain_signals[0].stage == "faq_stage"
    assert "Livrarea" in ctx.brain_signals[0].text
    assert ctx.fast_path is decision
    events = {e.type for e in ctx.events}
    assert "control_plane_decision" in events
    assert "turn_obligations" in events


def test_gate_keeps_complete_reply():
    ctx = _ctx("cât costă livrarea?")
    ctx.set_reply("Livrarea costă 20 lei.")
    decision = control_plane.gate_early_exit(ctx, "faq_stage")
    assert decision.complete
    assert ctx.reply is not None
    assert ctx.brain_signals == []


def test_gate_drops_pending_question_proposal_on_demote():
    ctx = _ctx("un cadou și cât costă livrarea?")
    ctx.state_proposals.append(SimpleNamespace(op="set_pending_question", key="intent"))
    ctx.set_clarify("Pentru cine e cadoul?", field="intent", resume_route="sales")
    control_plane.gate_early_exit(ctx, "triage_stage")
    assert ctx.reply is None
    assert all(p.op != "set_pending_question" for p in ctx.state_proposals)


# --- runner: OFF = byte-identic; ON = mesajul mixt trece de FAQ -----------------


async def _faq_like_stage(ctx, deps):
    ctx.set_reply("Livrarea costă 20 lei.")


_faq_like_stage.__name__ = "faq_stage"


async def _brain_like_stage(ctx, deps):
    ctx.set_reply("Livrarea costă 20 lei. Iar pentru ten uscat recomand serul X.")


_brain_like_stage.__name__ = "agent_stage"


def _settings(single_brain: bool) -> SimpleNamespace:
    return SimpleNamespace(
        single_brain_enabled=single_brain,
        turn_budget_alerts_enabled=False,
        response_telemetry_enabled=False,
    )


async def test_runner_flag_off_first_reply_wins(monkeypatch):
    monkeypatch.setattr(rnr, "get_settings", lambda: _settings(False))
    ctx = _ctx("cât costă livrarea? și vreau o cremă hidratantă")
    await run_pipeline(ctx, PipelineDeps(conn=None), [_faq_like_stage, _brain_like_stage])
    assert ctx.reply.text == "Livrarea costă 20 lei."  # comportamentul de azi, neatins
    assert ctx.brain_signals == []


async def test_runner_flag_on_mixed_message_reaches_brain(monkeypatch):
    monkeypatch.setattr(rnr, "get_settings", lambda: _settings(True))
    ctx = _ctx("cât costă livrarea? și vreau o cremă hidratantă")
    await run_pipeline(ctx, PipelineDeps(conn=None), [_faq_like_stage, _brain_like_stage])
    assert "recomand" in ctx.reply.text  # brain-ul acoperă AMBELE obligații
    assert len(ctx.brain_signals) == 1  # răspunsul FAQ a devenit semnal, nu s-a pierdut


async def test_runner_flag_on_exact_faq_still_fast(monkeypatch):
    monkeypatch.setattr(rnr, "get_settings", lambda: _settings(True))
    ctx = _ctx("cât costă livrarea?")
    calls: list[str] = []

    async def _spy_brain(ctx, deps):
        calls.append("brain")

    _spy_brain.__name__ = "agent_stage"
    await run_pipeline(ctx, PipelineDeps(conn=None), [_faq_like_stage, _spy_brain])
    assert ctx.reply.text == "Livrarea costă 20 lei."
    assert calls == []  # fast path exact: brain-ul nici nu rulează


async def test_runner_flag_on_halt_untouched(monkeypatch):
    monkeypatch.setattr(rnr, "get_settings", lambda: _settings(True))
    ctx = _ctx("orice")

    async def _gate(ctx, deps):
        ctx.halt_silent("handoff_active")

    _gate.__name__ = "gates_stage"
    ran: list[str] = []

    async def _after(ctx, deps):
        ran.append("after")

    await run_pipeline(ctx, PipelineDeps(conn=None), [_gate, _after])
    assert ctx.halt and ctx.reply is None and ran == []
