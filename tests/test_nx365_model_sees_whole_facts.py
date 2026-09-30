"""NX-365 — modelul vede faptele ÎNTREGI: toate ingredientele și disponibilitatea.

Turul real `e9ec19c9` (`sole-ro`, 2026-09-30, «nu vreau cu acid hialuronic»): botul a scris «Am
selectat doar variante fără acid hialuronic» și a arătat DR.JART+ Cicapair, care îl are. Modelul
nu mințise: vedea primele 4 ingrediente din 12, iar acidul hialuronic e al 7-lea. În același tur,
un produs epuizat a fost recomandat ca „varianta mai accesibilă”, fiindcă lista trimisă modelului
nu spunea nimic despre disponibilitate.
"""

from src.agent.finalize import _rich_bundle
from src.agent.prompt_builder import PromptInputs, build_rich_system
from src.domain.pack import FacetSpec
from src.worker import compose

INGREDIENTS = FacetSpec(key="key_ingredients", labels={"ro": "Ingrediente cheie"})

#: Lista REALĂ din catalog, în ordinea din catalog (DR.JART+ Cicapair, `d534d6ed`).
DR_JART = [
    "centella asiatica",
    "extract",
    "asiaticozid",
    "acid madecassic",
    "acid asiatic",
    "allantoina",
    "acid hialuronic",
    "betaina",
    "glicerina",
    "pantenol",
    "palmitoyl tripeptide-8",
    "complex r-protector",
]


def _product(**kw):
    base = {
        "id": "d534d6ed",
        "name": "DR.JART+ Cicapair Intensive Soothing Repair Treatment Lotion",
        "price": 179.0,
        "rating": 4.8,
        "availability": "in_stock",
        "attributes": {"key_ingredients": list(DR_JART)},
    }
    base.update(kw)
    return base


def test_the_model_sees_every_ingredient():
    bundle = _rich_bundle([_product()], (INGREDIENTS,), "ro")
    assert "acid hialuronic" in bundle  # al 7-lea; pe `main` se oprea la „acid madecassic”
    assert "complex r-protector" in bundle


def test_the_client_table_still_shows_four():
    """Tăierea la 4 rămâne pentru OCHI (tabelul de comparație, fișa de detaliu)."""
    cell = compose._facet_cell(INGREDIENTS, {"key_ingredients": DR_JART}, "ro")
    assert cell == "centella asiatica, extract, asiaticozid, acid madecassic"
    summary = compose.facet_summary(_product(), (INGREDIENTS,), "ro")
    assert "acid hialuronic" not in summary


def test_an_unavailable_product_is_marked_for_the_model():
    for availability in ("out_of_stock", "discontinued"):
        bundle = _rich_bundle([_product(availability=availability)], (INGREDIENTS,), "ro")
        assert "disponibilitate: EPUIZAT" in bundle
    assert "EPUIZAT" not in _rich_bundle([_product()], (INGREDIENTS,), "ro")
    assert "EPUIZAT" not in _rich_bundle([_product(availability=None)], (INGREDIENTS,), "ro")


def test_the_rules_say_what_the_marker_and_the_whole_lists_mean():
    system = build_rich_system(PromptInputs.build("SOLE", "ecommerce", "ro", ["Ten"], []))
    assert "disponibilitate: EPUIZAT" in system
    # Recenzia NX-365: „Ingrediente cheie” nu e compoziția. 442 de produse SOLE au acid hialuronic
    # în compoziție fără să fie printre ingredientele cheie, deci absența nu dovedește nimic.
    assert "NU e compoziția completă" in system
    assert "nu afirma niciodată" in system
    assert "îl păstrezi în\n  `items`" in system


def test_numbers_from_the_whole_list_may_be_spoken():
    """Recenzia NX-365: modelul vede acum lista întreagă, deci o cifră de pe poziția 6 («vitamina
    B5») e întemeiată; altfel `scrub_intro` arunca fraza care o numea."""
    product = _product(attributes={"key_ingredients": [*DR_JART[:5], "vitamina b5"]})
    assert "5" in compose.spec_numbers([product], (INGREDIENTS,), "ro")
