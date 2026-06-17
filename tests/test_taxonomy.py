"""NX-72 — taxonomie concern→cheie de filtru (clasificator determinist, fără LLM)."""

from src.tools.taxonomy import _norm, map_concerns


def test_map_known_concern():
    assert map_concerns("beauty", ["ten gras"]) == ["oily"]
    assert map_concerns("beauty", ["piele sensibilă"]) == ["sensitive"]
    assert map_concerns("beauty", ["acnee"]) == ["acne"]


def test_unknown_concern_ignored():
    # Necunoscut → ignorat (NU produce filtru fals care ar goli rezultatul).
    assert map_concerns("beauty", ["frigider"]) == []
    assert map_concerns("beauty", ["ten gras", "frigider"]) == ["oily"]


def test_normalization_case_and_diacritics():
    assert _norm("Ten Grăs") == "ten gras"
    assert map_concerns("beauty", ["TEN GRAS"]) == ["oily"]
    assert map_concerns("beauty", ["Piele Grasă"]) == ["oily"]


def test_dedupe_and_stable_order():
    # Sinonime către aceeași cheie → unic; ordine stabilă (sortată).
    assert map_concerns("beauty", ["ten gras", "piele grasă"]) == ["oily"]
    assert map_concerns("beauty", ["riduri", "acnee"]) == ["acne", "anti_aging"]


def test_empty_and_none():
    assert map_concerns("beauty", None) == []
    assert map_concerns("beauty", []) == []


def test_unknown_vertical_returns_empty():
    # Vertical fără tabel → fără mapare, fără crash (tool merge pe query+category).
    assert map_concerns("hvac", ["ten gras"]) == []
    assert map_concerns("ecommerce", ["oily"]) == []
