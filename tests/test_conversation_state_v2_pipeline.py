"""NX-235 — cablajul din pipeline, pe AMBELE poziții ale flagurilor.

„Flag OFF byte-identic" e jumătate din adevăr: cealaltă jumătate e ce se întâmplă când îl aprinzi.
Testele de aici rulează turul REAL (`handle_turn` cu DB stubbed, pattern G8-1) în cele trei trepte
de rollout — stins, shadow, scriere — și verifică exact ce promite cardul:

  • stins  → `new_state` identic cu cel de dinainte de card, plus zero evenimente noi;
  • shadow → v2 se calculează și se măsoară, dar autoritatea la scriere rămâne v1;
  • write  → se persistă DOAR documentul v2, iar cititorul v1 vede aceleași fapte prin proiecție.

ZERO OpenAI / DB real.
"""

import pytest

from src.config import get_settings
from src.conversation.state_reducer import StateUpdateProposal
from src.conversation.state_v2 import is_v2
from src.db.provider import static_db
from src.models import BusinessConfig, Contact, ConversationState, TurnContext
from src.worker import processor as proc
from src.worker import turn_uow as uow
from src.worker.context import context_blocks, facts_block, memory_block
from src.worker.processor import handle_turn
from src.worker.stages.clarify import clarification_gate, clarify_resume_stage


class _FakeTx:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *a):
        return False


class _FakeConn:
    def transaction(self):
        return _FakeTx()


@pytest.fixture
def flags(monkeypatch):
    """Flagurile NX-235, resetate pe fiecare test (settings e singleton)."""
    settings = get_settings()

    def _set(**kw):
        for key, value in kw.items():
            monkeypatch.setattr(settings, key, value)

    _set(
        conversation_state_v2_enabled=False,
        conversation_state_v2_write_enabled=False,
        clarification_policy_v2_enabled=False,
        reference_precedence_v2_enabled=False,
        conversation_sensitive_memory_enabled=False,
    )
    return _set


async def _run(monkeypatch, stage, *, initial_state, body="salut") -> tuple[dict, list]:
    """Un tur complet. Întoarce `(new_state, events)` — evenimentele contează la fel de mult ca
    scrierea: shadow-ul se JUDECĂ după ce a măsurat."""
    captured: dict = {}
    events: list = []

    async def fake_conv(*a, **k):
        return {
            "id": "conv",
            "state": initial_state,
            "state_version": 3,
            "locale": "ro",
            "bot_active": True,
        }

    async def fake_patch(conn, business_id, conv_id, new_state, version, **k):
        captured["new_state"] = new_state

    async def anoop(*a, **k):
        return None

    async def fake_contact(*a, **k):
        return Contact(id="c", business_id="biz-1")

    async def fake_claim(*a, **k):
        return True

    async def fake_persist(db, bid, conv, contact, batch):
        events.extend(batch)

    monkeypatch.setattr(uow, "claim_inbound", fake_claim)
    monkeypatch.setattr(uow, "mark_inbound_completed", anoop)
    monkeypatch.setattr(uow, "get_or_create_contact", fake_contact)
    monkeypatch.setattr(uow, "get_or_create_conversation", fake_conv)
    monkeypatch.setattr(uow, "insert_message", lambda *a, **k: _async("msg-id"))
    monkeypatch.setattr(uow, "touch_last_inbound", anoop)
    monkeypatch.setattr(uow, "get_recent_messages", anoop)
    monkeypatch.setattr(uow, "get_summary_for_context", anoop)
    monkeypatch.setattr(uow, "enqueue_outbox", lambda *a, **k: _async("outbox-1"))
    monkeypatch.setattr(uow, "patch_conversation_state", fake_patch)
    monkeypatch.setattr(proc, "persist_events", fake_persist)
    monkeypatch.setattr(proc, "_record_turn_cost", anoop)
    monkeypatch.setattr(proc, "_llm_within_budget", lambda *a, **k: _async(None))
    monkeypatch.setattr(proc, "run_aftercare", anoop)

    business = BusinessConfig(id="biz-1", slug="s", name="n")
    event = {
        "channel_kind": "telegram",
        "sender_external_id": "u1",
        "provider_msg_id": "m1",
        "content_type": "text",
        "body": body,
    }
    await handle_turn(static_db(_FakeConn()), business, "chan-1", event, stages=[stage])
    return captured["new_state"], events


