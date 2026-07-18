"""NX-185 — QuerySpec (shadow): build din triaj + merger pur (owner unic) + fingerprint determinist.

Zero schimbare de comportament (shadow) → triaj-ul rămâne byte-identic (verificat de regresie).
"""

from types import SimpleNamespace

from src.agent.query_spec import build_query_spec, merge_query_spec


def _rd(filters, category=None):
    return SimpleNamespace(filters=filters, category_key=category)


def test_build_query_spec_from_triage_filters():
    spec = build_query_spec(_rd({"budget_max": 80, "concerns": ["oily"], "brand": "X"}, "creme"))
    assert spec.subject_category == "creme"
    facets = {(c.facet, c.op, c.strength) for c in spec.constraints}
    assert ("price", "lte", "hard") in facets
    assert ("concerns", "contains", "hard") in facets
    assert ("brand", "eq", "hard") in facets
    assert all(c.source == "current_turn" for c in spec.constraints)
    assert len(spec.hard()) == len(spec.constraints)  # toate din triaj = hard
    # filters lipsă → spec gol (robust)
    assert build_query_spec(_rd(None)).constraints == ()


def test_merge_current_wins_and_inherits():
    prev = build_query_spec(_rd({"budget_max": 100, "concerns": ["dry"]}, "creme"))
    cur = build_query_spec(_rd({"concerns": ["oily"]}, "creme"))  # aceeași categorie, nevoie nouă
    merged = merge_query_spec(prev, cur)
    prices = [c for c in merged.constraints if c.facet == "price"]
    assert prices and prices[0].source == "inherited"  # bugetul din prev persistă (nesuprascris)
    assert any(c.facet == "concerns" and c.value == "oily" for c in merged.constraints)


def test_merge_topic_switch_resets_inheritance():
    prev = build_query_spec(_rd({"budget_max": 100}, "creme"))
    cur = build_query_spec(_rd({"concerns": ["volum"]}, "sampoane"))  # categorie DIFERITĂ
    merged = merge_query_spec(prev, cur)
    assert all(c.facet != "price" for c in merged.constraints)  # topic switch → fără moștenire
    assert merged.subject_category == "sampoane"


def test_fingerprint_deterministic_order_independent():
    a = build_query_spec(_rd({"budget_max": 80, "concerns": ["oily"]}, "creme"))
    b = build_query_spec(_rd({"concerns": ["oily"], "budget_max": 80}, "creme"))
    assert a.fingerprint() == b.fingerprint()  # sortat → stabil indiferent de ordine


def test_fingerprint_no_raw_free_text_pii():
    # brand/suitable_for = text liber din triaj → valoarea NU apare brută în telemetrie (hash).
    rd = _rd({"brand": "SecretBrandXYZ", "suitable_for": "ion.popescu"}, "creme")
    fp = build_query_spec(rd).fingerprint()
    assert "SecretBrandXYZ" not in fp and "ion.popescu" not in fp
    assert "brand:eq:" in fp  # fațeta+op rămân vizibile pentru grupare
