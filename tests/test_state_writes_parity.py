"""NX-331 felia 3b-3 — paritatea vederii v1 pe siturile mutate pe propuneri, cu flagurile stinse.

Fiecare sit scria starea v1 direct, lângă propunerea v2. Acum propunerea e sursa, iar
`worker/state_writes.apply_v1_view` derivă scrierea v1 din ea. Invarianta (I16, partea de stare):
`ctx.state` după sit e IDENTIC cu ce producea codul de pe `main` înainte de card. Referința de mai
jos e acel cod, copiat verbatim (`_legacy_*`), rulat pe o copie a aceluiași context.
"""

from __future__ import annotations

import copy

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from src.agent import action_kernel as kernel
from src.conversation.state_reducer import StateUpdateProposal
from src.conversation.state_v2 import ConversationStateV2
from src.models import BusinessConfig, Contact, InboundMessage, Route, RouteDecision, TurnContext
from src.web.action_models import ActionArgs, ActionCommand
from src.worker.canonicalize import canonicalize_clarify_field
from src.worker.runner import PipelineDeps
from src.worker.stages.clarify import clarify_resume_stage
from src.worker.state_writes import MAX_ASKED_INTENTS, apply_v1_view

_KEYS = st.sampled_from(("budget_max", "skin_type", "brand", "concerns", "size", "k1", "k2"))
_ASKED = st.lists(_KEYS, max_size=10)
_CONSTRAINTS = st.dictionaries(_KEYS, st.sampled_from(("a", "200 lei", "m")), max_size=4)


def _ctx(body: str = "200 lei") -> TurnContext:
    ctx = TurnContext(
        turn_id="t",
        business=BusinessConfig(id="biz-1", slug="s", name="n"),
        contact=Contact(id="c", business_id="biz-1"),
        message=InboundMessage(provider_msg_id="m", body=body),
        conversation_id="conv",
    )
    ctx.route = RouteDecision(route=Route.SALES)
    return ctx


def _seed(ctx: TurnContext, asked: list[str], constraints: dict[str, str], field: str) -> None:
    ctx.state.asked_intents = list(asked)
    ctx.state.constraints = dict(constraints)
    ctx.state.pending_question = {"field": field, "resume_route": "sales", "attempts": 1}


# --- situl 1: kernelul de acțiuni (`_handle_answer_clarification`) --------------------------------


def _legacy_answer(ctx: TurnContext, field: str, answer: str) -> None:
    """Scrierea v1 de pe `main` înainte de NX-331, verbatim."""
    ctx.state.constraints[field] = answer
    if field not in ctx.state.asked_intents:
        ctx.state.asked_intents.append(field)
        ctx.state.asked_intents[:] = ctx.state.asked_intents[-8:]
    ctx.state.pending_question = None


def _answer(option_text: str) -> ActionCommand:
    return ActionCommand(
        action_id="a" * 32,
        kind="answer_clarification",
        args=ActionArgs(question_id="q:budget_max:1", option_ref=0),
        policy="one_shot",
        source_turn_id="src",
        source_revision=1,
        conversation_id="conv",
        option_text=option_text,
    )


@settings(max_examples=150, deadline=None)
@given(asked=_ASKED, constraints=_CONSTRAINTS, answer=st.sampled_from(("200 lei", "m", "")))
def test_action_kernel_answer_writes_the_same_v1_state(asked, constraints, answer):
    ctx = _ctx()
    _seed(ctx, asked, constraints, "budget_max")
    legacy = copy.deepcopy(ctx)
    kernel._handle_answer_clarification(ctx, _answer(answer))
    if answer:  # opțiune goală ⇒ respinsă ca stale, înainte de orice scriere (ambele versiuni)
        _legacy_answer(legacy, "budget_max", answer)
    assert ctx.state == legacy.state


# --- situl 2: reluarea clarificării pe text (`clarify_resume_stage`) ---------------------------


