"""NX-182 — relaxed_constraints + disclosure determinist (funcții pure + gating).

OFF (kill-switch) → () + "" → byte-identic. ON → structura de constrângeri relaxate +
linia de disclosure per-locale (fără valori brute).
"""

from src.config import get_settings
from src.models import RelaxedConstraint, Relevance
from src.tools.catalog_tools import _relaxed_constraints
from src.worker.compose import _relaxed_disclosure


def test_relaxed_constraints_from_steps():
    base = {
        "price_max": 80,
        "concerns": ["oily"],
        "category": "creme",
        "features": ["niacinamide"],
        "in_stock_only": False,
    }
    # treapta câștigătoare a relaxat concerns + category (None), a păstrat price + features
    winning = {**base, "concerns": None, "category": None}
    rc = _relaxed_constraints([base, {}, winning], winning, relax_depth=2)
    assert {c.facet_key for c in rc} == {"concerns", "category"}
    assert all(c.relaxed_step == 2 for c in rc)
    # concerns → value normalizată (join listă)
    assert any(c.facet_key == "concerns" and c.original_value == "oily" for c in rc)
    # nimic relaxat (base == winning) → ()
    assert _relaxed_constraints([base], base, 0) == ()
    # fără treaptă câștigătoare → ()
    assert _relaxed_constraints([base], None, 0) == ()


def test_relaxed_disclosure_gated_and_localized(monkeypatch):
    rel = Relevance(relaxed=True, relaxed_constraints=(RelaxedConstraint("concerns", 1, "oily"),))
    # OFF → gol (byte-identic)
    monkeypatch.setattr(get_settings(), "relaxed_disclosure_enabled", False)
    assert _relaxed_disclosure(rel, "ro") == ""
    # ON → linie de disclosure per-locale
    monkeypatch.setattr(get_settings(), "relaxed_disclosure_enabled", True)
    out_ro = _relaxed_disclosure(rel, "ro")
    assert "relaxat" in out_ro.lower() and "nevoia cerută" in out_ro
    out_en = _relaxed_disclosure(rel, "en")
    assert "relaxed" in out_en.lower() and "requested need" in out_en
    # None / gol → "" chiar și ON
    assert _relaxed_disclosure(None, "ro") == ""
    assert _relaxed_disclosure(Relevance(), "ro") == ""


def test_relaxed_disclosure_dedups_and_multi_facet(monkeypatch):
    monkeypatch.setattr(get_settings(), "relaxed_disclosure_enabled", True)
    rel = Relevance(
        relaxed_constraints=(
            RelaxedConstraint("concerns", 2, "oily"),
            RelaxedConstraint("category", 2, "creme"),
        )
    )
    out = _relaxed_disclosure(rel, "ro")
    assert "nevoia cerută" in out and "categoria cerută" in out