async def _async(value):
    return value


async def _reply_stage(ctx, deps):
    ctx.set_reply("ok")


V1_STATE = {
    "search_constraints": {"budget_max": 150, "concerns": ["acnee"], "category_key": "seruri"},
    "constraints": {"budget_max": "150"},
    "cart": [{"product_id": "p1", "name": "A", "price": 9.0, "quantity": 1}],
    "safety": {"contexts": ["pregnancy"]},
}


def _types(events) -> set[str]:
    return {e.type for e in events}


# ── Treapta 0: flag stins ────────────────────────────────────────────────────


async def test_with_the_flag_off_nothing_changes(monkeypatch, flags):
    new_state, events = await _run(monkeypatch, _reply_stage, initial_state=V1_STATE)
    assert not is_v2(new_state)
    assert new_state["search_constraints"] == V1_STATE["search_constraints"]
    assert new_state["cart"] == V1_STATE["cart"]
    assert not _types(events) & {
        "conversation_state_loaded",
        "conversation_state_shadow_diff",
        "conversation_state_serialized",
    }


async def test_with_the_flag_off_the_turn_has_no_reduced_state(monkeypatch, flags):
    seen: dict = {}

    async def stage(ctx, deps):
        seen["state_v2"] = ctx.state_v2
        ctx.set_reply("ok")

    await _run(monkeypatch, stage, initial_state=V1_STATE)
    assert seen["state_v2"] is None


# ── Treapta 1: shadow (v2 se calculează, v1 rămâne autoritatea) ──────────────


async def test_shadow_measures_without_changing_what_is_written(monkeypatch, flags):
    flags(conversation_state_v2_enabled=True)
    new_state, events = await _run(monkeypatch, _reply_stage, initial_state=V1_STATE)
    assert not is_v2(new_state)  # v1 rămâne formatul persistat
    assert new_state["search_constraints"] == V1_STATE["search_constraints"]
    assert {"conversation_state_loaded", "conversation_state_shadow_diff"} <= _types(events)


async def test_shadow_hydrates_the_reduced_state_for_the_stages(monkeypatch, flags):
    flags(conversation_state_v2_enabled=True)
    seen: dict = {}

    async def stage(ctx, deps):
        seen["budget"] = ctx.state_v2.need_for("budget_max")
        seen["topic"] = ctx.state_v2.topic.category_key
        ctx.set_reply("ok")

    await _run(monkeypatch, stage, initial_state=V1_STATE)
    assert seen["budget"].normalized_value == 150.0
    assert seen["budget"].strength == "hard"
    assert seen["topic"] == "seruri"


# ── Treapta 2: scriere v2 ────────────────────────────────────────────────────


async def test_the_written_document_is_v2_and_still_readable_as_v1(monkeypatch, flags):
    flags(conversation_state_v2_enabled=True, conversation_state_v2_write_enabled=True)
    new_state, events = await _run(monkeypatch, _reply_stage, initial_state=V1_STATE)

    assert is_v2(new_state)
    legacy = ConversationState.from_jsonb(new_state)
    assert legacy.search_constraints["budget_max"] == 150.0
    assert legacy.search_constraints["category_key"] == "seruri"
    assert legacy.cart[0]["product_id"] == "p1"  # NX-237 owner — cărat neatins
    assert legacy.safety["contexts"] == ["pregnancy"]  # NX-173 owner — cărat neatins
    assert "conversation_state_lazy_upgraded" in _types(events)


async def test_a_second_turn_reads_the_v2_document_natively(monkeypatch, flags):
    flags(conversation_state_v2_enabled=True, conversation_state_v2_write_enabled=True)
    first, _ = await _run(monkeypatch, _reply_stage, initial_state=V1_STATE)
    second, events = await _run(monkeypatch, _reply_stage, initial_state=first)

    assert is_v2(second)
    loaded = next(e for e in events if e.type == "conversation_state_loaded")
    assert loaded.properties["outcome"] == "native"
    assert "conversation_state_lazy_upgraded" not in _types(events)


