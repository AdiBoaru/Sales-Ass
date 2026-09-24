"""Motivul de card cu „SPF 40" — conversația reală `cd98a513` (`sole-ro`, 2026-09-24), turul 2.

Modelul a scris motive pentru PURITO («…și SPF 30…») și Dear Klairs («…și SPF 40…»), iar
`scrub_prose` le-a aruncat întregi: cardurile au ieșit fără motiv. SPF-ul e pe fișa fiecăruia, în
nume și în `key_ingredients`. Poarta compară acum PERECHEA etichetă + număr cu fișa produsului.
Pur: fără DB, fără rețea.
"""

from __future__ import annotations

import pytest

from src.config import get_settings
from src.worker.compose import grounded_identifiers, scrub_prose
from src.worker.text_scrub import has_unverifiable_claim, labeled_quantities

KLAIRS = {
    "name": "Dear Klairs Illuminating Supple Blemish, 40 gr - Crema coloranta de fata formulata "
    "cu factor SPF 40 si pigmenti corectori",
    "attributes": {"spf": 40, "key_ingredients": ["factor spf 40", "pigmenti corectori"]},
}
PURITO = {
    "name": "PURITO Wonder Releaf Centella BB Cream, 15 Rose Ivory, 30 ml",
    "attributes": {"key_ingredients": ["centella asiatica", "protectie solara spf 30 pa+++"]},
}


@pytest.fixture(autouse=True)
def _flag_on(monkeypatch):
    monkeypatch.setattr(get_settings(), "labeled_quantity_grounding_enabled", True)


def test_pairs_are_label_and_number_both_ways() -> None:
    pairs = labeled_quantities("cu SPF 40 si 30 ml")
    assert {"spf 40", "40 si", "30 ml"} <= pairs
    assert "40" not in pairs and "30" not in pairs  # numărul singur nu e fapt


def test_the_real_reasons_survive() -> None:
    reason = "Potrivită pentru roșeață, cu pigmenți corectori și SPF 40 pentru un ten uniform."
    assert scrub_prose(reason, grounded_identifiers(KLAIRS)) == reason
    reason = "Bună pentru calmare, cu centella, niacinamide și SPF 30."
    assert scrub_prose(reason, grounded_identifiers(PURITO)) == reason


def test_a_number_the_sheet_does_not_carry_still_drops_the_reason() -> None:
    """SPF 50 nu e pe fișa PURITO, iar „40 lei" nu e pe nicio fișă (obiecția NX-313 rămâne)."""
    assert scrub_prose("Cu SPF 50, pentru calmare.", grounded_identifiers(PURITO)) is None
    assert scrub_prose("Doar 40 lei, cu SPF 40.", grounded_identifiers(KLAIRS)) is None


def test_another_products_fact_does_not_ground_this_one() -> None:
    assert scrub_prose("Cu SPF 40, pentru un ten uniform.", grounded_identifiers(PURITO)) is None


def test_identifiers_keep_working() -> None:
    """NX-313 neatins: „complex v11" de pe fișă trece, cifra liberă nu."""
    grounded = frozenset({"v11"})
    assert not has_unverifiable_claim("cu complex v11 pentru bariera", grounded)
    assert has_unverifiable_claim("cu complex v11 si 12 ore de hidratare", grounded)


def test_flag_off_is_nx313(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "labeled_quantity_grounding_enabled", False)
    reason = "Potrivită pentru roșeață, cu pigmenți corectori și SPF 40 pentru un ten uniform."
    assert scrub_prose(reason, grounded_identifiers(KLAIRS)) is None
