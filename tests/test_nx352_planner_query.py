"""NX-352 — textul căutării se COMPUNE din ce a validat kernelul, nu din fraza clientului
(`kernel.v5.0`).

Sonda NX-351 pe catalogul real: 64 din 81 de planuri `search` aveau fraza clientului ca `query`, iar
pe treapta `strict` fiecare cuvânt e o poartă (lecția NX-298): «si ceva mai ieftin ?» dădea zero
produse, «pai mi se usuca pielea dupa dus» potriviri pe «dus» (loțiuni de corp). O variantă care
scădea din frază a picat la două recenzii; textul e acum subiectul numit (sau numele lui din stare),
altfel o fațetă filtrată, altfel termenii nemapați. Cazurile sunt formele reale ale sondei și ale
recenziilor. Zero model, zero DB."""

from __future__ import annotations

from src.agent.turn_planner import plan_turn
from src.conversation.interpretation import CheckedChange
from tests.kernel import fixture_catalog as fc
from tests.test_kernel_planner import (
    SOLE_SHELF,
    _gate,
    _interp,
    _need,
    _search,
    _state,
)


def _args(
    query,
    changes=(),
    *,
    needs=(),
    shelf=SOLE_SHELF,
    product_type=None,
    name="sole-ro",
    vocab=None,
):
    """Un tur prin plannerul real, cu schimbările VALIDATE (`checked`) ca în producție: fiecare
    schimbare compactă poate purta `provenance`, `matched` (cuvintele care au numit valoarea) și
    `rejected`."""
    specs = [dict(c) for c in changes]
    meta = [
        (s.pop("provenance", "explicit"), tuple(s.pop("matched", ())), s.pop("rejected", None))
        for s in specs
    ]
    interp = _interp(acts=[{"kind": "find", "query": query}], changes=specs)
    checked = [
        CheckedChange(
            change=change,
            dimension=change.dimension,
            canonical_value=change.value if change.number is None else change.number,
            provenance=provenance,
            strength="soft",
            rejected=rejected,
            matched=matched,
        )
        for change, (provenance, matched, rejected) in zip(interp.changes, meta, strict=True)
    ]
    planned = plan_turn(
        interp,
        _state(shelf, needs=tuple(needs), product_type=product_type),
        (),
        (),
        _gate(),
        changed=False,
        pack=fc.pack(name),
        vocab=vocab if vocab is not None else fc.vocabulary(name),
        locale="ro",
        checked=checked,
    )
    return _search(planned)


def test_a_relative_price_request_searches_the_subject_not_the_sentence():
    """«si ceva mai ieftin ?»: «si», «ceva», «mai» sunt cuvinte goale, «ieftin» e comparator."""
    price = {"op": "set", "dimension": "price", "relation": "lte", "quote": "mai ieftin"}
    args = _args("si ceva mai ieftin ?", [price], product_type="crema de fata")
    assert args.query != "si ceva mai ieftin ?"
    assert "ieftin" not in args.query


def test_a_described_need_is_not_the_search_text():
    """«pai mi se usuca pielea dupa dus»: nevoia (descrisă) e deja preferință; căutarea e
    subiectul."""
    change = {
        "op": "set",
        "dimension": "skin_type",
        "relation": "eq",
        "value": "dry",
        "quote": "mi se usuca pielea dupa dus",
        "provenance": "implicit",
    }
    needs = [_need("skin_type", "dry", source="user_implicit")]
    args = _args(
        "pai mi se usuca pielea dupa dus", [change], needs=needs, product_type="crema de fata"
    )
    assert "dus" not in args.query.split() and args.prefer.get("skin_type") == ["dry"]


def test_a_spoken_need_is_a_filter_and_its_words_leave_the_text():
    """«ai ceva anti aging?» cu subiect: nevoia spusă e filtru relaxabil (NX-352), deci cuvintele ei
    nu mai sunt și poartă de text."""
    change = {
        "op": "add",
        "dimension": "concerns",
        "relation": "eq",
        "value": "anti_aging",
        "quote": "anti aging",
        "matched": ["anti", "aging"],
    }
    args = _args(
        "ai ceva anti aging?",
        [change],
        needs=[_need("concerns", "anti_aging")],
        product_type="crema de fata",
    )
    assert (args.features or args.concerns) == ["anti_aging"]
    assert "aging" not in args.query