async def test_a_revocation_proposed_by_a_stage_survives_the_write(monkeypatch, flags):
    flags(conversation_state_v2_enabled=True, conversation_state_v2_write_enabled=True)

    async def stage(ctx, deps):
        ctx.state_proposals.append(
            StateUpdateProposal("revoke", key="budget_max", source="user_explicit")
        )
        ctx.set_reply("ok")

    new_state, events = await _run(monkeypatch, stage, initial_state=V1_STATE)
    assert ConversationState.from_jsonb(new_state).search_constraints.get("budget_max") is None
    assert "constraint_revoked" in _types(events)


async def test_a_model_proposal_to_relax_a_hard_need_is_rejected_and_measured(monkeypatch, flags):
    flags(conversation_state_v2_enabled=True, conversation_state_v2_write_enabled=True)

    async def stage(ctx, deps):
        ctx.state_proposals.append(
            StateUpdateProposal("set_need", key="budget_max", value=9999, source="model_inferred")
        )
        ctx.set_reply("ok")

    new_state, events = await _run(monkeypatch, stage, initial_state=V1_STATE)
    assert ConversationState.from_jsonb(new_state).search_constraints["budget_max"] == 150.0
    rejected = [e for e in events if e.type == "need_update_rejected"]
    assert rejected and rejected[0].properties["reason"] == "hard_downgrade"


async def test_the_displayed_set_lands_in_references_with_a_revision(monkeypatch, flags):
    flags(conversation_state_v2_enabled=True, conversation_state_v2_write_enabled=True)

    async def stage(ctx, deps):
        ctx.set_reply("uite")
        ctx.reply.products = [{"product_id": "p9", "name": "Ser Z", "price": 49.0}]

    new_state, _ = await _run(monkeypatch, stage, initial_state=V1_STATE)
    refs = new_state["references"]
    assert [d["product_id"] for d in refs["displayed_products"]] == ["p9"]
    assert refs["displayed_revision"] > 0
    assert ConversationState.from_jsonb(new_state).displayed_products[0].product_id == "p9"


async def test_the_serialization_event_reports_a_bucket_not_a_size(monkeypatch, flags):
    flags(conversation_state_v2_enabled=True, conversation_state_v2_write_enabled=True)
    _, events = await _run(monkeypatch, _reply_stage, initial_state=V1_STATE)
    event = next(e for e in events if e.type == "conversation_state_serialized")
    assert event.properties["state_size_bytes_bucket"] in {
        "0-1k",
        "1-2k",
        "2-4k",
        "4-6k",
        "over-budget",
    }


# ── Clarify: poarta + propunerile typed ─────────────────────────────────────


async def test_clarify_resume_still_fills_the_slot_with_the_flag_off(monkeypatch, flags):
    async def stage(ctx, deps):
        ctx.state.pending_question = {"field": "budget_max", "resume_route": "sales"}
        await clarify_resume_stage(ctx, deps)
        ctx.set_reply("ok")

    new_state, _ = await _run(monkeypatch, stage, initial_state={}, body="200 lei")
    assert new_state["constraints"]["budget_max"] == "200 lei"
    assert new_state["asked_intents"] == ["budget_max"]


async def test_clarify_resume_normalizes_the_answer_when_v2_is_on(monkeypatch, flags):
    flags(conversation_state_v2_enabled=True, conversation_state_v2_write_enabled=True)

    async def stage(ctx, deps):
        ctx.state.pending_question = {"field": "budget_max", "resume_route": "sales"}
        await clarify_resume_stage(ctx, deps)
        ctx.set_reply("ok")

    new_state, _ = await _run(monkeypatch, stage, initial_state={}, body="200 lei")
    legacy = ConversationState.from_jsonb(new_state)
    assert legacy.search_constraints["budget_max"] == 200.0  # canonic, nu „200 lei"
    assert legacy.asked_intents == ["budget_max"]


