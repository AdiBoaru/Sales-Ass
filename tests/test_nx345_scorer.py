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


def test_an_implicit_hypothesis_that_matches_the_label_is_a_hit():
    v = rp.compare(LABEL, _got([["set", "skin_type", "dry", "eq"]], ["implicit"]))
    assert (v["change_hits"], v["change_emitted"], v["neutral_hypotheses"]) == (1, 1, 0)
    assert (v["hypotheses"], v["hypotheses_contradicted"]) == (1, 0)


def test_an_inferred_change_never_matches_because_the_state_never_carries_it():
    """Recenzia NX-345a (P1): `inferred` e doar semnal de ordonare, nepersistat (I23). Numărat ca
    succes, un prompt care slăbește citatele ar urca F1 fără ca kernelul să scrie nimic."""
    v = rp.compare(LABEL, _got([["set", "skin_type", "dry", "eq"]], ["inferred"]))
    assert (v["change_hits"], v["change_emitted"], v["neutral_hypotheses"]) == (0, 0, 1)
    gamed = rp.compare(
        {"primary_act": "find", "targets": [], "changes": [["set", "skin_type", "dry", "eq"]]},
        _got(
            [
                ["set", "skin_type", "dry", "eq"],
                ["set", "category", "machiaj", "eq"],
                ["set", "brand", "x", "eq"],
            ],
            ["inferred", "inferred", "inferred"],
        ),
    )
    assert gamed["change_hits"] == 0  # recall 0: nimic din ce a cerut eticheta nu e în stare
    assert gamed["change_emitted"] == 0  # nici raftul `inferred` nu intră în stare (I23)


def test_an_implicit_shelf_switch_is_a_fact_not_a_hypothesis():
    """Recenzia NX-345a (P1): pe raft, `implicit` devine `set_topic`, parchează subiectul, iar
    plannerul caută pe raftul nou. Neetichetat e un pozitiv fals, nu o ipoteză neutră."""
    v = rp.compare(
        {"primary_act": "find", "targets": [], "changes": []},
        _got([["set", "category", "machiaj", "eq"]], ["implicit"]),
    )
    assert (v["change_emitted"], v["neutral_hypotheses"], v["hypotheses"]) == (1, 0, 0)


def test_facts_match_before_hypotheses_so_order_does_not_move_the_numbers():
    same = [["set", "skin_type", "dry", "eq"], ["set", "skin_type", "dry", "eq"]]
    first = rp.compare(LABEL, _got(same, ["explicit", "implicit"]))
    second = rp.compare(LABEL, _got(same, ["implicit", "explicit"]))
    keys = ("change_hits", "change_emitted", "neutral_hypotheses")
    assert [first[k] for k in keys] == [second[k] for k in keys] == [1, 1, 1]


def test_a_number_hypothesis_where_the_label_wants_a_relative_price_is_contradicted():
    """Recenzia NX-345a: eticheta cere o limită relativă (valoare nulă), modelul pune un număr.
    Neutru în F1, dar contrazis: prețul copiat din istoric e exact riscul acesta."""
    v = rp.compare(
        {"primary_act": "find", "targets": [], "changes": [["set", "price", None, "lte"]]},
        _got([["set", "price", "110", "lte"]], ["implicit"]),
    )
    assert (v["hypotheses"], v["hypotheses_contradicted"]) == (1, 1)
    assert v["number_for_relative"]


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


def _state(strength="soft", confirmed=False):
    return ConversationStateV2(
        topic=Topic(category_key="ten"),
        needs=(
            Need(
                key="skin_type",
                operator="eq",
                normalized_value="dry",
                strength=strength,
                confirmed=confirmed,
            ),
        ),
    )


def test_observed_marks_provenance_and_the_already_active_values():
    checked = [
        _checked("set", "category", "ten"),  # raftul deja activ ⇒ activ
        _checked("set", "skin_type", "dry", "implicit"),  # o ipoteză pe nevoia activă ⇒ activ
        _checked("remove", "skin_type", "dry"),  # o retragere nu e un `set` ⇒ judecată
        _checked("set", "skin_type", "dry", relation="avoid"),  # o excludere ⇒ judecată
    ]
    got = rp.observed_parts(_interp(), checked, (), _state())
    assert got["change_provenance"] == ["explicit", "implicit", "explicit", "explicit"]
    assert got["change_active"] == [True, True, False, False]


