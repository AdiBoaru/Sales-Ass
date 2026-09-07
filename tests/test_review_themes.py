"""NX-279 — rezumatul de recenzii e frecvență verificabilă, nu invenție.

Ce separă un pro derivat de unul inventat: se numără recenzii distincte (nu apariții), lauda vine
doar din recenzii care laudă, nuanța poartă polaritatea în frază (cu excluderi pentru negație),
promisiunile se numără dar nu se afișează fără ratificare, iar textul trece porțile P0 / cifre /
punctuație la BUILD. Cazurile sunt marcate (happy 1-3, edge 4-8, failure 9-13)."""

from __future__ import annotations

import json
import pathlib
from decimal import Decimal

from src.catalog.review_themes import (
    COPY,
    MIN_REVIEWS_PER_PRODUCT,
    ReviewRow,
    band,
    build_theme_matchers,
    compose,
    digest,
    evidence,
    label_problem,
    load_vocabulary,
    rule_id,
    select_themes,
)
from src.worker.text_scrub import has_medical_claim

ROOT = pathlib.Path(__file__).resolve().parent.parent
PACK = ROOT / "db" / "seed" / "domain_pack_sole_ro.json"

# Vocabular MINIM, în forma pe care o are `domain_pack.review_themes`. Nu e „vocabularul SOLE":
# e forma, cu o laudă, o nuanță cu negație și o promisiune neratificată.
VOCAB = {
    "version": "test.1",
    "themes": [
        {
            "key": "absorbs_fast",
            "kind": "praise",
            "labels": {"ro": "se absoarbe rapid", "en": "absorbs quickly"},
            "phrases": ["se absoarbe rapid", "se absoarbe instant"],
            "excludes": ["nu se absoarbe"],
        },
        {
            "key": "no_residue",
            "kind": "praise",
            "labels": {"ro": "nu lasă urme"},
            "phrases": ["nu lasa urme"],
        },
        {
            "key": "gentle",
            "kind": "praise",
            "promise": "claim",
            "labels": {"ro": "e blând cu pielea sensibilă"},
            "phrases": ["nu irita"],
        },
        {
            "key": "transfers",
            "kind": "nuance",
            "labels": {"ro": "se transferă"},
            "phrases": ["se transfera"],
            "excludes": ["nu se transfera"],
        },
    ],
}


def _rows(*specs: tuple[int | None, str]) -> list[ReviewRow]:
    return [ReviewRow(id=f"r{i:03}", rating=rt, body=body) for i, (rt, body) in enumerate(specs)]


def _build(rows, vocab=VOCAB, locale="ro"):
    v = load_vocabulary(vocab)
    d = digest("p1", rows, v, build_theme_matchers(v))
    return v, d, (compose(d, v, locale) if d else None)


# --- happy ----------------------------------------------------------------------------------------


def test_1_pro_is_distinct_reviews_with_share_and_summary_band():
    rows = _rows(
        (5, "Se absoarbe rapid și nu lasă urme. Chiar se absoarbe instant."),
        (5, "Se absoarbe rapid, perfect."),
        (5, "Se absoarbe rapid și nu lasă urme."),
        (4, "Nu lasă urme."),
        (5, "Exact ce căutam."),
        (5, "Textură ok."),
    )
    v, d, comp = _build(rows)
    counts = {t.key: t.reviews for t in d.themes}
    # O recenzie cu două fraze ale aceleiași teme se numără O dată.
    assert counts == {"absorbs_fast": 3, "no_residue": 3}
    assert comp.pro_keys == ("absorbs_fast", "no_residue")
    assert comp.top_pros == ("se absoarbe rapid", "nu lasă urme")
    # 3/6 = 50% → „cele mai multe"; ambele în aceeași bandă, o singură propoziție.
    assert comp.summary == "Cele mai multe recenzii spun că se absoarbe rapid și nu lasă urme."
    assert comp.top_cons == ()
    assert d.sentiment == Decimal("1.00")


def test_2_nuance_counts_only_the_positive_form_and_renders_as_con():
    rows = _rows(
        (5, "Se transferă pe mască, păcat."),
        (4, "Rujul se transferă puțin pe pahar."),
        (5, "Se transferă, dar culoarea e superbă."),
        (5, "Nu se transferă deloc, rezistă."),
        (5, "Nu se transferă."),
        (5, "Frumos."),
    )
    v, d, comp = _build(rows)
    assert {t.key: t.reviews for t in d.themes} == {"transfers": 3}
    assert comp.con_keys == ("transfers",)
    assert comp.top_cons == ("se transferă",)
    assert comp.summary == "Câteva recenzii menționează că se transferă."


