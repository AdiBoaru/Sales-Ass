"""NX-370 — o rafinare nu mai pierde produsul de pe ecran care o împlinește.

Conversația c8 din 2026-10-01: «ce șampon îmi recomanzi pt păr creț și uscat» a arătat LEE
STAFFORD Moisture Burst (fără sulfați), iar la «fără sulfați te rog» modelul a cerut
`features=["sulfate_free"]` (cod inventat, zero produse). Doi pași s-au compus:
1. `features` nerostit se relaxa ULTIMUL, deci scara arunca întâi nevoia reală (păr uscat) și
   raftul;
2. prima pagină a oricărei căutări noi excludea ce era pe ecran, deci Moisture Burst, primul în
   pool, nu ajungea pe pagină. Pe c10 aceeași excludere golea pagina la «cât costă și e în stoc?».
"""

from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from src.config import get_settings
from src.tools import catalog_tools as ct


@pytest.fixture
def provenance_on(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "search_relax_by_provenance_enabled", True)
    monkeypatch.setattr(s, "search_sort_mode_enabled", True)
    return s


def _fields(steps):
    """Ce filtre mai are fiecare treaptă (pentru a vedea ORDINEA relaxării)."""
    return [
        tuple(k for k in ("features", "facet_filters", "category") if step.get(k)) for step in steps
    ]


def test_unuttered_features_relax_first(provenance_on):
    steps = ct._relax_ladder(
        price_max=None,
        facet_filters={"concerns": ["hair_dryness"]},
        category=["par"],
        in_stock_only=False,
        features=["sulfate_free"],
        facets_uttered=True,
        category_uttered=True,
        features_uttered=False,
    )
    order = _fields(steps)
    assert order[0] == ("features", "facet_filters", "category")
    assert order[1] == ("facet_filters", "category")  # codul inventat pleacă primul


def test_uttered_features_still_relax_last(provenance_on):
    steps = ct._relax_ladder(
        price_max=None,
        facet_filters={"concerns": ["acne"]},
        category=None,
        in_stock_only=False,
        features=["niacinamida"],
        facets_uttered=False,
        features_uttered=True,
    )
    assert _fields(steps)[-1] == ()
    assert _fields(steps)[-2] == ("features",)  # „cu niacinamidă" rostit rămâne cel mai mult


def test_without_provenance_the_old_order_holds(monkeypatch):
    monkeypatch.setattr(get_settings(), "search_relax_by_provenance_enabled", False)
    steps = ct._relax_ladder(
        price_max=None,
        facet_filters={"concerns": ["x"]},
        category=None,
        in_stock_only=False,
        features=["y"],
        features_uttered=False,
    )
    assert _fields(steps)[-2] == ("features",)


def _ctx(body: str) -> NS:
    return NS(message=NS(body=body))


@pytest.mark.parametrize(
    "body, excluded",
    [
        ("fara sulfati te rog", False),  # rafinare: afișatele rămân în joc
        ("cat costa si e in stoc?", False),  # întrebare despre produsul afișat (c10)
        ("mai arata-mi", True),
        ("nu ai altele?", True),
        ("mai arată-mi alte produse", True),
    ],
)
def test_first_page_excludes_shown_only_when_others_are_asked(body, excluded):
    seen = {"p1"}
    assert (ct._first_page_excludes(_ctx(body), seen, planned=False) == seen) is excluded


def test_planned_search_never_excludes_on_its_first_page():
    assert ct._first_page_excludes(_ctx("mai arata-mi"), {"p1"}, planned=True) == set()


def test_kill_switch_restores_unconditional_exclusion(monkeypatch):
    monkeypatch.setattr(get_settings(), "search_first_page_keeps_shown_enabled", False)
    assert ct._first_page_excludes(_ctx("fara sulfati"), {"p1"}, planned=False) == {"p1"}
