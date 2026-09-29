"""NX-348 — tipul spus de client ajunge în subiect; completarea subiectului e rafinare.

Recenzia NX-345a a găsit că un `set product_type` din interpretare («vreau un ser», `explicit`) era
respins MEREU de reducer (`topic_key`): delta îl trimitea ca `set_need`, dar tipul e a doua jumătate
a SUBIECTULUI (`Topic.product_type`). Acum delta trimite schimbările de subiect ale turului într-un
singur `set_topic` pe pereche, iar reducerul (kernel.v3.0, decis de Adi pe 2026-09-29) schimbă
subiectul doar când o jumătate DEJA SETATĂ primește altă valoare; completarea unei jumătăți goale
păstrează nevoile. Zero model, zero DB.
"""

from __future__ import annotations

from src.conversation.delta import TurnDelta, to_delta
from src.conversation.interpretation import (
    Act,
    CheckedChange,
    StateChange,
    TurnInterpretation,
)
from src.conversation.needs import NeedVocabulary
from src.conversation.state_reducer import ReducerPolicy, StateUpdateProposal, reduce_turn
from src.conversation.state_v2 import ConversationStateV2
from tests.kernel import fixture_catalog as fc

POLICY = ReducerPolicy(vocabulary=NeedVocabulary.from_pack(fc.pack("electronics")))


def _change(dimension, value, *, op="set", provenance="explicit", strength="soft"):
    change = StateChange(
        op=op,
        target=None,
        dimension=dimension,
        relation="eq",
        value=value,
        number=None,
        unit=None,
        relative_to=None,
        quote=value,
    )
    return CheckedChange(
        change=change,
        dimension=dimension,
        canonical_value=value,
        provenance=provenance,
        strength=strength,
        rejected=None,
    )


def _interp(n):
    return TurnInterpretation(
        thread="continue",
        acts=[Act(kind="find", targets=[], query=None)],
        changes=[],
        references=[],
        ambiguities=[],
        corrects_previous_turn=False,
    )


def step(state, *checked):
    """Un tur prin lanțul de producție: `to_delta` (pur) → `reduce_turn` (pur)."""
    delta = to_delta(
        _interp(len(checked)),
        list(checked),
        needs=POLICY.vocabulary,
        turn_id="t",
    )
    return reduce_turn(state, delta, (), (), None, False, POLICY), delta


def parked(result):
    return [a for a in result.applied if a.op == "park"]


def test_an_explicit_type_reaches_the_subject():
    """Reproducerea: pe `main`, `topic.product_type` rămânea `None` și propunerea era respinsă."""
    result, delta = step(ConversationStateV2(), _change("product_type", "smartphone"))
    assert result.state.topic.product_type == "smartphone"
    assert [p.op for p in delta.proposals] == ["set_topic"]
    assert not [r for r in result.rejected if r.reason == "topic_key"]


def test_shelf_and_type_in_one_turn_are_one_subject_proposal():
    result, delta = step(
        ConversationStateV2(),
        _change("category", "telefoane"),
        _change("product_type", "smartphone"),
    )
    assert [p.op for p in delta.proposals] == ["set_topic"]
    topic = result.state.topic
    assert (topic.category_key, topic.product_type) == ("telefoane", "smartphone")
    assert parked(result) == []


def test_the_first_type_on_a_shelf_refines_the_subject_and_keeps_its_needs():
    """Decizia lui Adi: completarea unei jumătăți goale e rafinare, nu schimbare de subiect."""
    state, _ = step(ConversationStateV2(), _change("category", "telefoane"))
    state, _ = step(state.state, _change("brand", "samsung"))
    refined, _ = step(state.state, _change("product_type", "smartphone"))
    topic = refined.state.topic
    assert (topic.category_key, topic.product_type) == ("telefoane", "smartphone")
    assert refined.state.need_for("brand") is not None
    assert refined.state.parked is None and parked(refined) == []


def test_the_first_shelf_on_a_typed_subject_refines_it_too():
    state, _ = step(ConversationStateV2(), _change("product_type", "smartphone"))
    state, _ = step(state.state, _change("brand", "samsung"))
    refined, _ = step(state.state, _change("category", "telefoane"))
    topic = refined.state.topic
    assert (topic.category_key, topic.product_type) == ("telefoane", "smartphone")
    assert refined.state.need_for("brand") is not None
    assert parked(refined) == []


def test_another_type_on_the_same_shelf_is_still_a_subject_change():
    state, _ = step(
        ConversationStateV2(),
        _change("category", "telefoane"),
        _change("product_type", "smartphone"),
    )
    changed, _ = step(state.state, _change("product_type", "accesoriu"))
    assert changed.state.topic.product_type == "accesoriu"
    assert changed.state.parked.topic.product_type == "smartphone"
    assert [a.outcome for a in parked(changed)] == ["parked"]


def test_another_shelf_parks_and_drops_the_old_type():
    state, _ = step(
        ConversationStateV2(),
        _change("category", "telefoane"),
        _change("product_type", "smartphone"),
    )
    changed, _ = step(state.state, _change("category", "laptopuri"))
    assert (changed.state.topic.category_key, changed.state.topic.product_type) == (
        "laptopuri",
        None,
    )
    assert changed.state.parked.topic.product_type == "smartphone"


def test_restating_the_same_type_changes_nothing():
    state, _ = step(ConversationStateV2(), _change("product_type", "smartphone"))
    again, _ = step(state.state, _change("product_type", "smartphone"))
    assert again.state.topic == state.state.topic
    assert parked(again) == []


def test_an_inferred_type_stays_a_ranking_signal():
    """I23: `inferred` nu se persistă, nici ca subiect."""
    result, delta = step(
        ConversationStateV2(), _change("product_type", "smartphone", provenance="inferred")
    )
    assert result.state.topic.product_type is None
    assert [(s.dimension, s.value) for s in delta.ranking] == [("product_type", "smartphone")]


def test_two_types_in_one_turn_keep_the_last_and_are_counted():
    result, delta = step(
        ConversationStateV2(),
        _change("product_type", "smartphone", op="add"),
        _change("product_type", "accesoriu", op="add"),
    )
    assert result.state.topic.product_type == "accesoriu"
    assert delta.counters.get("subject_multiple") == 1


def test_a_type_only_proposal_is_not_an_invalid_payload():
    """Pe `main`, un `set_topic` de interpretare fără raft era `invalid_payload`."""
    state = ConversationStateV2()
    proposal = StateUpdateProposal(
        "set_topic", product_type="smartphone", origin="interpretation", source="user_explicit"
    )
    result = reduce_turn(
        state, TurnDelta(thread="continue", proposals=(proposal,)), (), (), None, False, POLICY
    )
    assert result.state.topic.product_type == "smartphone"
    assert not result.rejected
