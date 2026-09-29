"""NX-348 — tipul spus de client ajunge în subiect; completarea subiectului e rafinare.

Recenzia NX-345a a găsit că un `set product_type` din interpretare («vreau un ser», `explicit`) era
respins MEREU de reducer (`topic_key`): delta îl trimitea ca `set_need`, dar tipul e a doua jumătate
a SUBIECTULUI (`Topic.product_type`). Acum delta trimite schimbările de subiect ale turului într-un
singur `set_topic` pe pereche, iar reducerul (kernel.v3.0, decis de Adi pe 2026-09-29) schimbă
subiectul doar când o jumătate DEJA SETATĂ primește altă valoare; completarea unei jumătăți goale
păstrează nevoile. Zero model, zero DB.
"""

from __future__ import annotations

from dataclasses import replace

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


def step(state, *checked, compatible=None, corrects=False):
    """Un tur prin lanțul de producție: `to_delta` (pur) → [compatibilitatea perechii, pe care o
    scrie orchestratorul din catalog] → `reduce_turn` (pur)."""
    delta = to_delta(
        _interp(len(checked)),
        list(checked),
        needs=POLICY.vocabulary,
        turn_id="t",
    )
    if compatible is not None:
        delta = replace(
            delta,
            proposals=tuple(
                replace(p, pair_compatible=compatible) if p.op == "set_topic" else p
                for p in delta.proposals
            ),
        )
    return reduce_turn(state, delta, (), (), None, corrects, POLICY), delta


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
    refined, _ = step(state.state, _change("product_type", "smartphone"), compatible=True)
    topic = refined.state.topic
    assert (topic.category_key, topic.product_type) == ("telefoane", "smartphone")
    assert refined.state.need_for("brand") is not None
    assert refined.state.parked is None and parked(refined) == []
    assert [a.outcome for a in refined.applied if a.op == "set_topic"] == ["refined"]


def test_the_first_shelf_on_a_typed_subject_refines_it_too():
    state, _ = step(ConversationStateV2(), _change("product_type", "smartphone"))
    state, _ = step(state.state, _change("brand", "samsung"))
    refined, _ = step(state.state, _change("category", "telefoane"), compatible=True)
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


# --- recenzia adversarială NX-348 ----------------------------------------------------------------


def test_filling_a_half_without_proof_of_the_pair_is_a_subject_change():
    """Constatarea 2: «telefoane» + «un laptop» nu e rafinare. Fără dovadă (incompatibil sau fără
    date) completarea e subiect nou: se parchează, iar cealaltă jumătate se golește."""
    for compatible in (False, None):
        state, _ = step(ConversationStateV2(), _change("category", "telefoane"))
        state, _ = step(state.state, _change("brand", "samsung"))
        moved, _ = step(state.state, _change("product_type", "laptop"), compatible=compatible)
        topic = moved.state.topic
        assert (topic.category_key, topic.product_type) == (None, "laptop")
        assert moved.state.parked.topic.category_key == "telefoane"
        assert ("brand", "samsung") in {
            (n.key, n.normalized_value) for n in moved.state.parked.needs
        }
        assert moved.state.need_for("brand") is None


def test_a_shelf_incompatible_with_the_type_drops_the_type():
    state, _ = step(ConversationStateV2(), _change("product_type", "smartphone"))
    moved, _ = step(state.state, _change("category", "laptopuri"), compatible=False)
    assert (moved.state.topic.category_key, moved.state.topic.product_type) == ("laptopuri", None)
    assert moved.state.parked.topic.product_type == "smartphone"


def test_needs_of_a_shelfless_subject_are_parked_not_leaked():
    """Constatarea 1: o nevoie spusă pe un subiect fără raft pleacă (parcată) cu subiectul, nu
    rămâne activă pe produsul următor."""
    state, _ = step(ConversationStateV2(), _change("product_type", "smartphone"))
    state, _ = step(state.state, _change("brand", "apple"))
    moved, _ = step(state.state, _change("product_type", "accesoriu"))
    assert moved.state.need_for("brand") is None
    assert ("brand", "apple") in {(n.key, n.normalized_value) for n in moved.state.parked.needs}


