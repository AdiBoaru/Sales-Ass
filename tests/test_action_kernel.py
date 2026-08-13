"""NX-236 — kernelul: dispatch typed, refuzuri oneste, zero fabricare de text de client.

Ce demonstrează fișierul:
  • o acțiune pe un produs NUMIT nu trece prin deixis și nu poate alege alt produs, oricât s-ar
    reordona lista afișată între emitere și click;
  • paginarea și răspunsul la clarificare sunt legate de sesiune / de întrebarea exactă — dacă
    lumea s-a schimbat, rezultatul e un refuz RANDABIL, nu o alegere greșită și nu tăcere;
  • comerțul (mutant) e refuzat până la receipt-ul NX-237, fără nicio scriere;
  • un `kind` necunoscut nu ajunge niciodată la un tool.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.agent import action_kernel as kernel
from src.agent.tool_definitions import TOOL_NAMES
from src.models import (
    BusinessConfig,
    Contact,
    InboundMessage,
    ProductRef,
    RetrievalResult,
    Route,
    RouteDecision,
    TurnContext,
)
from src.web.action_models import (
    KIND_REGISTRY,
    ActionArgs,
    ActionCommand,
    Continue,
    Handled,
    Rejected,
)

PID_A = "11111111-1111-4111-8111-111111111111"
PID_B = "22222222-2222-4222-8222-222222222222"


@pytest.fixture
def ctx_factory():
    """Un `TurnContext` minimal de tur web, ca în `test_review_intent` (fără DB, fără LLM)."""

    def _make() -> TurnContext:
        ctx = TurnContext(
            turn_id="turn-action",
            business=BusinessConfig(id="biz-1", slug="demo", name="Demo"),
            contact=Contact(id="contact-1", business_id="biz-1"),
            message=InboundMessage(provider_msg_id="msg-1", body=""),
            conversation_id="conv-1",
        )
        ctx.route = RouteDecision(route=Route.SALES)
        return ctx

    return _make


@pytest.fixture
def settings_on(monkeypatch):
    """Kill-switch APRINS (fără a atinge configurația reală a procesului)."""
    monkeypatch.setattr(kernel, "get_settings", lambda: SimpleNamespace(web_actions_enabled=True))


@pytest.fixture
def settings_off(monkeypatch):
    monkeypatch.setattr(kernel, "get_settings", lambda: SimpleNamespace(web_actions_enabled=False))


def _command(kind: str, **args) -> ActionCommand:
    return ActionCommand(
        action_id="a" * 32,
        kind=kind,
        args=ActionArgs(**args),
        policy="one_shot",
        source_turn_id="src-turn",
        source_revision=3,
        conversation_id="conv-1",
        option_text=args.pop("option_text", None) if "option_text" in args else None,
    )


class _Deps:
    """`PipelineDeps` minimal: kernelul nu atinge DB-ul direct, handlerele sunt monkeypatch-uite."""


# ── Dispatch pe produse ─────────────────────────────────────────────────────────────────────
async def test_request_reviews_serves_the_named_product(ctx_factory, monkeypatch):
    served: list[str] = []

    async def _reviews(ctx, deps, product_id, name=""):
        served.append(product_id)
        ctx.set_reply("recenzii")

    monkeypatch.setattr(kernel, "serve_reviews", _reviews)
    ctx = ctx_factory()
    outcome = await kernel.dispatch(ctx, _Deps(), _command("request_reviews", product_ref=PID_A))
    assert isinstance(outcome, Handled)
    assert served == [PID_A]


async def test_reordering_the_displayed_list_cannot_change_the_target(ctx_factory, monkeypatch):
    """Invariantul din failure matrix: acțiunea NUMEȘTE produsul, deci ordinea nu contează."""
    served: list[str] = []

    async def _details(ctx, deps, product_id):
        served.append(product_id)
        ctx.set_reply("detalii")

    monkeypatch.setattr(kernel, "serve_details", _details)
    ctx = ctx_factory()
    ctx.state.displayed_products = [
        ProductRef(product_id=PID_B, name="B", price=10.0),
        ProductRef(product_id=PID_A, name="A", price=20.0),
    ]
    await kernel.dispatch(ctx, _Deps(), _command("request_details", product_ref=PID_A))
    assert served == [PID_A]


async def test_product_action_records_an_explicit_selection(ctx_factory, monkeypatch):
    async def _details(ctx, deps, product_id):
        ctx.retrieval = RetrievalResult(products=[{"id": product_id}], source="detail_intent")
        ctx.set_reply("detalii")

    monkeypatch.setattr(kernel, "serve_details", _details)
    ctx = ctx_factory()
    await kernel.dispatch(ctx, _Deps(), _command("select_product", product_ref=PID_A))
    proposals = [p for p in ctx.state_proposals if p.op == "set_references"]
    assert proposals and proposals[0].source == "action"
    assert proposals[0].payload["selected_product"] == PID_A


async def test_selection_is_not_recorded_when_the_product_is_gated(ctx_factory, monkeypatch):
    """Safety gate / produs dispărut: nu ținem minte ca „ales" ce tocmai am refuzat să arătăm."""

    async def _details(ctx, deps, product_id):
        ctx.set_reply("nu mai e disponibil")  # fără `ctx.retrieval` — nimic n-a fost servit

    monkeypatch.setattr(kernel, "serve_details", _details)
    ctx = ctx_factory()
    await kernel.dispatch(ctx, _Deps(), _command("select_product", product_ref=PID_A))
    assert ctx.state_proposals == []


