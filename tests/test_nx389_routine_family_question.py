"""NX-389a (`kernel.v9.0`) — o rutină fără familie întreabă pentru ce e, între familiile pe care
catalogul le poate servi.

Turul real: «fa-mi o rutina de seara pt pete pigmentare» (`a04d7b5b`, `sole-ro`, 2026-10-07).
Nevoia `concerns:hyperpigmentation` e împărțită pe catalog între îngrijirea feței (269) și machiaj
(121), deci `routine_family` n-are familie, iar planul era o căutare (cinci seruri). Decizia lui Adi
(2026-10-09): machiajul intră în calcul, iar ambiguitatea se întreabă; «nu știu» ⇒ familia
majoritară, spusă clientului, fără a doua întrebare. Lanțul pur (`kernel_step`), zero model, zero
DB."""

from __future__ import annotations

import dataclasses

import pytest

from src.catalog.vocabulary import CATEGORY_DIMENSION, CatalogVocabulary, VocabEntry
from src.conversation.ambiguity_gate import ROUTINE_FAMILY_KEY
from src.conversation.clarification_policy import ClarificationPolicy
from src.conversation.interpretation import TurnInterpretation
from src.conversation.routine_family import FamilyOption, routine_family_options
from src.conversation.state_v2 import AskedQuestion, ConversationStateV2
from tests.kernel import fixture_catalog as fc
from tests.kernel import fixture_sole_catalog as sole

ON = ClarificationPolicy(routine_family_question=True)
OFF = ClarificationPolicy()

COUNTS = {
    "concerns:hyperpigmentation": {"fata": 269, "machiaj": 121, "corp": 1, "par": 1},
    "concerns:dandruff": {"par": 11},
    "concerns:dark_circles": {"machiaj": 50, "fata": 3},
    "concerns:pores": {"fata": 271, "machiaj": 44, "corp": 8},
    "skin_type:dry": {"fata": 242, "machiaj": 66, "corp": 39, "par": 9},
}
TOTALS = {"fata": 1253, "machiaj": 483, "par": 158, "corp": 125}


def sole_pack(**overrides):
    """Pachetul SOLE cu numărătorile NX-389 fixe (seed-ul se poate regenera fără să mute
    testele)."""
    pack = fc.pack("sole-ro")
    fields = {
        "family_by_need": {"concerns:pores": "fata"},
        "family_counts_by_need": COUNTS,
        "family_counts": TOTALS,
        "min_family_products": 15,
        **overrides,
    }
    steps = dataclasses.replace(pack.routine_steps, **fields)
    return dataclasses.replace(pack, routine_steps=steps)


def shelves_vocab() -> CatalogVocabulary:
    """Vocabularul SOLE sintetic cu rafturile pe CĂI, ca în producție (rădăcinile din
    `family_by_shelf`)."""
    base = sole.vocabulary()
    needs = {
        "concerns": ("hyperpigmentation", "dandruff", "dark_circles", "pores", "hydration"),
        "skin_type": ("dry", "oily", "sensitive"),
    }
    dimensions = dict(base.dimensions)
    for dimension, values in needs.items():
        have = {e.key for e in dimensions.get(dimension, ())}
        extra = tuple(VocabEntry(key=v, label=v, count=20) for v in values if v not in have)
        dimensions[dimension] = (*dimensions.get(dimension, ()), *extra)
    entries = (
        VocabEntry(key="ten", label="Ten", count=900, path="ten"),
        VocabEntry(key="par", label="Par", count=300, path="par"),
        VocabEntry(key="corp", label="Corp", count=200, path="corp"),
        VocabEntry(key="machiaj", label="Machiaj", count=500, path="machiaj"),
    )
    return CatalogVocabulary(
        business_id=base.business_id, dimensions={**dimensions, CATEGORY_DIMENSION: entries}
    )


def change(dimension, value, quote):
    return {
        "op": "set",
        "dimension": dimension,
        "value": value,
        "quote": quote,
        "relation": "eq",
        "number": None,
        "unit": None,
        "target": None,
        "relative_to": None,
    }


def interp(*changes, act="bundle", readings=None) -> TurnInterpretation:
    ambiguities = (
        [{"about": "scope", "target": None, "readings": list(readings)}] if readings else []
    )
    return TurnInterpretation.model_validate(
        {
            "thread": "continue",
            "acts": [{"kind": act, "targets": [], "query": None}],
            "changes": list(changes),
            "references": [],
            "ambiguities": ambiguities,
            "corrects_previous_turn": False,
        }
    )


def family_step(
    message: str,
    *changes,
    act="bundle",
    readings=None,
    policy=ON,
    state=None,
    pack=None,
):
    return fc.kernel_step(
        "sole-ro",
        state or ConversationStateV2(revision=1),
        interp(*changes, act=act, readings=readings),
        message,
        loaded=pack or sole_pack(),
        vocab=shelves_vocab(),
        policy=policy,
        answer_pending=True,
    )


