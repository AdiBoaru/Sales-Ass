"""NX-390 (`kernel.v10.0`) — familia unei rutini o decide ce a SPUS clientul, nu dacă modelul a
scris un raft.

Setul nevăzut `routines-2026-10-09`, rulat pe producție cu NX-389 aprins, a picat regula GO pe două
ture cu aceeași cauză:

- `d847f351` «buna, vreau o rutina pentru ten gras»: modelul a scris doar `skin_type=oily` (citatul
  „ten gras”), fără raft ⇒ poarta a întrebat „machiaj, ten sau păr?”, iar la «doar pt seara» rutina
  a căzut pe familia majoritară a tenului gras, machiajul;
- `75137b28` «fa mi o rutina pt piele deshidratata»: modelul a ghicit `ten-ingrijirea-tenului` din
  „piele” (`implicit`), iar poarta l-a luat drept familie spusă ⇒ fără întrebare.

Lanțul pur (`kernel_step`), zero model, zero DB."""

from __future__ import annotations

import dataclasses

from src.catalog.vocabulary import CATEGORY_DIMENSION, CatalogVocabulary, VocabEntry
from src.conversation.ambiguity_gate import ROUTINE_FAMILY_KEY
from src.conversation.state_v2 import ConversationStateV2, Topic
from tests.kernel import fixture_catalog as fc
from tests.test_nx389_routine_family_question import (
    OFF,
    ON,
    _primary,
    change,
    family_step,
    interp,
    shelves_vocab,
    sole_pack,
)

TEN_GRAS = change("skin_type", "oily", "ten gras")


def _topic(step) -> str | None:
    return step.gate_state.topic.category_key


# --- r1: «rutina pentru ten gras» ---------------------------------------------------------------


def test_the_shelf_named_inside_the_condition_is_noted_by_the_validator():
    step = family_step("buna, vreau o rutina pentru ten gras", TEN_GRAS)
    (checked,) = step.checked
    assert checked.provenance == "explicit"
    assert checked.matched == ("ten", "gras")
    assert checked.shelf == "ten"


def test_a_routine_for_oily_skin_is_a_face_routine_without_asking():
    step = family_step("buna, vreau o rutina pentru ten gras", TEN_GRAS)
    out = step.outcome
    assert out.decision.reason != "no_family"
    assert out.asked_key != ROUTINE_FAMILY_KEY
    assert _topic(step) == "ten"
    assert step.delta.counters.get("subject_from_named_part") == 1
    plan = _primary(step)
    assert (plan.executor, plan.family) == ("bundle", "fata")


def test_the_next_turn_keeps_the_face_routine_not_the_makeup_majority():
    first = family_step("buna, vreau o rutina pentru ten gras", TEN_GRAS)
    state = dataclasses.replace(first.state_after, revision=first.state_after.revision)
    step = family_step("doar pt seara", change("routine_time", "pm", "seara"), state=state)
    assert step.outcome.decision.verdict == "act"
    plan = _primary(step)
    assert (plan.executor, plan.family) == ("bundle", "fata")
    assert "family_defaulted" not in [code for _, code in step.planned.disclosures]


def test_the_named_shelf_beats_a_shelf_the_model_only_guessed():
    guessed = change("category", "corp", "ten")  # ghicit: „ten” nu numește raftul `corp`
    step = family_step("rutina pentru ten gras", TEN_GRAS, guessed)
    assert _topic(step) == "ten"
    assert (_primary(step).executor, _primary(step).family) == ("bundle", "fata")


def test_an_explicit_other_shelf_wins_over_the_part_named_in_the_condition():
    """«rutină de machiaj pentru ten gras»: clientul a numit machiajul (recenzia NX-388)."""
    step = family_step(
        "rutina de machiaj pentru ten gras", change("category", "machiaj", "machiaj"), TEN_GRAS
    )
    assert _topic(step) == "machiaj"
    assert "subject_from_named_part" not in step.delta.counters
    assert _primary(step).family == "machiaj"


def test_a_find_does_not_turn_the_named_part_into_the_subject():
    """Regula e a rutinei: o căutare «ceva pentru ten gras» rămâne cum era (NX-388)."""
    step = family_step("ceva pentru ten gras", TEN_GRAS, act="find")
    assert _topic(step) is None
    assert "subject_from_named_part" not in step.delta.counters