def test_3_same_input_same_bytes_and_evidence_is_reconstructible():
    rows = _rows(*[(5, "Se absoarbe rapid.")] * 4, (5, "Nu lasă urme."), (5, "ok"))
    v, d1, c1 = _build(rows)
    _, d2, c2 = _build(list(reversed(rows)))
    assert d1 == d2 and c1 == c2
    e = evidence(d1, c1, v)
    assert e["rule_id"] == rule_id(v) == "review_themes.v1:test.1"
    assert e["themes"]["absorbs_fast"]["reviews"] == 4
    assert e["themes"]["absorbs_fast"]["sample"] == ["r000", "r001", "r002"]
    assert json.dumps(e, sort_keys=True) == json.dumps(evidence(d2, c2, v), sort_keys=True)


# --- edge -----------------------------------------------------------------------------------------


def test_4_praise_from_low_ratings_is_not_counted_but_nuance_is():
    rows = _rows(
        (2, "Nu se absoarbe rapid, rămâne gras. Se transferă."),
        (1, "Se absoarbe rapid? Deloc. Se transferă."),
        (3, "Se absoarbe rapid dar se transferă."),
        (5, "Bun."),
        (5, "Bun."),
    )
    v, d, comp = _build(rows)
    counts = {t.key: t.reviews for t in d.themes}
    # Lauda sub 4★ nu intră (și oricum prima e exclusă de „nu se absoarbe"); nuanța intră.
    assert "absorbs_fast" not in counts
    assert counts["transfers"] == 3
    assert comp.con_keys == ("transfers",)


def test_5_claim_theme_is_counted_but_withheld_until_ratified():
    rows = _rows(*[(5, "Nu irită deloc tenul sensibil.")] * 5)
    v, d, comp = _build(rows)
    assert {t.key: t.reviews for t in d.themes} == {"gentle": 5}
    assert comp is None  # nimic afișabil → niciun rând
    ratified = json.loads(json.dumps(VOCAB))
    ratified["themes"][2]["ratified"] = True
    v2, d2, comp2 = _build(rows, ratified)
    assert comp2.pro_keys == ("gentle",)
    assert comp2.summary == "Cele mai multe recenzii spun că e blând cu pielea sensibilă."
    # Reținerea e declarată cu motiv, nu tăcută.
    _, _, withheld = select_themes(d, v, "ro")
    assert withheld == [("gentle", "claim_unratified")]


def test_6_thresholds_min_reviews_and_min_share():
    # 2 recenzii cu tema, dintr-un total de 20 → sub MIN_THEME_REVIEWS.
    rows = _rows((5, "Se absoarbe rapid."), (5, "Se absoarbe rapid."), *[(5, "ok")] * 18)
    v, d, comp = _build(rows)
    assert comp is None
    # 3 din 40 = 7,5% → sub MIN_PRAISE_SHARE (10%), dar peste MIN_NUANCE_SHARE (5%).
    rows = _rows(*[(5, "Se absoarbe rapid.")] * 3, *[(5, "Se transferă.")] * 3, *[(5, "ok")] * 34)
    v, d, comp = _build(rows)
    assert comp.pro_keys == ()
    assert comp.con_keys == ("transfers",)
    assert ("absorbs_fast", "below_min_share") in comp.withheld


def test_7_locale_is_part_of_the_key():
    rows = _rows(*[(5, "Se absoarbe rapid și nu lasă urme.")] * 5)
    v, d, _ = _build(rows)
    en = compose(d, v, "en")
    # `no_residue` n-are etichetă în `en` → reținut, nu tradus din senin.
    assert en.pro_keys == ("absorbs_fast",)
    assert en.summary == "Most reviews say it absorbs quickly."
    assert ("no_residue", "no_label:en") in en.withheld
    assert compose(d, v, "de") is None  # locale fără șablon → niciun rând


def test_8_bands_and_ordering_by_count_then_pack_position():
    rows = _rows(
        *[(5, "Se absoarbe rapid.")] * 3,
        *[(5, "Nu lasă urme.")] * 6,
        (5, "ok"),
    )
    v, d, comp = _build(rows)
    # 6/10 → majority; 3/10 → many. Ordinea pro-urilor e după număr, nu după poziția în pachet.
    assert comp.pro_keys == ("no_residue", "absorbs_fast")
    assert comp.summary == (
        "Cele mai multe recenzii spun că nu lasă urme. Multe recenzii spun că se absoarbe rapid."
    )
    assert band(0.5) == "majority" and band(0.2) == "many" and band(0.19) == "minority"


# --- failure --------------------------------------------------------------------------------------


def test_9_under_min_reviews_no_digest():
    rows = _rows(*[(5, "Se absoarbe rapid.")] * (MIN_REVIEWS_PER_PRODUCT - 1))
    v, d, comp = _build(rows)
    assert d is None and comp is None


