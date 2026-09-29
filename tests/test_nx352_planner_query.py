"""NX-352 — plannerul caută REZIDUUL cererii, nu fraza clientului (`kernel.v5.0`).

Sonda NX-351 pe catalogul real: 64 din 81 de planuri `search` aveau fraza clientului ca `query`, iar
pe treapta `strict` fiecare cuvânt e o poartă (lecția NX-298): «si ceva mai ieftin ?» dădea zero
produse, «pai mi se usuca pielea dupa dus» potriviri pe «dus» (loțiuni de corp). Reziduul scoate
formula locale-i (tabelele `query_terms`) și cuvintele unei nevoi purtate deja de un filtru sau de
o preferință; numele subiectului rămâne întreg. Cazurile sunt formele reale ale sondei. Zero model,
zero DB."""

from __future__ import annotations

from tests.test_kernel_planner import (
    SOLE_SHELF,
    _interp,
    _need,
    _plan,
    _search,
    _state,
)


def _args(query, changes=(), *, needs=(), shelf=SOLE_SHELF, product_type=None, name="sole-ro"):
    interp = _interp(acts=[{"kind": "find", "query": query}], changes=list(changes))
    state = _state(shelf, needs=tuple(needs), product_type=product_type)
    return _search(_plan(name, interp, state))


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
        },
        {
            "op": "set",
            "dimension": "skin_type",
            "relation": "eq",
            "value": "dry",
            "quote": "ten uscat",
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
    """Pe SOLE marca e coloană: o marcă soft ajunge doar în `gaps`, deci cuvântul ei rămâne text,
    altfel s-ar pierde de tot."""
    change = {
        "op": "set",
        "dimension": "brand",
        "relation": "eq",
        "value": "cerave",
        "quote": "cerave",
    }
    args = _args(
        "ceva de la cerave",
        [change],
        needs=[_need("brand", "cerave", source="user_implicit")],
    )
    assert args.query == "cerave" and args.brand is None


def test_without_residue_or_subject_the_whole_request_is_the_last_resort():
    """Nimic de căutat în afara formulei și fără subiect: cererea întreagă, ca înainte (nu
    tăcere)."""
    price = {"op": "set", "dimension": "price", "relation": "lte", "quote": "mai ieftin"}
    args = _args("ceva mai ieftin", [price], shelf=None)
    assert args.query == "ceva mai ieftin"