async def test_compare_refuses_honestly_when_the_table_cannot_be_built(ctx_factory, monkeypatch):
    async def _compare(ctx, deps, ids):
        return False

    monkeypatch.setattr(kernel, "serve_comparison", _compare)
    ctx = ctx_factory()
    outcome = await kernel.dispatch(
        ctx, _Deps(), _command("compare_selection", product_refs=(PID_A, PID_B))
    )
    assert isinstance(outcome, Rejected) and outcome.code == "action_stale"
    assert ctx.reply is not None and ctx.reply.text  # P6: refuzul e un mesaj, nu tăcere
    assert ctx.reply.cacheable is False


async def test_compare_records_what_was_compared(ctx_factory, monkeypatch):
    async def _compare(ctx, deps, ids):
        ctx.set_reply("tabel")
        return True

    monkeypatch.setattr(kernel, "serve_comparison", _compare)
    ctx = ctx_factory()
    outcome = await kernel.dispatch(
        ctx, _Deps(), _command("compare_selection", product_refs=(PID_A, PID_B))
    )
    assert isinstance(outcome, Handled)
    payloads = [p.payload for p in ctx.state_proposals if p.op == "set_references"]
    assert payloads[-1]["compared_products"] == [PID_A, PID_B]


# ── Paginare ────────────────────────────────────────────────────────────────────────────────
def test_show_more_continues_when_the_session_matches(ctx_factory):
    ctx = ctx_factory()
    ctx.state.active_search = {"fp": "fp-1", "pool": [PID_A], "cursor": 0, "page": 0, "filters": {}}
    outcome = kernel._handle_show_more(ctx, _command("show_more", session_ref="fp-1"))
    assert isinstance(outcome, Continue)
    assert ctx.route is not None and ctx.route.route == Route.SALES
    assert ctx.reply is None  # creierul/paginarea continuă turul


def test_show_more_on_a_replaced_session_is_stale(ctx_factory):
    ctx = ctx_factory()
    ctx.state.active_search = {"fp": "fp-2", "pool": [PID_A], "cursor": 0, "page": 0, "filters": {}}
    outcome = kernel._handle_show_more(ctx, _command("show_more", session_ref="fp-1"))
    assert isinstance(outcome, Rejected) and outcome.code == "action_stale"
    assert ctx.reply is not None and ctx.reply.text


def test_show_more_without_a_session_is_stale(ctx_factory):
    ctx = ctx_factory()
    ctx.state.active_search = None
    outcome = kernel._handle_show_more(ctx, _command("show_more", session_ref="fp-1"))
    assert isinstance(outcome, Rejected) and outcome.code == "action_stale"


# ── Clarificare ─────────────────────────────────────────────────────────────────────────────
def _pending(field="budget_max", attempts=1):
    return {"field": field, "resume_route": "sales", "attempts": attempts}


def _answer(option_text="200 lei", question_id="q:budget_max:1"):
    return ActionCommand(
        action_id="a" * 32,
        kind="answer_clarification",
        args=ActionArgs(question_id=question_id, option_ref=0),
        policy="one_shot",
        source_turn_id="src",
        source_revision=1,
        conversation_id="conv",
        option_text=option_text,
    )


def test_answer_fills_the_slot_and_closes_the_question(ctx_factory):
    ctx = ctx_factory()
    ctx.state.pending_question = _pending()
    outcome = kernel._handle_answer_clarification(ctx, _answer())
    assert isinstance(outcome, Continue)
    assert ctx.state.constraints["budget_max"] == "200 lei"
    assert ctx.state.pending_question is None  # clarify_resume nu o mai consumă a doua oară
    ops = [(p.op, p.source) for p in ctx.state_proposals]
    assert ("resolve_question", "action") in ops
    assert ("set_need", "action") in ops
    assert ctx.route.route == Route.SALES


def test_answer_to_a_different_question_does_not_touch_state(ctx_factory):
    """Cerința explicită a cardului: alegerea stale NU modifică starea."""
    ctx = ctx_factory()
    ctx.state.pending_question = _pending(field="skin_type")
    outcome = kernel._handle_answer_clarification(ctx, _answer())
    assert isinstance(outcome, Rejected) and outcome.code == "action_stale"
    assert ctx.state.constraints == {}
    assert ctx.state_proposals == []
    assert ctx.state.pending_question == _pending(field="skin_type")