def test_10_labels_fail_closed_at_load():
    bad = {
        "version": "t",
        "themes": [
            {
                "key": "a",
                "kind": "praise",
                "labels": {"ro": "recomandat de dermatologi"},
                "phrases": ["x y"],
            },
            {"key": "b", "kind": "praise", "labels": {"ro": "ține 24 de ore"}, "phrases": ["x z"]},
            {
                "key": "c",
                "kind": "praise",
                "labels": {"ro": "fin — dar puternic"},
                "phrases": ["w z"],
            },
            {"key": "h", "kind": "praise", "labels": {"ro": "ok"}, "phrases": ["h h"]},
            # Aceeași frază ca `h` (deja acceptată) → coliziune. Fraza lui `a` NU e ocupată:
            # `a` a căzut înainte să-și înregistreze frazele, deci n-a rezervat nimic.
            {"key": "d", "kind": "praise", "labels": {"ro": "ok"}, "phrases": ["h h"]},
            {"key": "e", "kind": "weird", "labels": {"ro": "ok"}, "phrases": ["q"]},
            {"key": "f", "kind": "praise", "labels": {}, "phrases": ["q q"]},
            {"key": "g", "kind": "praise", "labels": {"ro": "ok"}, "phrases": []},
        ],
    }
    v = load_vocabulary(bad)
    assert [t.key for t in v.themes] == ["h"]
    reasons = dict(v.rejected)
    assert reasons["a"] == "label_medical_claim:ro"
    assert reasons["b"] == "label_digits:ro"
    assert reasons["c"] == "label_punctuation:ro"
    assert reasons["d"] == "phrase_collision:h"
    assert reasons["e"] == "bad_kind"
    assert reasons["f"] == "no_labels"
    assert reasons["g"] == "no_phrases"


def test_11_missing_version_empties_the_vocabulary():
    v = load_vocabulary({"themes": VOCAB["themes"]})
    assert v.themes == () and v.rejected == (("*", "missing_version"),)
    assert load_vocabulary(None).rejected == (("*", "missing"),)


def test_12_summary_never_carries_digits_or_forbidden_punctuation():
    for locale, copy in COPY.items():
        for key in ("majority", "many", "minority", "nuance"):
            text = copy[key].format(list="x")
            assert label_problem(text) is None, (locale, key, text)
            assert not has_medical_claim(text)


def test_14_specific_theme_outranks_generic_one_when_base_rates_are_known():
    """Măsurat pe prima rulare SOLE: „textură ușoară" e pe 88% din produse și înghesuia pro-urile
    specifice în `over_cap`. Cu rate de bază, cota se împarte la răspândire: 6/10 pe o temă
    universală pierde în fața lui 3/10 pe una rară."""
    rows = _rows(
        *[(5, "Se absoarbe rapid.")] * 3,
        *[(5, "Nu lasă urme.")] * 6,
        (5, "ok"),
    )
    v, d, by_count = _build(rows)
    assert by_count.pro_keys == ("no_residue", "absorbs_fast")
    rates = {"no_residue": 0.9, "absorbs_fast": 0.1}
    by_lift = compose(d, v, "ro", base_rates=rates)
    assert by_lift.pro_keys == ("absorbs_fast", "no_residue")
    # Benzile rezumatului rămân pe COTĂ, nu pe lift: „cele mai multe" e o afirmație despre produs.
    assert by_lift.summary == (
        "Cele mai multe recenzii spun că nu lasă urme. Multe recenzii spun că se absoarbe rapid."
    )
    e = evidence(d, by_lift, v, base_rates=rates)
    assert e["ordering"] == "specificity"
    assert e["themes"]["absorbs_fast"]["base_rate"] == 0.1
    # Fără rate: `share`, iar evidence-ul o spune.
    assert evidence(d, by_count, v)["ordering"] == "share"


def test_13_sole_pack_vocabulary_loads_clean():
    """Vocabularul real al tenantului trece toate porțile la load: nicio temă respinsă, nicio
    etichetă cu claim medical / cifre / punctuație, nicio coliziune de fraze."""
    pack = json.loads(PACK.read_text(encoding="utf-8"))
    v = load_vocabulary(pack.get("review_themes"))
    assert v.rejected == ()
    assert len(v.themes) >= 10
    kinds = {t.kind for t in v.themes}
    assert kinds == {"praise", "nuance"}
    # Promisiunile pleacă neratificate: reținerea e starea implicită, ratificarea e a omului.
    for t in v.themes:
        if t.promise == "claim":
            assert t.ratified is False, t.key
        assert t.label("ro") is not None, t.key
    matchers = build_theme_matchers(v)
    assert set(matchers) == {t.key for t in v.themes}