def test_a_refinement_gives_the_first_shelf_to_the_shelfless_needs():
    state, _ = step(ConversationStateV2(), _change("product_type", "smartphone"))
    state, _ = step(state.state, _change("brand", "samsung"))
    refined, _ = step(state.state, _change("category", "telefoane"), compatible=True)
    moved, _ = step(refined.state, _change("category", "laptopuri"))
    assert moved.state.need_for("brand") is None
    assert moved.state.parked.topic.category_key == "telefoane"
    assert ("brand", "samsung") in {(n.key, n.normalized_value) for n in moved.state.parked.needs}


def test_a_type_learned_from_the_shown_set_counts_as_empty():
    """Constatarea 3: tipul DEDUS de cod din setul arătat (NX-314) nu l-a spus clientul, deci primul
    tip spus de client îl completează (cu dovadă), nu schimbă subiectul."""
    from src.conversation.state_v2 import Topic

    learned = ConversationStateV2(
        revision=1,
        topic=Topic(category_key="telefoane", product_type="smartphone", type_learned=True),
    )
    state, _ = step(learned, _change("brand", "samsung"))
    refined, _ = step(state.state, _change("product_type", "accesoriu"), compatible=True)
    assert refined.state.topic.product_type == "accesoriu"
    assert not refined.state.topic.type_learned
    assert refined.state.need_for("brand") is not None and parked(refined) == []


def test_type_learned_survives_the_jsonb_round_trip_and_is_absent_when_false():
    from src.conversation.state_v2 import Topic

    assert "type_learned" not in Topic(product_type="x").to_jsonb()
    doc = Topic(product_type="x", type_learned=True).to_jsonb()
    assert Topic.from_jsonb(doc).type_learned is True


def test_a_correction_refining_the_type_is_not_a_subject_contradiction():
    """Constatarea 4: pe o propunere doar cu tip, raftul efectiv e cel curent."""
    state, _ = step(ConversationStateV2(), _change("category", "telefoane"))
    refined, _ = step(
        state.state, _change("product_type", "smartphone"), compatible=True, corrects=True
    )
    assert not [a for a in refined.applied if a.op == "correction" and a.outcome == "applied"]


def test_several_values_for_one_half_keep_only_the_last_subject_change():
    """Constatarea 7: [tip smartphone, raft telefoane, raft laptopuri] nu mai dă perechea amestecată
    (laptopuri, smartphone): rămâne doar ultima schimbare de subiect."""
    result, delta = step(
        ConversationStateV2(),
        _change("product_type", "smartphone"),
        _change("category", "telefoane"),
        _change("category", "laptopuri"),
    )
    topic = result.state.topic
    assert (topic.category_key, topic.product_type) == ("laptopuri", None)
    assert delta.counters.get("subject_multiple") == 1


def test_the_subject_proposal_takes_the_explicit_source():
    _, delta = step(
        ConversationStateV2(),
        _change("category", "telefoane", provenance="implicit"),
        _change("product_type", "smartphone"),
    )
    assert delta.proposals[0].source == "user_explicit"


def test_pair_exists_reads_the_subtree():
    from types import SimpleNamespace

    from src.catalog.subject_pairs import pair_exists
    from src.catalog.vocabulary import VocabEntry

    vocab = SimpleNamespace(
        categories=(
            VocabEntry(key="ten", label="Ten", count=3, path="ten"),
            VocabEntry(key="ten-creme", label="Creme", count=2, path="ten/creme"),
            VocabEntry(key="par", label="Par", count=1, path="par"),
        )
    )
    rows = [("ten/creme", "crema de fata"), ("par", "sampon")]
    assert pair_exists("ten", "crema de fata", rows, vocab)
    assert pair_exists("ten-creme", "crema de fata", rows, vocab)
    assert not pair_exists("par", "crema de fata", rows, vocab)
    assert not pair_exists("necunoscut", "sampon", rows, vocab)


def test_the_pairs_query_is_tenant_scoped():
    from src.db.queries import catalog

    sql = " ".join(catalog._SUBJECT_TYPE_PAIRS_SQL.split())
    assert "p.business_id = $1" in sql and "c.business_id = p.business_id" in sql