def test_answer_after_a_re_ask_is_stale(ctx_factory):
    """A doua încercare pe același slot e ALTĂ întrebare (alt `question_id`)."""
    ctx = ctx_factory()
    ctx.state.pending_question = _pending(attempts=2)
    outcome = kernel._handle_answer_clarification(ctx, _answer(question_id="q:budget_max:1"))
    assert isinstance(outcome, Rejected) and outcome.code == "action_stale"
    assert ctx.state.constraints == {}


def test_answer_without_a_pending_question_is_stale(ctx_factory):
    ctx = ctx_factory()
    ctx.state.pending_question = None
    outcome = kernel._handle_answer_clarification(ctx, _answer())
    assert isinstance(outcome, Rejected) and outcome.code == "action_stale"


def test_answer_with_a_vanished_option_is_stale(ctx_factory):
    """Lista de opțiuni s-a scurtat între emitere și click (`option_text is None`)."""
    ctx = ctx_factory()
    ctx.state.pending_question = _pending()
    outcome = kernel._handle_answer_clarification(ctx, _answer(option_text=None))
    assert isinstance(outcome, Rejected) and outcome.code == "action_stale"
    assert ctx.state.constraints == {}


# ── Comerț + kind-uri fără handler ──────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "kind", [k for k, spec in KIND_REGISTRY.items() if spec.mutating or not spec.available]
)
async def test_kinds_without_a_safe_handler_are_refused_without_side_effects(ctx_factory, kind):
    ctx = ctx_factory()
    outcome = await kernel.dispatch(ctx, _Deps(), _command(kind, product_ref=PID_A))
    assert isinstance(outcome, Rejected) and outcome.code == "action_unavailable"
    assert ctx.state_proposals == []
    assert ctx.state_patch == {}
    assert ctx.reply is not None and ctx.reply.text  # refuz ONEST, nu confirmare


async def test_unknown_kind_never_reaches_a_tool(ctx_factory):
    ctx = ctx_factory()
    outcome = await kernel.dispatch(ctx, _Deps(), _command("execute_tool", product_ref=PID_A))
    assert isinstance(outcome, Rejected) and outcome.code == "action_unavailable"


def test_action_registry_cannot_name_a_tool():
    assert not (set(KIND_REGISTRY) & set(TOOL_NAMES))


# ── Stagiul ─────────────────────────────────────────────────────────────────────────────────
async def test_stage_is_a_noop_without_an_action(ctx_factory):
    ctx = ctx_factory()
    await kernel.action_kernel_stage(ctx, _Deps())
    assert ctx.reply is None and ctx.events == []


async def test_stage_refuses_honestly_after_a_rollback(ctx_factory, monkeypatch, settings_off):
    """Turul poartă o comandă acceptată cât timp flagul era APRINS, iar între timp s-a stins.

    A-l lăsa să curgă ar însemna un turn de text cu mesaj gol — adică tăcere pe o cale terminală.
    Refuzul e vizibil în metrici și randabil pentru client (P6)."""
    ctx = ctx_factory()
    ctx.action = _command("request_details", product_ref=PID_A)
    await kernel.action_kernel_stage(ctx, _Deps())
    assert ctx.reply is not None and ctx.reply.text
    assert {e.type for e in ctx.events} >= {"web_action_consumed"}
    assert ctx.state_proposals == []


async def test_stage_emits_low_cardinality_telemetry(ctx_factory, monkeypatch, settings_on):
    async def _details(ctx, deps, product_id):
        ctx.set_reply("detalii")

    monkeypatch.setattr(kernel, "serve_details", _details)
    ctx = ctx_factory()
    ctx.action = _command("request_details", product_ref=PID_A)
    await kernel.action_kernel_stage(ctx, _Deps())
    kinds = {e.type: e.properties for e in ctx.events}
    assert kinds["web_action_consumed"]["kind"] == "request_details"
    assert kinds["web_action_consumed"]["outcome"] == "handled"
    # P12: niciun `action_id`, niciun produs, niciun token în etichete.
    for event in ctx.events:
        flat = str(event.properties)
        assert "a" * 32 not in flat
        assert PID_A not in flat


async def test_stage_never_leaves_the_turn_silent_when_a_handler_crashes(
    ctx_factory, monkeypatch, settings_on
):
    async def _boom(ctx, deps, product_id):
        raise RuntimeError("handler stricat")

    monkeypatch.setattr(kernel, "serve_details", _boom)
    ctx = ctx_factory()
    ctx.action = _command("request_details", product_ref=PID_A)
    await kernel.action_kernel_stage(ctx, _Deps())
    assert ctx.reply is not None and ctx.reply.text  # P6
