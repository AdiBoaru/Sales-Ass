"""NX-169 — proiecția faptelor canonice în view-urile agentului (search/detail/compare).
Pur pe funcțiile de view (fără DB), cu un DomainPack fixture."""

import src.tools.catalog_tools as mod
from src.domain.pack import DomainPack, FacetSpec
from src.tools.catalog_tools import _brief, _compare_view, _detail_view, _facet_pairs


def _pack():
    return DomainPack(
        vertical="beauty_salon",
        comparison_facets=(
            FacetSpec(
                key="suitable_for",
                labels={"ro": "Potrivit pentru"},
                value_labels={"oily": {"ro": "ten gras"}},
            ),
            FacetSpec(
                key="finish",
                labels={"ro": "Finish"},
                value_labels={"matte": {"ro": "mat"}, "dewy": {"ro": "luminos"}},
            ),
            FacetSpec(key="key_ingredients", labels={"ro": "Ingrediente cheie"}),
        ),
    )


def _prod(**over):
    p = {
        "id": "1",
        "name": "Velora Fond",
        "brand": "Velora",
        "price": 59.9,
        "availability": "in_stock",
        "rating": 4.8,
        "attributes": {
            "suitable_for": ["oily"],
            "finish": "matte",
            "key_ingredients": ["niacinamidă"],
            "best_for": "ten gras",
        },
    }
    p.update(over)
    return p


def test_facet_pairs_generic_localized():
    pairs = dict(_facet_pairs(_prod()["attributes"], _pack(), "ro"))
    assert pairs["Potrivit pentru"] == "ten gras"  # value_label RO
    assert pairs["Finish"] == "mat"
    assert pairs["Ingrediente cheie"] == "niacinamidă"  # fără value_labels → display-ready


def test_brief_projects_facts_and_best_for():
    v = _brief([_prod()], _pack(), "ro")
    assert "Finish: mat" in v
    assert "bun pt ten gras" in v  # best_for static
    assert "attributes" not in v and "{" not in v  # fără obiecte brute


def test_detail_projects_usage_badges_sections():
    p = _prod()
    p["attributes"]["usage"] = {"time": ["evening"]}
    p["sections"] = [{"kind": "warnings", "title": "De reținut", "body": "Evită zona ochilor."}]
    p["badges"] = ["Fără parfum", "Best-seller"]
    v = _detail_view(p, _pack(), "ro")
    assert "cum se folosește: seara" in v
    assert "etichete: Fără parfum, Best-seller" in v
    assert "de reținut: Evită zona ochilor" in v
    assert "recomandat pentru: ten gras" in v
    assert "finish: mat" in v.lower()


def test_compare_shows_only_differing_axes():
    a = _prod(id="1", name="Velora Fond", price=50)
    b = _prod(id="2", name="Aria Fond", price=80)
    b["attributes"]["finish"] = "dewy"  # DIFERĂ; suitable_for + ingrediente IDENTICE
    v = _compare_view([a, b], _pack(), "ro")
    assert "diferențe:" in v
    assert "finish:" in v.lower()  # finish diferă → axă
    assert "preț:" in v  # preț diferă → axă
    assert "potrivit pentru:" not in v.lower()  # identic → NU apare
    assert "ingrediente cheie:" not in v.lower()  # identic → NU apare


def test_kill_switch_off_reverts(monkeypatch):
    monkeypatch.setattr(mod, "_projection_on", lambda: False)
    v = _brief([_prod()], _pack(), "ro")
    assert "Finish" not in v and "bun pt" not in v  # proiecția OFF (byte-vechi)
    cv = _compare_view([_prod(id="1"), _prod(id="2", name="B")], _pack(), "ro")
    assert "diferențe:" not in cv  # OFF → detail per produs, nu diff


def test_token_budget_and_sparse_degradation():
    # ≤6 produse, linii compacte, fără dict-uri brute
    v = _brief([_prod() for _ in range(6)], _pack(), "ro")
    assert len(v.splitlines()) == 6
    # date sărace (fără attributes) → degradează lin, nu crapă
    sparse = {"id": "9", "name": "X", "brand": "-", "price": 10, "availability": "in_stock"}
    assert _brief([sparse], _pack(), "ro")
    assert _detail_view(sparse, _pack(), "ro")
    assert _compare_view([sparse, _prod()], _pack(), "ro")
