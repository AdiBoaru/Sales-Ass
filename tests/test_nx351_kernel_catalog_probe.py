"""NX-351 — metricile sondei pe catalogul real (pure): adevărul acumulat din etichete, M1/M2 și
regula GO pre-înregistrată pe cazurile de margine. Zero model, zero DB."""

from __future__ import annotations

from scripts.nx351_kernel_catalog_probe import (
    Card,
    Truth,
    need_share,
    next_truth,
    pair_rule,
    subject_share,
    verdict,
)


def _label(*changes, uncertain=False):
    return {"changes": [list(c) for c in changes], "uncertain": uncertain}


def test_the_truth_accumulates_over_the_conversation():
    truth = next_truth(Truth(), _label(("set", "category", "ten", "eq")))
    truth = next_truth(truth, None)
    truth = next_truth(truth, _label(("set", "product_type", "ser de fata", "eq")))
    truth = next_truth(truth, _label(("add", "concerns", "acne", "eq")))
    assert truth == Truth("ser de fata", "ten", (("concerns", "acne"),))


def test_uncertain_labels_and_exclusions_change_nothing():
    base = Truth("ruj", None, ())
    assert next_truth(base, _label(("set", "product_type", "gloss", "eq"), uncertain=True)) == base
    assert next_truth(base, _label(("add", "brand", "x", "avoid"))) == base


def test_a_set_replaces_the_values_of_its_dimension_and_add_appends():
    truth = next_truth(Truth(), _label(("set", "skin_type", "dry", "eq")))
    truth = next_truth(truth, _label(("set", "skin_type", "oily", "eq")))
    truth = next_truth(truth, _label(("add", "concerns", "acne", "eq")))
    truth = next_truth(truth, _label(("add", "concerns", "redness", "eq")))
    assert truth.needs == (("skin_type", "oily"), ("concerns", "acne"), ("concerns", "redness"))


def test_a_new_subject_drops_the_needs_of_the_old_one():
    truth = Truth("crema de fata", "ten", (("concerns", "acne"),))
    moved = next_truth(
        truth,
        _label(("set", "product_type", "sampon", "eq"), ("add", "concerns", "dandruff", "eq")),
    )
    assert moved == Truth("sampon", "ten", (("concerns", "dandruff"),))
    shelf = next_truth(truth, _label(("set", "category", "par", "eq")))
    assert shelf == Truth(None, "par", ())


def test_the_price_and_unmapped_words_are_not_needs():
    truth = next_truth(
        Truth(), _label(("set", "price", "100", "lte"), ("add", "unmapped", "x", "eq"))
    )
    assert not truth.known


def test_m1_uses_the_type_first_then_the_shelf_subtree():
    cards = [
        Card("crema de fata", "ten/ten-ingrijire"),
        Card("ser de fata", "ten/ten-ingrijire"),
        Card("ruj", "machiaj/buze"),
    ]
    assert subject_share(Truth("crema de fata", "ten"), cards, "ten") == 1 / 3
    assert subject_share(Truth(None, "ten"), cards, "ten") == 2 / 3
    assert subject_share(Truth(None, "ten"), cards, "te") == 0.0  # prefixul nu e subarbore
    assert subject_share(Truth(), cards, None) is None
    assert subject_share(Truth("ruj"), [], None) == 0.0


def test_m2_averages_over_the_needs_and_reads_lists_and_booleans():
    cards = [
        Card(attributes={"concerns": ["acne", "redness"], "fragrance_free": True}),
        Card(attributes={"concerns": ["acne"], "fragrance_free": False}),
    ]
    truth = Truth(needs=(("concerns", "Acne"), ("concerns", "redness"), ("fragrance_free", "true")))
    assert need_share(truth, cards) == (1.0 + 0.5 + 0.5) / 3
    assert need_share(Truth(), cards) is None
    assert need_share(truth, []) == 0.0


def test_the_pair_rule_on_its_margins():
    # media exact la marjă trece; 2 ture pierdute trec, 3 nu; o pierdere de exact 0,5 nu se numără
    assert pair_rule([(0.95, 1.0)])["passed"] is True
    assert pair_rule([(0.94, 1.0)])["passed"] is False
    two = [(0.0, 1.0), (0.0, 1.0), (1.0, 0.0), (1.0, 0.0)]
    assert pair_rule(two) == {"n": 4, "kernel": 0.5, "v1": 0.5, "losing_turns": 2, "passed": True}
    three = [*two, (0.0, 1.0), (1.0, 0.0)]
    assert pair_rule(three)["losing_turns"] == 3 and pair_rule(three)["passed"] is False
    assert pair_rule([(0.5, 1.0), (1.0, 0.5)])["losing_turns"] == 0
    assert pair_rule([])["passed"] is None


def test_the_verdict_needs_every_rule():
    good = [(1.0, 1.0)]
    assert verdict(good, good, 2, 0)["verdict"] == "GO"
    assert verdict(good, good, 3, 0)["failed"] == ["M3_empty"]
    assert verdict(good, good, 0, 1)["verdict"] == "NO-GO"
    unmeasured = verdict([], good, 0, 0)
    assert unmeasured["verdict"] == "NOT-MEASURED" and unmeasured["unmeasured"] == ["M1_subject"]