def test_an_avoided_need_names_no_shelf_for_the_subject():
    avoided = {**TEN_GRAS, "relation": "avoid", "quote": "nu ten gras"}
    step = family_step("rutina, nu ten gras", avoided)
    assert "subject_from_named_part" not in step.delta.counters


# --- r10: «rutina pt piele deshidratata» ---------------------------------------------------------


def test_a_shelf_the_model_guessed_this_turn_still_asks_for_the_family():
    step = family_step("fa mi o rutina pt piele deshidratata", change("category", "ten", "piele"))
    (checked,) = step.checked
    assert checked.provenance == "implicit"
    out = step.outcome
    assert (out.decision.verdict, out.decision.reason) == ("must_ask", "no_family")
    assert out.asked_key == ROUTINE_FAMILY_KEY
    assert _primary(step).executor == "ask"


def test_a_guessed_shelf_without_a_routine_family_never_asks_for_one():
    """Pe trafic (40 de zile, 75 de ture `bundle`), un raft `implicit` fără familie de rutină e o
    cerere de UN produs etichetată `bundle` («si o periuta pt el?» ⇒ `igiena-dentara`): regula GO
    NX-389 cere zero întrebări de familie pe ele."""
    base = shelves_vocab()
    extra = VocabEntry(
        key="igiena-dentara", label="Igiena dentara", count=40, path="igiena-dentara"
    )
    vocab = CatalogVocabulary(
        business_id=base.business_id,
        dimensions={
            **base.dimensions,
            CATEGORY_DIMENSION: (*base.categories, extra),
        },
    )
    step = fc.kernel_step(
        "sole-ro",
        ConversationStateV2(revision=1),
        interp(change("category", "igiena-dentara", "periuta")),
        "si o periuta pt el?",
        loaded=sole_pack(),
        vocab=vocab,
        policy=ON,
        answer_pending=True,
    )
    assert step.outcome.asked_key != ROUTINE_FAMILY_KEY
    assert step.outcome.decision.verdict == "act"


def test_a_shelf_said_explicitly_does_not_ask():
    step = family_step("imi trebuie o rutina de corp", change("category", "corp", "de corp"))
    assert step.outcome.decision.verdict == "act"
    assert (_primary(step).executor, _primary(step).family) == ("bundle", "corp")


def test_a_shelf_from_an_earlier_turn_is_the_family_even_if_it_was_guessed_then():
    """«mi se usucă pielea pe picioare» → «fă-mi o rutină»: subiectul de mai devreme e contextul
    acestui client (r9 din set), nu o ghicire a turului."""
    state = ConversationStateV2(revision=3, topic=Topic(category_key="corp", changed_at_revision=2))
    step = family_step("fa mi o rutina", state=state)
    assert step.outcome.decision.verdict == "act"
    assert (_primary(step).executor, _primary(step).family) == ("bundle", "corp")


def test_after_the_question_the_guessed_shelf_does_not_beat_the_majority_choice():
    """Fără alegere la întrebare, familia o decide poarta (majoritatea, spusă), nu raftul ghicit."""
    first = family_step("fa mi o rutina pt piele deshidratata", change("category", "corp", "piele"))
    assert first.outcome.asked_key == ROUTINE_FAMILY_KEY
    step = family_step("nu stiu, alege tu", state=first.state_after)
    plan = _primary(step)
    assert (plan.executor, plan.family) == ("bundle", "fata")
    assert "family_defaulted" in [code for _, code in step.planned.disclosures]


def test_the_shelf_chosen_after_a_guessed_question_is_the_family_from_then_on():
    first = family_step("fa mi o rutina pt piele deshidratata", change("category", "corp", "piele"))
    chosen = family_step("pentru ten", change("category", "ten", "ten"), state=first.state_after)
    assert _topic(chosen) == "ten"
    assert (_primary(chosen).executor, _primary(chosen).family) == ("bundle", "fata")
    later = family_step(
        "doar pt seara", change("routine_time", "pm", "seara"), state=chosen.state_after
    )
    assert later.outcome.decision.verdict == "act"
    assert (_primary(later).executor, _primary(later).family) == ("bundle", "fata")
    assert "family_defaulted" not in [code for _, code in later.planned.disclosures]


def test_with_the_flag_off_a_guessed_shelf_is_todays_routine():
    step = family_step(
        "fa mi o rutina pt piele deshidratata", change("category", "ten", "piele"), policy=OFF
    )
    assert step.outcome.decision.verdict == "act"
    assert (_primary(step).executor, _primary(step).family) == ("bundle", "fata")