def _legacy_resume(ctx: TurnContext) -> None:
    """Scrierea v1 de pe `main` înainte de NX-331, verbatim (fără rutare și evenimente)."""
    pq = ctx.state.pending_question
    answer = (ctx.message.body or "").strip()
    field = canonicalize_clarify_field(pq.get("field"), ctx.business.domain_pack)
    ctx.state.constraints[field] = answer
    if field not in ctx.state.asked_intents:
        ctx.state.asked_intents.append(field)
        ctx.state.asked_intents[:] = ctx.state.asked_intents[-8:]


@settings(max_examples=150, deadline=None)
@given(
    asked=_ASKED,
    constraints=_CONSTRAINTS,
    field=st.sampled_from(("budget_max", "budget", "skin_type", "intent")),
)
@pytest.mark.asyncio
async def test_clarify_resume_writes_the_same_v1_state(asked, constraints, field):
    ctx = _ctx(body="  pe la 200  ")
    _seed(ctx, asked, constraints, field)
    legacy = copy.deepcopy(ctx)
    await clarify_resume_stage(ctx, PipelineDeps(conn=object(), redis=None, llm=None))
    _legacy_resume(legacy)
    assert ctx.state == legacy.state
    # Întrebarea rămâne în așteptare în tur: `set_clarify` îi citește `attempts`.
    assert ctx.state.pending_question is not None
    # Fără stare v2 (flagul stins) nu pleacă nicio propunere, exact ca înainte.
    assert ctx.state_proposals == []


# --- situl 3: întrebarea de îngustare (`finalize._apply_turn_shape`) ---------------------------


def _legacy_note(ctx: TurnContext, facet: str) -> None:
    asked = [k for k in (ctx.state.asked_intents or []) if k != facet]
    ctx.state.asked_intents[:] = [*asked, facet][-8:]


@settings(max_examples=200, deadline=None)
@given(asked=_ASKED, facet=_KEYS)
def test_note_asked_writes_the_same_v1_state_as_the_narrowing_question(asked, facet):
    ctx = _ctx()
    ctx.state.asked_intents = list(asked)
    legacy = copy.deepcopy(ctx)
    apply_v1_view(ctx, StateUpdateProposal("note_asked", key=facet))
    _legacy_note(legacy, facet)
    assert ctx.state == legacy.state


def test_the_narrowing_question_site_emits_the_proposal_and_the_v1_view():
    """`_apply_turn_shape` cu o întrebare acceptată: propunerea `note_asked` pleacă spre reducer,
    iar `asked_intents` primește fațeta la coadă (forma de pe `main`)."""
    from types import SimpleNamespace

    from src.agent.finalize import _apply_turn_shape, _TurnShape
    from src.models import RichReply

    ctx = _ctx()
    ctx.state.asked_intents = ["skin_type", "brand"]
    shape = _TurnShape(
        offer=SimpleNamespace(facet="skin_type", values=("dry", "oily", "sensitive"), gain=0.5),
        offer_phrases=("ten uscat", "ten gras", "ten sensibil"),
    )

    def rich() -> RichReply:
        return RichReply(
            intro="Am găsit câteva creme.",
            items=[],
            pick=None,
            education=None,
            chips=[],
            disclaimer="",
        )

    question = {"question": "Ai tenul uscat, gras sau sensibil?"}
    _apply_turn_shape(ctx, rich(), question, shape)
    assert ctx.state.asked_intents == ["brand", "skin_type"]
    # Fără stare v2 (flagul stins) nu pleacă nicio propunere, exact ca pe `main`.
    assert ctx.state_proposals == []
    ctx.state_v2 = ConversationStateV2()
    _apply_turn_shape(ctx, rich(), question, shape)
    assert [(p.op, p.key) for p in ctx.state_proposals] == [("note_asked", "skin_type")]


def test_the_v1_view_cap_is_the_one_every_site_used():
    assert MAX_ASKED_INTENTS == 8
