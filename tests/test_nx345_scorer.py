"""NX-345 — ce se numără ca schimbare (deciziile lui Adi, 2026-09-29), pe comparatorul replay-ului.

D1: o ipoteză a interpretării (`implicit`/`inferred`, pe care kernelul n-o face fapt) neetichetată e
NEUTRĂ în F1; ipotezele au metrica lor, precizia: câte numesc altă valoare pe o dimensiune
etichetată. D3: un `set` pe valoarea deja ACTIVĂ înaintea turului e neutru, fiindcă reducerul îl
tratează ca nimic. F1 pe regula de dinainte rămâne raportat (`f1_all_emitted`), ca verdictele v1-v3
să rămână reproductibile pe regula cu care au fost rulate. ZERO model, ZERO DB.
"""

from __future__ import annotations

from scripts import nx335_interpret_replay as rp
from src.conversation.interpretation import (
    Act,
    CheckedChange,
    StateChange,
    TurnInterpretation,
)
from src.conversation.state_v2 import ConversationStateV2, Need, Topic

LABEL = {"primary_act": "find", "targets": [], "changes": [["set", "skin_type", "dry", "eq"]]}


def _got(changes, provenance=None, active=None):
    out = {"primary_act": "find", "targets": [], "changes": changes}
    if provenance is not None:
        out["change_provenance"] = provenance
    if active is not None:
        out["change_active"] = active
    return out


# --- D1 -------------------------------------------------------------------------------------------


def test_an_unlabelled_hypothesis_is_neutral_an_unlabelled_fact_is_a_false_positive():
    hypothesis = rp.compare(
        LABEL,
        _got(
            [["set", "skin_type", "dry", "eq"], ["add", "product_type", "crema", "eq"]],
            ["explicit", "implicit"],
        ),
    )
    assert (hypothesis["change_hits"], hypothesis["change_emitted"]) == (1, 1)
    assert hypothesis["neutral_hypotheses"] == 1 and hypothesis["change_emitted_all"] == 2

    fact = rp.compare(
        LABEL,
        _got(
            [["set", "skin_type", "dry", "eq"], ["add", "product_type", "crema", "eq"]],
            ["explicit", "explicit"],
        ),
    )
    assert (fact["change_hits"], fact["change_emitted"], fact["neutral_hypotheses"]) == (1, 2, 0)


def test_a_hypothesis_that_matches_the_label_is_a_hit():
    v = rp.compare(LABEL, _got([["set", "skin_type", "dry", "eq"]], ["inferred"]))
    assert (v["change_hits"], v["change_emitted"], v["neutral_hypotheses"]) == (1, 1, 0)
    assert (v["hypotheses"], v["hypotheses_contradicted"]) == (1, 0)


def test_a_hypothesis_naming_another_value_on_a_labelled_dimension_is_contradicted():
    v = rp.compare(LABEL, _got([["set", "skin_type", "oily", "eq"]], ["implicit"]))
    assert (v["hypotheses"], v["hypotheses_contradicted"]) == (1, 1)
    assert v["change_emitted"] == 0  # neutră în F1, numărată în precizia ipotezelor
    other_dim = rp.compare(LABEL, _got([["add", "concerns", "dry", "eq"]], ["implicit"]))
    assert other_dim["hypotheses_contradicted"] == 0  # altă dimensiune: nu contrazice


def test_null_valued_hypotheses_are_neutral_too():
    v = rp.compare(
        {"primary_act": "find", "targets": [], "changes": []},
        _got([["add", "unmapped", None, "eq"]], ["implicit"]),
    )
    assert (v["null_emitted"], v["null_emitted_all"], v["neutral_hypotheses"]) == (0, 1, 1)


def test_without_provenance_nothing_changes():
    """Etichetele, testele de dinainte și rapoartele fără `checked` se numără exact ca înainte."""
    got = _got([["set", "skin_type", "dry", "eq"], ["add", "product_type", "crema", "eq"]])
    v = rp.compare(LABEL, got)
    assert v["change_emitted"] == v["change_emitted_all"] == 2
    misaligned = rp.compare(LABEL, {**got, "change_provenance": ["implicit"]})
    assert misaligned["change_emitted"] == 2


