"""NX-321 — un număr e buget doar dacă nu poartă unitatea ALTEI dimensiuni.

`price_bound_source` (NX-319) compara marginea de preț a modelului cu orice număr din mesaj, deci
«vreau crema de 100 ml» coroborea un `price_max=100` inventat, pe căutare ca și pe rutină.
Dimensiunea vine din registrul de unități al tenantului, nu din cuvinte în cod (P9/P11).
"""

from __future__ import annotations

import pytest

from src.agent.tool_executor import _safe_tool_args
from src.domain.constraints import EMPTY_UNITS, build_units, monetary_mentions
from src.tools.catalog_tools import PRICE_BOUND_SPOKEN_NOW, price_bound_source

UNITS = build_units(
    {
        "price": {"factors": {"lei": 1, "ron": 1, "bani": 0.01}, "canonical": "lei"},
        "volume": {"factors": {"ml": 1, "l": 1000}, "canonical": "ml"},
        "spf": {"factors": {"spf": 1, "ip": 1}, "canonical": "spf"},
    }
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("sub 100 lei", {100.0}),
        ("ceva sub 100", {100.0}),  # fără unitate: buget POSIBIL (forma dominantă)
        ("am 150 de lei", {150.0}),
        ("vreau crema de 100 ml", set()),
        ("100ml", set()),
        ("SPF 50 sub buget", set()),
        ("crema spf 50 sub 120", {120.0}),
        ("1.500 lei", {1.5, 1500.0}),  # ambele lecturi, ca `_message_numbers`
        ("", set()),
    ],
)
def test_monetary_mentions(text, expected):
    assert monetary_mentions(text, units=UNITS) == expected


def test_registru_gol_nu_exclude_nimic():
    assert monetary_mentions("100 ml", units=EMPTY_UNITS) == {100.0}


def test_price_bound_source_respinge_volumul():
    kwargs = {
        "texts": ["vreau crema de 100 ml"],
        "relative_request": False,
        "session_price_max": None,
    }
    assert price_bound_source(100, **kwargs) == PRICE_BOUND_SPOKEN_NOW  # comparația veche
    assert price_bound_source(100, units=UNITS, **kwargs) is None


def test_price_bound_source_fara_registru_e_neschimbat():
    kwargs = {"texts": ["ceva sub 80"], "relative_request": False, "session_price_max": None}
    assert price_bound_source(80, units=EMPTY_UNITS, **kwargs) == price_bound_source(80, **kwargs)


def test_tool_call_routine_plan_fara_text_al_clientului():
    """P12: nevoile pot conține sănătate sau sarcină; în analytics pleacă doar structura."""
    out = _safe_tool_args(
        "routine_plan",
        {
            "family": "fata",
            "concerns": ["sunt insarcinata si am acnee", "alergie la parfum"],
            "budget_max": 100,
            "anchor_id": "p-1",
            "moment": None,
        },
    )
    assert out == {
        "family": "fata",
        "moment": None,
        "has_anchor": True,
        "budget_max": 100,
        "n_needs": 2,
    }
    assert not any(isinstance(v, str) and "insarcinata" in v for v in out.values())