def test_an_unmapped_word_stays_the_search_text():
    """«Vreau produse de la gerovital»: cuvântul nemapat E căutarea; formula pleacă."""
    change = {
        "op": "add",
        "dimension": "unmapped",
        "relation": "contains",
        "value": "gerovital",
        "quote": "produse de la gerovital",
    }
    args = _args("Vreau produse de la gerovital", [change], shelf=None)
    assert args.query == "gerovital"


def test_the_subject_name_stays_whole_next_to_a_spoken_need():
    """«vreau o cremă de față pentru ten uscat»: numele subiectului rămâne întreg (cu cuvântul gol
    din el), nevoia spusă e filtru."""
    changes = [
        {
            "op": "set",
            "dimension": "product_type",
            "relation": "eq",
            "value": "crema de fata",
            "quote": "cremă de față",
            "matched": ["crema", "de", "fata"],
        },
        {
            "op": "set",
            "dimension": "skin_type",
            "relation": "eq",
            "value": "dry",
            "quote": "ten uscat",
            "matched": ["ten", "uscat"],
        },
    ]
    args = _args(
        "vreau o cremă de față pentru ten uscat",
        changes,
        needs=[_need("skin_type", "dry")],
        product_type="crema de fata",
    )
    assert args.query == "cremă de față" and args.concerns == ["dry"]


def test_a_brand_that_does_not_reach_the_search_keeps_its_word():
    """Pe SOLE marca e coloană: o marcă spusă, dar soft, ajunge doar în `gaps`, deci cuvântul ei
    rămâne text lângă subiect, altfel s-ar pierde de tot."""
    change = {
        "op": "set",
        "dimension": "brand",
        "relation": "eq",
        "value": "cerave",
        "quote": "cerave",
        "matched": ["cerave"],
    }
    args = _args("ceva de la cerave", [change], needs=[_need("brand", "cerave")])
    assert "cerave" in args.query.split() and args.brand is None


def test_without_anything_validated_the_whole_request_is_the_last_resort():
    """Nimic de căutat în afara formulei și fără subiect: cererea întreagă, ca înainte (nu
    tăcere)."""
    price = {"op": "set", "dimension": "price", "relation": "lte", "quote": "mai ieftin"}
    args = _args("ceva mai ieftin", [price], shelf=None)
    assert args.query == "ceva mai ieftin"


# --- recenzia NX-352 ------------------------------------------------------------------------------


def test_a_negated_word_is_never_a_text_gate():
    """Constatarea 1: «fără parfum» nu caută «parfum» (ar cere exact ce e ocolit)."""
    avoid = {
        "op": "add",
        "dimension": "unmapped",
        "relation": "avoid",
        "value": "parfum",
        "quote": "fara parfum",
    }
    args = _args("vreau o crema fara parfum", [avoid], shelf=None)
    assert "parfum" not in args.query and "crema" in args.query


def test_a_rejected_change_consumes_nothing():
    """Constatarea 2: citatul unei schimbări respinse de validator nu scoate cuvintele cererii."""
    rejected = {
        "op": "set",
        "dimension": "concerns",
        "relation": "eq",
        "value": "acne",
        "quote": "bumbac rosu",
        "matched": ["bumbac", "rosu"],
        "rejected": "semantic_mismatch",
    }
    args = _args("ceva din bumbac rosu", [rejected], needs=[_need("concerns", "acne")], shelf=None)
    assert "bumbac" in args.query and "rosu" in args.query