def test_a_model_answering_the_label_still_scores_one():
    v = rp.compare(LABEL, _got([["set", "skin_type", "dry", "eq"]], ["explicit"], [False]))
    assert v["change_hits"] == v["change_labelled"] == v["change_emitted"] == 1


# --- D3 -------------------------------------------------------------------------------------------


def test_a_set_on_the_already_active_value_is_neutral():
    v = rp.compare(
        LABEL,
        _got(
            [["set", "skin_type", "dry", "eq"], ["set", "category", "ten", "eq"]],
            ["explicit", "explicit"],
            [False, True],
        ),
    )
    assert (v["change_hits"], v["change_emitted"], v["neutral_active"]) == (1, 1, 1)


def _checked(op, dimension, value, provenance="explicit", relation="eq"):
    change = StateChange(
        op=op,
        target=None,
        dimension=dimension,
        relation=relation,
        value=value,
        number=None,
        unit=None,
        relative_to=None,
        quote=value or "",
    )
    return CheckedChange(
        change=change,
        dimension=dimension,
        canonical_value=value,
        provenance=provenance,
        strength="soft",
        rejected=None,
    )


def _interp():
    return TurnInterpretation(
        thread="continue",
        acts=[Act(kind="find", targets=[], query=None)],
        changes=[],
        references=[],
        ambiguities=[],
        corrects_previous_turn=False,
    )


def test_observed_marks_provenance_and_the_already_active_values():
    state = ConversationStateV2(
        topic=Topic(category_key="ten"),
        needs=(Need(key="skin_type", operator="eq", normalized_value="dry"),),
    )
    checked = [
        _checked("set", "category", "ten"),  # raftul deja activ ⇒ activ
        _checked("set", "skin_type", "dry", "implicit"),  # nevoia deja activă ⇒ activ
        _checked("set", "category", "par"),  # alt raft ⇒ judecat
        _checked("remove", "skin_type", "dry"),  # o retragere nu e un `set` ⇒ judecată
        _checked("set", "skin_type", "dry", relation="avoid"),  # o excludere ⇒ judecată
    ]
    got = rp.observed_parts(_interp(), checked, (), state)
    assert got["change_provenance"] == ["explicit", "implicit", "explicit", "explicit", "explicit"]
    assert got["change_active"] == [True, True, False, False, False]


def test_price_and_numeric_bounds_are_never_already_active():
    """Limitele stau pe chei proprii (`budget_*`), deci D3 nu le atinge: rămân judecate."""
    state = ConversationStateV2(
        needs=(Need(key="budget_max", operator="lte", normalized_value=100),)
    )
    assert not rp.already_active(state, "price", "100")


# --- rapoartele vechi -----------------------------------------------------------------------------


def test_old_reports_recover_provenance_from_the_validator_verdict():
    got = _got([["set", "skin_type", "dry", "eq"], ["add", "product_type", "crema", "eq"]])
    checked = [
        {"provenance": "explicit", "rejected": None},
        {"provenance": "explicit", "rejected": "unknown_dimension"},  # respinsă: nu e în `changes`
        {"provenance": "implicit", "rejected": None},
    ]
    assert rp.with_provenance(got, checked)["change_provenance"] == ["explicit", "implicit"]
    assert "change_provenance" not in rp.with_provenance(got, checked[:1])  # nealiniat ⇒ neatins


def test_summary_reports_both_rules_and_the_hypothesis_precision():
    def row(verdict):
        return {
            "arm": "none",
            "outcome": "ok",
            "verdict": verdict,
            "event": {"provenance": {}, "rejected": {}, "unknown_reference": 0},
            "observed": {},
            "ms": 1.0,
            "cost_usd": 0.0,
            "first_divergence": None,
        }

    v = rp.compare(
        LABEL,
        _got(
            [["set", "skin_type", "dry", "eq"], ["add", "product_type", "crema", "eq"]],
            ["explicit", "implicit"],
        ),
    )
    arm = rp.summarize([row(v)], ("none",))["arms"]["none"]
    assert arm["changes"]["f1"] == 1.0
    assert arm["changes"]["f1_all_emitted"] == round(2 * 0.5 * 1 / 1.5, 3)
    assert arm["hypotheses"]["n"] == 1 and arm["hypotheses"]["contradicted"] == 0
    assert rp.THRESHOLDS["hypotheses_contradicted_max"] == 0.15