async def test_a_free_text_answer_does_not_become_a_stored_fact(monkeypatch, flags):
    flags(conversation_state_v2_enabled=True, conversation_state_v2_write_enabled=True)
    answer = "pentru sora mea Ana care are tenul mixt"

    async def stage(ctx, deps):
        ctx.state.pending_question = {"field": "budget_max", "resume_route": "sales"}
        await clarify_resume_stage(ctx, deps)
        ctx.set_reply("ok")

    new_state, _ = await _run(monkeypatch, stage, initial_state={}, body=answer)
    import json

    assert answer not in json.dumps(new_state, ensure_ascii=False)
    assert ConversationState.from_jsonb(new_state).search_constraints.get("budget_max") is None


def test_the_clarification_gate_always_asks_when_the_policy_is_off(flags):
    ctx = _ctx()
    assert clarification_gate(ctx, "budget_max").ask is True
    assert ctx.events == []  # zero telemetrie nouă cu flagul stins


def test_the_clarification_gate_refuses_a_question_we_already_know(monkeypatch, flags):
    flags(conversation_state_v2_enabled=True, clarification_policy_v2_enabled=True)
    from src.conversation.needs import NeedVocabulary
    from src.conversation.state_reducer import ReducerPolicy, reduce_all
    from src.conversation.state_v2 import ConversationStateV2

    ctx = _ctx()
    ctx.state_v2 = reduce_all(
        ConversationStateV2(),
        [StateUpdateProposal("set_need", key="budget_max", value=200, source="user_explicit")],
        ReducerPolicy(vocabulary=NeedVocabulary.from_pack(None)),
    ).state
    decision = clarification_gate(ctx, "budget_max")
    assert decision.ask is False and decision.reason == "already_known"
    assert any(e.type == "clarification_decision" for e in ctx.events)


# ── Proiecția în prompt ──────────────────────────────────────────────────────


def _ctx(**kw) -> TurnContext:
    from types import SimpleNamespace

    ctx = TurnContext(
        turn_id="t1",
        business=SimpleNamespace(id="b1", domain_pack=None, name="n", vertical="beauty"),
        contact=SimpleNamespace(id="c1", profile={}, lifecycle="new"),
        message=SimpleNamespace(body="salut", content_type="text"),
        conversation_id="conv",
        state=ConversationState(),
    )
    for key, value in kw.items():
        setattr(ctx, key, value)
    return ctx


def _reduced(*proposals):
    from src.conversation.needs import NeedVocabulary
    from src.conversation.state_reducer import ReducerPolicy, reduce_all
    from src.conversation.state_v2 import ConversationStateV2

    return reduce_all(
        ConversationStateV2(),
        list(proposals),
        ReducerPolicy(vocabulary=NeedVocabulary.from_pack(None)),
    ).state


def test_the_memory_block_is_empty_without_the_reduced_state():
    assert memory_block(_ctx()) == ""


def test_the_memory_block_marks_hard_needs_and_revocations():
    state = _reduced(
        StateUpdateProposal("set_need", key="budget_max", value=200, source="user_explicit"),
        StateUpdateProposal("set_need", key="brand", value="petala", source="user_explicit"),
        StateUpdateProposal("revoke", key="size", source="user_explicit"),
    )
    block = memory_block(_ctx(state_v2=state))
    assert "budget_max lte 200.0 (obligatoriu)" in block
    assert (
        "brand eq petala" in block and "(obligatoriu)" not in block.split("brand eq petala")[1][:20]
    )
    assert "Retrase de client" in block and "size" in block


def test_a_revoked_fact_is_not_re_injected_by_the_structured_memory():
    """Pasul 1 din Codex Attack Plan, pe drumul lateral: `facts_block`, nu istoricul."""
    state = _reduced(StateUpdateProposal("revoke", key="budget_max", source="user_explicit"))
    ctx = _ctx(state_v2=state, facts=[{"canonical_key": "budget_band", "fact_value": "sub 100"}])
    assert facts_block(ctx) == ""
    assert "sub 100" not in context_blocks(ctx)


def test_a_live_fact_is_still_injected():
    ctx = _ctx(state_v2=_reduced(), facts=[{"canonical_key": "fav_brands", "fact_value": "Petală"}])
    assert "Petală" in facts_block(ctx)