def test_a_wide_description_does_not_become_the_search_text():
    """Constatarea 3 (ambele recenzii): un citat larg de descriere nu mai e text; textul e
    subiectul numit, iar ingredientul spus e filtru."""
    changes = [
        {
            "op": "set",
            "dimension": "product_type",
            "relation": "eq",
            "value": "ser de fata",
            "quote": "un ser",
            "matched": ["ser"],
        },
        {
            "op": "add",
            "dimension": "key_ingredients",
            "relation": "eq",
            "value": "vitamina c",
            "quote": "vitamina c",
            "matched": ["vitamina", "c"],
        },
        {
            "op": "set",
            "dimension": "skin_type",
            "relation": "eq",
            "value": "oily",
            "quote": "un ser cu vitamina c pentru ten gras",
            "provenance": "implicit",
        },
    ]
    args = _args(
        "as vrea un ser cu vitamina c pentru ten gras",
        changes,
        needs=[
            _need("key_ingredients", "vitamina c"),
            _need("skin_type", "oily", source="user_implicit"),
        ],
        product_type="ser de fata",
    )
    assert args.query == "ser"
    assert "vitamina c" in [*(args.concerns or []), *(args.features or [])]


def test_an_absolute_comparator_is_formula_only_next_to_a_number():
    """Constatarea 7: «maxim 100 lei» pleacă întreg; «fix» dintr-un nume de produs rămâne."""
    price = {
        "op": "set",
        "dimension": "price",
        "relation": "lte",
        "number": 100,
        "unit": "lei",
        "quote": "maxim 100 lei",
        "matched": ["100"],
    }
    cream = {
        "op": "set",
        "dimension": "product_type",
        "relation": "eq",
        "value": "crema de fata",
        "quote": "o crema",
        "matched": ["crema"],
    }
    capped = _args(
        "o crema maxim 100 lei",
        [cream, price],
        needs=[_need("budget_max", 100.0, "hard")],
        shelf=None,
        product_type="crema de fata",
    )
    assert capped.query == "crema" and capped.price_max == 100.0
    # fără nimic validat, ultima rezervă e cererea întreagă: «fix» nu e aruncat ca comparator
    assert "fix" in _args("un spray fix pentru machiaj", shelf=None).query


def test_the_current_subject_is_protected_from_a_need_quote():
    """Constatarea 7: subiectul din turele de dinainte nu e tăiat de citatul unei nevoi care
    împarte un cuvânt cu el («fond de ten» lângă «ten uscat»)."""
    need = {
        "op": "set",
        "dimension": "skin_type",
        "relation": "eq",
        "value": "dry",
        "quote": "ten uscat",
        "matched": ["ten", "uscat"],
    }
    args = _args(
        "un fond de ten pentru ten uscat",
        [need],
        needs=[_need("skin_type", "dry")],
        product_type="fond de ten",
    )
    assert "fond" in args.query and "ten" in args.query.split() and "uscat" not in args.query


# --- a doua recenzie NX-352: textul se COMPUNE, nu se scade din frază -----------------------------


def test_function_words_and_price_adjectives_never_become_the_search():
    """«decât», «ieftină», «nu prea scump»: niciun cuvânt nevalidat nu mai e poartă de text."""
    cream = {
        "op": "set",
        "dimension": "product_type",
        "relation": "eq",
        "value": "crema de fata",
        "quote": "o crema",
        "matched": ["crema"],
    }
    assert _args("o crema ieftina", [cream], product_type="crema de fata").query == "crema"
    price = {"op": "set", "dimension": "price", "relation": "lte", "quote": "mai ieftin"}
    telefoane = _args("ceva mai ieftin decat astea", [price], product_type="crema de fata")
    assert "decat" not in telefoane.query and "ieftin" not in telefoane.query


def test_a_correction_keeps_the_withdrawn_value_out_of_the_text():
    """«ten gras, nu uscat» (`replace`): nici valoarea retrasă, nici cea nouă nu devin text lângă
    subiect; nevoia nouă e filtru."""
    replace = {
        "op": "replace",
        "target": "c1",
        "dimension": "skin_type",
        "relation": "eq",
        "value": "oily",
        "quote": "ten gras, nu uscat",
        "matched": ["ten", "gras"],
    }
    args = _args(
        "ten gras, nu uscat",
        [replace],
        needs=[_need("skin_type", "oily")],
        product_type="crema de fata",
    )
    assert "uscat" not in args.query and "gras" not in args.query
