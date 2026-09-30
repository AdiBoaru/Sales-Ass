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

from src.catalog.subject_pairs import proposal_pair
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


def _change(dimension, value, *, op="set", provenance="explicit", strength="soft", umbrella=()):
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
        umbrella=umbrella,
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
    if compatible:
        # Perechea VERIFICATĂ e cea pe care o calculează orchestratorul (`proposal_pair`).
        def _pair(p):
            # NX-352: o umbrelă își verifică primul cod pe raft (ca `mark_pairs`)
            if p.type_umbrella and not p.product_type:
                return (p.category_key or state.topic.category_key, p.type_umbrella[0])
            return proposal_pair(p, state, delta.thread)

        delta = replace(
            delta,
            proposals=tuple(replace(p, pair_verified=_pair(p)) for p in delta.proposals),
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


# --- recenzia adversarială NX-348, a doua trecere ---------------------------------------------


def test_a_v1_subject_type_is_learned_after_the_v2_adapter():
    """N-A: tipul subiectului v1 îl scrie doar `_learn_subject`, deci e DEDUS."""
    from src.conversation.state_v2 import adapt_v1

    doc = {
        "search_constraints": {
            "category_key": "telefoane",
            "subject": {"type": "smartphone"},
        }
    }
    state = adapt_v1(doc, POLICY.vocabulary)
    assert state.topic.product_type == "smartphone" and state.topic.type_learned


def test_a_learned_type_does_not_travel_to_a_new_shelf():
    """N-B: primul raft peste un tip DEDUS nu păstrează tipul (clientul nu l-a spus)."""
    from src.conversation.state_v2 import Topic

    learned = ConversationStateV2(topic=Topic(product_type="smartphone", type_learned=True))
    moved, _ = step(learned, _change("category", "laptopuri"))
    assert (moved.state.topic.category_key, moved.state.topic.product_type) == ("laptopuri", None)
    assert moved.state.topic.changed_at_revision == moved.state.revision


def test_the_verified_pair_must_be_the_pair_the_reducer_applies():
    """N-C: o pereche verificată pe ALTĂ stare (alt raft) nu face rafinare."""
    state, _ = step(ConversationStateV2(), _change("category", "telefoane"))
    delta = to_delta(
        _interp(1), [_change("product_type", "smartphone")], needs=POLICY.vocabulary, turn_id="t"
    )
    stale = replace(
        delta,
        proposals=tuple(
            replace(p, pair_verified=("laptopuri", "smartphone")) for p in delta.proposals
        ),
    )
    result = reduce_turn(state.state, stale, (), (), None, False, POLICY)
    assert result.state.topic.category_key is None  # nu e rafinare: subiect nou, raftul golit


def test_a_resume_checks_the_pair_on_the_parked_subject():
    state, _ = step(ConversationStateV2(), _change("category", "telefoane"))
    state, _ = step(state.state, _change("category", "laptopuri"))
    pair = proposal_pair(
        to_delta(
            _interp(1), [_change("product_type", "smartphone")], needs=POLICY.vocabulary
        ).proposals[0],
        state.state,
        "resume",
    )
    assert pair == ("telefoane", "smartphone")


def test_a_subject_change_to_the_parked_subject_is_a_swap_not_an_eviction():
    """N-E (pre-existent, mascat de I4): «înapoi la telefoane» ca raft nou pierdea nevoile
    subiectului parcat; acum e un SCHIMB, ca `resume`."""
    state, _ = step(ConversationStateV2(), _change("category", "telefoane"))
    state, _ = step(state.state, _change("brand", "samsung"))
    state, _ = step(state.state, _change("category", "laptopuri"))
    back, _ = step(state.state, _change("category", "telefoane"))
    assert back.state.topic.category_key == "telefoane"
    assert back.state.need_for("brand") is not None
    assert back.state.parked.topic.category_key == "laptopuri"
    assert "evicted" not in [a.outcome for a in back.applied if a.op == "park"]


async def test_the_pairs_read_happens_only_when_needed_and_a_failure_is_counted():
    """N-D: citirea doar pentru o pereche completă; picată ⇒ contor, nicio pereche verificată."""
    from contextlib import asynccontextmanager

    from src.agent import interpreted_turn as it
    from tests.kernel import fixture_catalog as fc

    ops: list[str] = []

    class _Deps:
        def db(self, op):
            @asynccontextmanager
            async def _cm():
                ops.append(op)
                raise ConnectionError("jos")
                yield None  # pragma: no cover

            return _cm()

    vocab = fc.vocabulary("electronics")
    only_type = to_delta(
        _interp(1), [_change("product_type", "smartphone")], needs=POLICY.vocabulary
    )
    same = await it._with_pair_compatibility(_Deps(), "b", only_type, ConversationStateV2(), vocab)
    assert same is only_type and ops == []

    state, _ = step(ConversationStateV2(), _change("category", "telefoane"))
    failed = await it._with_pair_compatibility(_Deps(), "b", only_type, state.state, vocab)
    assert ops == [it.SUBJECT_PAIRS_OP]
    assert failed.counters.get("subject_pairs_unavailable") == 1
    assert all(p.pair_verified is None for p in failed.proposals)


def test_a_legacy_first_shelf_gives_it_to_the_earlier_needs():
    """N-F: prima ancorare pe calea v1 (`_learn_subject`) dă și ea raftul nevoilor spuse înainte,
    altfel o schimbare interpretată ulterioară nu le-ar parca."""
    from src.conversation.state_reducer import StateUpdateProposal as P

    state, _ = step(ConversationStateV2(), _change("brand", "samsung"))
    legacy = P("set_topic", category_key="telefoane", subject=True, source="catalog", turn_id="v1")
    anchored = reduce_turn(
        state.state, TurnDelta(thread="continue", proposals=(legacy,)), (), (), None, False, POLICY
    )
    moved, _ = step(anchored.state, _change("category", "laptopuri"))
    assert moved.state.need_for("brand") is None
    assert ("brand", "samsung") in {(n.key, n.normalized_value) for n in moved.state.parked.needs}


# --- NX-350 (kernel.v4.0): un tip spus VAG ține minte UMBRELA clientului -----------------------


def test_a_vague_type_becomes_an_umbrella_not_the_subject_type():
    """Fără vocabular, umbrela e codul presupus; tipul subiectului rămâne gol."""
    result, delta = step(
        ConversationStateV2(), _change("product_type", "smartphone", provenance="implicit")
    )
    topic = result.state.topic
    assert topic.product_type is None and topic.type_umbrella == ("smartphone",)
    assert delta.counters.get("subject_type_umbrella") == 1
    assert topic.has_subject


def test_an_umbrella_next_to_a_shelf_keeps_the_shelf():
    result, _ = step(
        ConversationStateV2(),
        _change("category", "telefoane"),
        _change("product_type", "smartphone", provenance="implicit"),
    )
    topic = result.state.topic
    assert (topic.category_key, topic.product_type, topic.type_umbrella) == (
        "telefoane",
        None,
        ("smartphone",),
    )


def test_a_stated_type_replaces_the_umbrella_without_parking():
    state, _ = step(ConversationStateV2(), _change("category", "telefoane"))
    state, _ = step(state.state, _change("product_type", "smartphone", provenance="implicit"))
    state, _ = step(state.state, _change("brand", "samsung"))
    stated, _ = step(state.state, _change("product_type", "smartphone"), compatible=True)
    topic = stated.state.topic
    assert (topic.product_type, topic.type_umbrella) == ("smartphone", ())
    assert stated.state.parked is None and stated.state.need_for("brand") is not None


def _vague(umbrella):
    return _change("product_type", umbrella[0], provenance="implicit", umbrella=umbrella)


CREAMS = ("crema de fata", "crema de corp")
LIPS = ("ruj", "luciu de buze")


def test_an_overlapping_umbrella_replaces_the_old_one_without_parking():
    state, _ = step(ConversationStateV2(), _vague(CREAMS))
    state, _ = step(state.state, _change("brand", "samsung"))
    moved, _ = step(state.state, _vague(("crema de corp", "crema de maini")))
    assert moved.state.topic.type_umbrella == ("crema de corp", "crema de maini")
    assert moved.state.parked is None and moved.state.need_for("brand") is not None


def test_a_disjoint_umbrella_is_another_kind_of_item_and_parks():
    """Recenzia NX-350, constatarea 3: regula e aceeași pe ambele sensuri."""
    state, _ = step(ConversationStateV2(), _vague(CREAMS))
    state, _ = step(state.state, _change("brand", "samsung"))
    moved, _ = step(state.state, _vague(LIPS))
    assert moved.state.topic.type_umbrella == LIPS
    assert moved.state.parked.topic.type_umbrella == CREAMS
    assert moved.state.need_for("brand") is None
    assert [n.key for n in moved.state.parked.needs] == ["brand"]


def test_a_stated_type_outside_the_umbrella_parks_it():
    state, _ = step(ConversationStateV2(), _change("category", "telefoane"), _vague(CREAMS))
    state, _ = step(state.state, _change("brand", "apple"))
    moved, _ = step(state.state, _change("product_type", "laptop"))
    # A treia recenzie NX-352: fără pereche verificată, raftul vechi pleacă (ca la tip schimbat)
    assert (moved.state.topic.category_key, moved.state.topic.product_type) == (None, "laptop")
    assert moved.state.parked.topic.type_umbrella == CREAMS
    assert moved.state.need_for("brand") is None
    kept, _ = step(state.state, _change("product_type", "laptop"), compatible=True)
    assert (kept.state.topic.category_key, kept.state.topic.product_type) == ("telefoane", "laptop")


def test_another_kind_keeps_the_shelf_and_naming_it_again_loses_nothing():
    """A doua recenzie NX-350, constatările 1-2: «cremă pentru ten» → «un ser» rămâne pe raft când
    perechea (raft, ser) există (NX-352), iar «un ser pentru ten» apoi nu aduce înapoi cremele
    parcate și nu evacuează nevoile serului. Fără pereche verificată, raftul pleacă."""
    state, _ = step(ConversationStateV2(), _change("category", "ten"), _vague(CREAMS))
    state, _ = step(state.state, _change("brand", "apple"))
    serums = ("ser de fata", "ser de par")
    unverified, _ = step(state.state, _vague(serums))
    assert unverified.state.topic.category_key is None
    state, _ = step(state.state, _vague(serums), compatible=True)
    assert (state.state.topic.category_key, state.state.topic.type_umbrella) == ("ten", serums)
    assert state.state.parked.topic.type_umbrella == CREAMS
    state, _ = step(state.state, _change("brand", "samsung"))
    again, _ = step(state.state, _change("category", "ten"), _vague(serums))
    assert again.state.topic == state.state.topic
    assert again.state.need_for("brand").normalized_value == "samsung"
    assert again.state.parked.topic.type_umbrella == CREAMS
    assert not [a for a in again.applied if a.op == "park"]


def test_a_stated_type_inside_an_umbrella_only_subject_refines_it():
    """Constatarea 4: nu e „primul" subiect, deci revizia lui rămâne și nevoile la fel."""
    state, _ = step(ConversationStateV2(), _vague(CREAMS))
    revision = state.state.topic.changed_at_revision
    state, _ = step(state.state, _change("brand", "apple"))
    refined, _ = step(state.state, _change("product_type", "crema de corp"))
    topic = refined.state.topic
    assert (topic.product_type, topic.type_umbrella) == ("crema de corp", ())
    assert topic.changed_at_revision == revision
    assert refined.state.parked is None and refined.state.need_for("brand") is not None
    assert [a.outcome for a in refined.applied if a.op == "set_topic"] == ["refined"]


def test_the_same_vague_code_as_the_stated_type_parks_nothing():
    """Constatarea 1: «ceva pentru ten» nu e alt fel de produs decât crema spusă clar."""
    state, _ = step(ConversationStateV2(), _change("product_type", "crema de fata"))
    state, _ = step(state.state, _change("brand", "apple"))
    same, _ = step(state.state, _change("product_type", "crema de fata", provenance="implicit"))
    assert same.state.topic == state.state.topic and same.state.parked is None


def test_an_umbrella_only_subject_is_parked_and_resumed_whole():
    """Constatarea 2: `_resume` și nevoile unui subiect fără raft și fără tip spus clar."""
    state, _ = step(ConversationStateV2(), _change("product_type", "smartphone"))
    state, _ = step(state.state, _change("brand", "samsung"))
    state, _ = step(state.state, _vague(("accesoriu",)))
    state, _ = step(state.state, _change("brand", "apple"))
    delta = to_delta(
        _interp(0).model_copy(update={"thread": "resume"}), [], needs=POLICY.vocabulary, turn_id="t"
    )
    back = reduce_turn(state.state, delta, (), (), None, False, POLICY).state
    assert back.topic.product_type == "smartphone"
    assert back.need_for("brand").normalized_value == "samsung"
    assert back.parked.topic.type_umbrella == ("accesoriu",)
    assert [n.normalized_value for n in back.parked.needs] == ["apple"]


def test_clear_topic_retires_the_needs_of_an_umbrella_only_subject():
    state, _ = step(ConversationStateV2(), _vague(CREAMS))
    state, _ = step(state.state, _change("brand", "apple"))
    cleared = reduce_turn(
        state.state,
        TurnDelta(
            thread="continue",
            proposals=(StateUpdateProposal("clear_topic", source="user_explicit", turn_id="t"),),
        ),
        (),
        (),
        None,
        False,
        POLICY,
    ).state
    assert cleared.need_for("brand") is None and cleared.topic.type_umbrella == CREAMS


def test_correcting_the_umbrella_with_another_kind_is_a_subject_contradiction():
    """Constatarea 4 (I21): corecția vag → vag disjunct retrage ce a spus turul anterior."""
    state, _ = step(
        ConversationStateV2(), _vague(CREAMS), _change("brand", "apple", provenance="implicit")
    )
    fixed, _ = step(state.state, _vague(LIPS), corrects=True)
    assert fixed.state.need_for("brand") is None
    assert "unconfirmed" not in [a.outcome for a in fixed.applied if a.op == "correction"]


def test_two_vague_types_merge_in_turns_and_a_stated_one_counts_as_multiple():
    """Constatarea 5: plafonul nu taie tăcut o umbrelă întreagă."""
    creams = tuple(f"crema {i}" for i in range(8))
    serums = ("ser de fata", "ser de par")
    _, delta = step(ConversationStateV2(), _vague(creams), _vague(serums))
    merged = delta.proposals[0].type_umbrella
    assert "ser de fata" in merged and "ser de par" in merged and len(merged) == 8
    _, delta = step(ConversationStateV2(), _change("product_type", "ruj"), _vague(CREAMS))
    assert delta.proposals[0].product_type == "ruj"
    assert delta.counters.get("subject_multiple") == 1


def test_the_v1_path_anchoring_a_shelf_keeps_the_umbrella_and_a_learned_type_does_not_beat_it():
    """Constatarea 7: o scriere v1 a subiectului nu uită umbrela, iar plannerul ordonează pe ea."""
    from src.agent import turn_planner as tp

    state, _ = step(ConversationStateV2(), _vague(CREAMS))
    learn = StateUpdateProposal(
        "set_topic",
        category_key="ten",
        product_type="crema de fata",
        subject=True,
        source="catalog",
        turn_id="v1",
    )
    after = reduce_turn(
        state.state, TurnDelta(thread="continue", proposals=(learn,)), (), (), None, False, POLICY
    ).state
    assert after.topic.type_umbrella == CREAMS and after.topic.type_learned
    assert tp.subject_kinds(after.topic) == CREAMS
    shelfless = replace(after.topic, category_key=None)
    planner = tp._Planner.__new__(tp._Planner)
    planner.state = replace(after, topic=shelfless)
    planner.vocab, planner.pack, planner.locale = None, None, "ro"
    assert planner._subject_label() == "crema"


def test_a_vague_request_for_another_kind_of_item_is_a_subject_change():
    """Pe un tip spus clar, o umbrelă care NU îl conține înseamnă alt fel de produs: subiect nou pe
    același raft (se parchează). Una care îl conține e o reformulare mai largă: nimic."""
    state, _ = step(
        ConversationStateV2(),
        _change("category", "telefoane"),
        _change("product_type", "smartphone"),
    )
    state, _ = step(state.state, _change("brand", "samsung"))
    other, _ = step(state.state, _change("product_type", "accesoriu", provenance="implicit"))
    assert other.state.topic.type_umbrella == ("accesoriu",)
    assert other.state.parked.topic.product_type == "smartphone"
    same, _ = step(state.state, _change("product_type", "smartphone", provenance="implicit"))
    assert same.state.topic == state.state.topic and same.state.parked is None


def test_the_umbrella_survives_the_jsonb_round_trip_and_is_absent_when_empty():
    from src.conversation.state_v2 import Topic

    assert "type_umbrella" not in Topic(category_key="x").to_jsonb()
    doc = Topic(type_umbrella=("a", "b")).to_jsonb()
    assert Topic.from_jsonb(doc).type_umbrella == ("a", "b")


def test_the_validator_computes_the_umbrella_from_the_customers_words():
    """«o cremă» ⇒ toate cremele; «creme de față» (plural, deci `implicit`) ⇒ doar crema de față;
    un cuvânt fără tip lângă cuvântul-tip nu îngustează umbrela."""
    from src.catalog.vocabulary import CatalogVocabulary, VocabEntry
    from src.conversation.interpretation import Act, StateChange, TurnInterpretation
    from src.conversation.provenance import UserWords, check_changes

    kinds = (
        "crema de fata",
        "crema de corp",
        "crema de maini",
        "ser de fata",
        "sampon",
        "fond de ten",
        "masca de par",
    )
    vocab = CatalogVocabulary(
        business_id="b",
        dimensions={
            "product_type": tuple(
                VocabEntry(key=k, label=k, count=9 - i) for i, k in enumerate(kinds)
            )
        },
    )

    def umbrella(said: str, quote: str) -> tuple[str, ...]:
        change = StateChange(
            op="set",
            target=None,
            dimension="product_type",
            relation="eq",
            value="crema de fata",
            number=None,
            unit=None,
            relative_to=None,
            quote=quote,
        )
        interp = TurnInterpretation(
            thread="continue",
            acts=[Act(kind="find", targets=[], query=None)],
            changes=[change],
            references=[],
            ambiguities=[],
            corrects_previous_turn=False,
        )
        [c] = check_changes(
            interp, words=UserWords(said, ()), vocab=vocab, pack=fc.pack("sole-ro"), locale="ro"
        )
        return c.provenance, c.umbrella

    prov, umb = umbrella("vreau o crema de hidratare", "o crema de hidratare")
    assert prov == "implicit" and umb[0] == "crema de fata"
    assert set(umb) == {"crema de fata", "crema de corp", "crema de maini"}
    # kernel.v6.0 (NX-364): pluralul numește codul întreg, cuvânt cu cuvânt, cu flexiunea
    # locale-i, deci e tipul SPUS, nu o umbrelă (pe v5.1: `implicit`, umbrela „crema de fata").
    prov, umb = umbrella("vreau creme de fata", "creme de fata")
    assert prov == "explicit" and umb == ()
    prov, umb = umbrella("vreau o crema de fata", "crema de fata")
    assert prov == "explicit" and umb == ()
    # Recenzia NX-350, constatarea 1: doar CAPUL unui cod deschide o umbrelă. «ten» e coada lui
    # „fond de ten", «pare» nu e «par», «fata de» nu e o cremă: umbrela e codul presupus.
    assert umbrella("vreau ceva pentru ten", "ceva pentru ten")[1] == ("crema de fata",)
    _, umb = umbrella("o crema, mi se pare ca am pielea uscata", "o crema, mi se pare ca")
    assert "masca de par" not in umb and "fond de ten" not in umb and umb[0] == "crema de fata"
    _, umb = umbrella("o crema pentru ten uscat", "o crema pentru ten uscat")
    assert set(umb) == {"crema de fata", "crema de corp", "crema de maini"}
    assert umbrella("un ser mai ieftin fata de asta", "un ser mai ieftin fata de asta")[1] == (
        "crema de fata",
    )
    # A doua recenzie, constatarea 4: coada codului oriunde altundeva nu îngustează umbrela.
    said = "o crema mai ieftina fata de cealalta"
    assert set(umbrella(said, said)[1]) == {"crema de fata", "crema de corp", "crema de maini"}


def test_the_planner_prefers_every_code_of_the_umbrella_and_labels_it_with_the_shared_word():
    from src.agent import turn_planner as tp
    from src.conversation.state_v2 import Topic

    state = ConversationStateV2(topic=Topic(type_umbrella=("crema de fata", "crema de corp")))
    label = tp._Planner.__new__(tp._Planner)
    label.state, label.vocab, label.pack, label.locale = state, None, None, "ro"
    assert label._subject_label() == "crema"
    assert label._subject()
    # Constatarea 6 (ambele recenzii): eticheta e capul primului cod, nu un cuvânt din coadă comun
    # tuturor («fata» e raftul de machiaj) și nici unul gol («de»).
    label.state = ConversationStateV2(topic=Topic(type_umbrella=("ser de fata", "masca de fata")))
    assert label._subject_label() == "ser"


def test_the_model_and_the_trace_see_the_umbrella():
    """Constatarea 8: vederea modelului și forma compactă din trace poartă umbrela."""
    from src.conversation.kernel_trace import state_view
    from src.conversation.state_v2 import ParkedTopic, Topic
    from src.conversation.turn_interpreter import _subject

    class _Input:
        pack = fc.pack("sole-ro")
        locale = "ro"

    inp = _Input()
    inp.state = ConversationStateV2(
        topic=Topic(type_umbrella=CREAMS),
        parked=ParkedTopic(topic=Topic(type_umbrella=LIPS), needs=(), shown=()),
    )
    lines = _subject(inp)
    assert any(line.startswith("type said broadly:") for line in lines)
    assert "PARKED: ruj | luciu de buze" in lines
    assert state_view(inp.state)["umbrella"] == list(CREAMS)
    assert "umbrella" not in state_view(ConversationStateV2())


def test_the_scorer_counts_an_umbrella_of_another_kind_as_moving_the_subject():
    """Constatarea 9: D3 nu mai judecă „deja activ" pe un tur care parchează prin umbrelă."""
    from scripts import nx335_interpret_replay as rp
    from src.conversation.state_v2 import Topic

    stated = ConversationStateV2(topic=Topic(product_type="crema de fata"))
    assert rp._umbrella_moves(stated, _vague(LIPS))
    assert not rp._umbrella_moves(stated, _vague(CREAMS))
    vague = ConversationStateV2(topic=Topic(type_umbrella=CREAMS))
    assert rp._umbrella_moves(vague, _vague(LIPS))
    assert not rp._umbrella_moves(vague, _change("product_type", "ruj"))


def test_a_new_stated_type_keeps_the_old_shelf_only_on_a_verified_pair():
    """NX-352 (sonda NX-351): «vreau un ruj» după un șampon pe raftul de păr nu caută rujuri pe
    raftul de păr. Raftul vechi rămâne doar pe o pereche (raft, tip nou) VERIFICATĂ în catalog."""
    state, _ = step(
        ConversationStateV2(),
        _change("category", "telefoane"),
        _change("product_type", "smartphone"),
    )
    moved, _ = step(state.state, _change("product_type", "laptop"))
    assert (moved.state.topic.category_key, moved.state.topic.product_type) == (None, "laptop")
    kept, _ = step(state.state, _change("product_type", "laptop"), compatible=True)
    assert (kept.state.topic.category_key, kept.state.topic.product_type) == ("telefoane", "laptop")