def test_an_explicit_reaffirmation_of_a_soft_need_is_a_change():
    """Recenzia NX-345a: reducerul întărește nevoia `soft` la `hard` și o confirmă, deci nu e
    „nimic". Pe o nevoie deja `hard` și confirmată, e."""
    change = [_checked("set", "skin_type", "dry")]
    assert rp.observed_parts(_interp(), change, (), _state())["change_active"] == [False]
    hard = _state(strength="hard", confirmed=True)
    assert rp.observed_parts(_interp(), change, (), hard)["change_active"] == [True]


def test_nothing_is_already_active_on_a_turn_that_moves_the_subject():
    """Recenzia NX-345a: schimbarea subiectului retrage/parchează nevoile, deci re-adăugarea lor
    în același tur se aplică din nou."""
    checked = [_checked("set", "category", "par"), _checked("set", "skin_type", "dry", "implicit")]
    got = rp.observed_parts(_interp(), checked, (), _state())
    assert got["change_active"] == [False, False]
    resume = _interp().model_copy(update={"thread": "resume"})
    got = rp.observed_parts(resume, [_checked("set", "category", "ten")], (), _state())
    assert got["change_active"] == [False]


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
    assert arm["unlabelled_hypotheses"]["k"] == 1 and arm["provenance_unknown_turns"] == 0
    assert arm["changes"]["f1"] == 1.0
    assert arm["changes"]["f1_all_emitted"] == round(2 * 0.5 * 1 / 1.5, 3)
    assert arm["hypotheses"]["n"] == 1 and arm["hypotheses"]["contradicted"] == 0
    assert rp.THRESHOLDS["hypotheses_contradicted_max"] == 0.15


def test_regressions_are_reported_on_both_rules():
    """Poarta de zgomot NX-339 se judecă pe regula pe care a fost măsurat zgomotul (`all`)."""
    got = _got(
        [["set", "skin_type", "dry", "eq"], ["add", "product_type", "crema", "eq"]],
        ["explicit", "implicit"],
    )
    before = [
        {
            "turn_id": "t",
            "arm": "none",
            "verdict": rp.compare(LABEL, _got([["set", "skin_type", "dry", "eq"]])),
        }
    ]
    after = [{"turn_id": "t", "arm": "none", "verdict": rp.compare(LABEL, got)}]
    assert rp.regressions(before, after)["regressed"] == 0  # ipoteza e neutră pe regula nouă
    assert rp.regressions(before, after, rule="all")["regressed"] == 1  # pe cea veche nu


def test_a_hypothesis_with_the_labelled_value_is_redundant_not_contradicted():
    """Recenzia NX-345a (a doua trecere): un `inferred` egal cu eticheta nu se potrivește (starea
    nu-l poartă), dar nici nu contrazice; la fel un duplicat `implicit` al unui fapt potrivit."""
    inferred = rp.compare(LABEL, _got([["set", "skin_type", "dry", "eq"]], ["inferred"]))
    assert (inferred["hypotheses_contradicted"], inferred["hypotheses_redundant"]) == (0, 1)
    dup = rp.compare(
        LABEL,
        _got([["set", "skin_type", "dry", "eq"]] * 2, ["explicit", "implicit"]),
    )
    assert (dup["change_hits"], dup["hypotheses_contradicted"], dup["hypotheses_redundant"]) == (
        1,
        0,
        1,
    )


def test_nothing_is_already_active_after_a_clear_or_a_correction():
    hard = _state(strength="hard", confirmed=True)
    clear = StateChange(
        op="clear",
        target="topic",
        dimension=None,
        relation=None,
        value=None,
        number=None,
        unit=None,
        relative_to=None,
        quote="altceva",
    )
    cleared = CheckedChange(
        change=clear,
        dimension="topic",
        canonical_value=None,
        provenance="explicit",
        strength="soft",
        rejected=None,
    )
    got = rp.observed_parts(_interp(), [cleared, _checked("set", "skin_type", "dry")], (), hard)
    assert got["change_active"] == [False, False]
    fix = _interp().model_copy(update={"corrects_previous_turn": True})
    got = rp.observed_parts(fix, [_checked("set", "skin_type", "dry")], (), hard)
    assert got["change_active"] == [False]


def test_an_inferred_shelf_does_not_move_the_subject():
    hard = _state(strength="hard", confirmed=True)
    checked = [
        _checked("set", "category", "par", "inferred"),
        _checked("set", "skin_type", "dry"),
    ]
    assert rp.observed_parts(_interp(), checked, (), hard)["change_active"] == [False, True]