PETE = change("concerns", "hyperpigmentation", "pete pigmentare")
SEARA = change("routine_time", "pm", "de seara")


def _primary(step):
    return step.planned.plans[step.planned.primary]


# --- întrebarea ----------------------------------------------------------------------------------


def test_a_routine_for_dark_spots_asks_face_care_or_makeup():
    step = family_step("fa-mi o rutina de seara pt pete pigmentare", PETE, SEARA)
    out = step.outcome
    assert (out.decision.verdict, out.decision.reason) == ("must_ask", "no_family")
    assert out.asked_key == ROUTINE_FAMILY_KEY
    assert out.family_labels == ("Ten", "Machiaj")
    assert out.decision.question == "Pentru ce să fie rutina: Ten și Machiaj?"
    assert _primary(step).executor == "ask"
    assert step.memory is not None and step.memory.key == ROUTINE_FAMILY_KEY


def test_a_routine_with_nothing_said_asks_among_every_family_that_can_serve_one():
    step = family_step("fa-mi o rutina")
    assert step.outcome.decision.reason == "no_family"
    assert step.outcome.family_labels == ("Ten", "Machiaj", "Par", "Corp")


def test_the_model_reading_face_or_body_beats_the_catalog_majority():
    """«fă-mi o rutină» după «mi se usucă pielea după duș»: pe catalog, ten uscat e 70% față, dar
    lectura modelului spune că ACEST client poate vorbi despre corp."""
    step = family_step(
        "fa mi o rutina",
        change("skin_type", "dry", "mi se usuca pielea"),
        readings=("ten", "corp"),
    )
    out = step.outcome
    assert (out.decision.verdict, out.decision.reason) == ("must_ask", "family_readings")
    assert out.family_labels == ("Ten", "Corp")


# --- fără întrebare ------------------------------------------------------------------------------


def test_a_single_family_that_can_serve_the_need_is_planned_without_asking():
    step = family_step("rutina pentru cearcane", change("concerns", "dark_circles", "cearcane"))
    out = step.outcome
    assert out.decision.verdict == "act"
    assert out.routine_family == "machiaj"
    plan = _primary(step)
    assert (plan.executor, plan.family) == ("bundle", "machiaj")
    assert "family_defaulted" not in [code for _, code in step.planned.disclosures]


def test_no_family_that_can_serve_the_need_keeps_todays_plan():
    step = family_step("rutina pentru matreata", change("concerns", "dandruff", "matreata"))
    assert step.outcome.decision.verdict == "act"
    assert step.outcome.routine_family is None
    assert _primary(step).executor == "search"


def test_a_need_with_a_clear_family_does_not_ask():
    step = family_step("rutina pentru pori", change("concerns", "pores", "pori"))
    assert step.outcome.decision.verdict == "act"
    assert (_primary(step).executor, _primary(step).family) == ("bundle", "fata")


def test_a_named_shelf_does_not_ask():
    step = family_step("rutina pentru ten cu pete", change("category", "ten", "ten"), PETE)
    assert step.outcome.decision.verdict == "act"
    assert (_primary(step).executor, _primary(step).family) == ("bundle", "fata")


def test_a_find_does_not_ask_for_a_routine_family():
    step = family_step("ceva pentru pete", PETE, act="find")
    assert step.outcome.decision.reason != "no_family"


def test_with_the_flag_off_the_turn_is_todays():
    step = family_step("fa-mi o rutina de seara pt pete pigmentare", PETE, SEARA, policy=OFF)
    assert step.outcome.decision.verdict == "act"
    assert _primary(step).executor == "search"


def test_without_the_counts_in_the_pack_nothing_is_asked():
    pack = sole_pack(family_counts_by_need={}, family_counts={})
    step = family_step("fa-mi o rutina de seara pt pete pigmentare", PETE, SEARA, pack=pack)
    assert step.outcome.decision.verdict == "act"
    assert _primary(step).executor == "search"


# --- «nu știu»: a doua oară, familia majoritară, spusă -------------------------------------------


def test_after_the_question_was_asked_the_routine_takes_the_majority_family_and_says_so():
    asked = _asked_with(("concerns", "hyperpigmentation"))
    step = family_step("nu stiu, alege tu", state=asked)
    out = step.outcome
    assert (out.decision.verdict, out.decision.reason) == ("act", "already_asked")
    assert out.decision.question is None
    plan = _primary(step)
    assert (plan.executor, plan.family) == ("bundle", "fata")
    assert "family_defaulted" in [code for _, code in step.planned.disclosures]
    assert step.planned.family_labels == ("Ten", "Machiaj")


# --- opțiunile -----------------------------------------------------------------------------------


def _asked_with(*pairs) -> ConversationStateV2:
    """Starea de după întrebare: nevoile spuse la cerere și cheia deja întrebată o dată."""
    return dataclasses.replace(
        _need_state(*pairs),
        revision=2,
        asked_questions=(AskedQuestion(ROUTINE_FAMILY_KEY, 1, 1),),
    )


def _need_state(*pairs) -> ConversationStateV2:
    from tests.test_kernel_planner import _need

    return ConversationStateV2(revision=1, needs=tuple(_need(k, v) for k, v in pairs))


def test_options_sum_the_counts_of_every_known_need():
    state = _need_state(("concerns", "hyperpigmentation"), ("skin_type", "dry"))
    options = routine_family_options(state, pack=sole_pack())
    assert options == (
        FamilyOption("fata", "ten", 511),
        FamilyOption("machiaj", "machiaj", 187),
        FamilyOption("corp", "corp", 40),
    )


@pytest.mark.parametrize("families", [("fata", "corp"), ("corp", "fata")])
def test_options_keep_only_the_families_the_model_read(families):
    state = _need_state(("skin_type", "dry"))
    options = routine_family_options(state, pack=sole_pack(), families=families)
    assert [o.family for o in options] == ["fata", "corp"]


def test_the_loader_reads_the_counts_and_rejects_a_bad_family():
    from src.domain.routine_steps import RoutineStepConfigError, build_spec

    raw = {
        "families": {"fata": ["curatare"], "par": ["sampon"]},
        "by_product_type": {"spuma": "fata:curatare", "sampon": "par:sampon"},
        "family_counts_by_need": {"concerns:acne": {"fata": 10}},
        "family_counts": {"fata": 20, "par": 5},
        "min_family_products": 8,
    }
    spec = build_spec(raw)
    assert spec.family_counts_by_need == {"concerns:acne": {"fata": 10}}
    assert (spec.family_counts, spec.min_family_products) == ({"fata": 20, "par": 5}, 8)
    with pytest.raises(RoutineStepConfigError):
        build_spec({**raw, "family_counts": {"nu-exista": 3}})
    with pytest.raises(RoutineStepConfigError):
        build_spec({**raw, "family_counts_by_need": {"acne": {"fata": 1}}})


def test_the_derivation_counts_every_family_of_a_need():
    from scripts.derive_family_by_need import family_counts_by_need

    rows = [("concerns", "acne", "fata")] * 3 + [("concerns", "acne", "machiaj")] * 2
    rows += [("concerns", "acne", "necunoscuta")]
    assert family_counts_by_need(rows, ["fata", "machiaj"]) == {
        "concerns:acne": {"fata": 3, "machiaj": 2}
    }


# --- scenariile pentru vocabularul închis al motivelor --------------------------------------------


def reason_scenarios():
    """Câte un pas pentru fiecare motiv nou din `GATE_REASONS` (testul de vocabular închis)."""
    asked = _asked_with(("concerns", "hyperpigmentation"))
    return [
        lambda: family_step("fa-mi o rutina de seara pt pete pigmentare", PETE, SEARA),
        lambda: family_step(
            "fa mi o rutina", change("skin_type", "dry", "pielea"), readings=("ten", "corp")
        ),
        lambda: family_step(
            "rutina pentru cearcane", change("concerns", "dark_circles", "cearcane")
        ),
        lambda: family_step("rutina pentru matreata", change("concerns", "dandruff", "matreata")),
        lambda: family_step("nu stiu", state=asked),
    ]


# --- legăturile: flagul, dezvăluirea, datele pachetului -------------------------------------------


def test_the_flag_reaches_the_gate_policy(monkeypatch):
    from src.agent import interpreted_turn as it
    from src.config import get_settings

    on = get_settings().model_copy(update={"routine_family_question_enabled": True})
    monkeypatch.setattr(it, "get_settings", lambda: on)
    assert it._gate_policy(sole_pack()).routine_family_question is True
    off = get_settings().model_copy(update={"routine_family_question_enabled": False})
    monkeypatch.setattr(it, "get_settings", lambda: off)
    assert it._gate_policy(sole_pack()).routine_family_question is False


def test_the_composer_gets_the_family_it_was_built_for_and_the_others():
    from src.agent.composer import DISCLOSURES
    from src.agent.kernel_executors import _disclosures_of

    asked = _asked_with(("concerns", "hyperpigmentation"))
    planned = family_step("nu stiu", state=asked).planned
    facts = dict(_disclosures_of(planned))
    assert facts["family_defaulted"] == {"built_for": "Ten", "other_options": ["Machiaj"]}
    assert "family_defaulted" in DISCLOSURES


def test_the_default_pack_has_the_question_and_the_fallback_sentence():
    from src.domain.pack import kernel_sentence

    pack = fc.pack("sole-ro")
    assert "{options}" in pack.clarify_templates["ro"]["family"]
    assert kernel_sentence(pack, "ro", "family_defaulted")
    assert kernel_sentence(pack, "en", "family_defaulted")
