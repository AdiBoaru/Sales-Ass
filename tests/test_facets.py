"""NX-186 — registru tipizat de fațete + coverage (pur, fără DB)."""

import math

import pytest

from src.domain.facets import FacetSpec, build_registry, facet_coverage, facet_value


def test_facetspec_validation_fail_closed():
    FacetSpec("fragrance_free", "bool", ("eq",))  # ok
    with pytest.raises(ValueError):
        FacetSpec("x", "badtype", ("eq",))
    with pytest.raises(ValueError):
        FacetSpec("x", "enum", ("badop",))
    with pytest.raises(ValueError):
        FacetSpec("x", "bool", ("eq",), missing_policy="nope")


def test_build_registry_rejects_duplicates():
    with pytest.raises(ValueError):
        build_registry([FacetSpec("a", "bool", ("eq",)), FacetSpec("a", "bool", ("eq",))])
    reg = build_registry([FacetSpec("a", "bool", ("eq",)), FacetSpec("b", "number", ("lte",))])
    assert set(reg) == {"a", "b"}


def test_facet_value_toplevel_attributes_and_alias():
    spec = FacetSpec("finish", "enum", ("eq",), values=("matte", "dewy"), aliases={"mat": "matte"})
    assert facet_value({"finish": "mat"}, spec) == "matte"  # alias + top-level
    assert facet_value({"attributes": {"finish": "dewy"}}, spec) == "dewy"  # din attributes
    assert facet_value({}, spec) is None  # lipsă
    b = FacetSpec("fragrance_free", "bool", ("eq",))
    assert facet_value({"fragrance_free": True}, b) is True


def test_facet_coverage_present_vs_valid_and_enforceable():
    spec = FacetSpec("finish", "enum", ("eq",), values=("matte", "dewy"), min_coverage=0.5)
    prods = [{"finish": "matte"}] * 6 + [{}] * 4  # 6/10 prezente, toate valide
    cov = facet_coverage(prods, spec)
    assert cov["present"] == 6 and cov["pct_present"] == 0.6 and cov["valid"] == 6
    assert cov["enforceable"] is True  # n>=10 și 0.6>=0.5
    # valoare enum INVALIDĂ → prezentă dar nu validă → NU enforceable (Codex: pct_valid, nu present)
    spec2 = FacetSpec("finish", "enum", ("eq",), values=("matte",))
    cov2 = facet_coverage([{"finish": "necunoscut"}] * 10, spec2)
    assert cov2["present"] == 10 and cov2["valid"] == 0 and cov2["pct_valid"] == 0.0
    assert cov2["enforceable"] is False  # 10 prezente dar 0 valide → nu se enforce-uiește
    # prea puține produse → NU enforceable (evită „100%" pe 3 produse)
    assert facet_coverage([{"finish": "matte"}] * 3, spec)["enforceable"] is False


def test_facet_coverage_typed_validity_bool_number():
    # Codex: coverage-ul valida orice non-enum. bool → doar bool real; number → doar numeric.
    b = FacetSpec("fragrance_free", "bool", ("eq",))
    cov = facet_coverage([{"fragrance_free": True}] * 4 + [{"fragrance_free": "necunoscut"}] * 6, b)
    assert cov["present"] == 10 and cov["valid"] == 4 and cov["pct_valid"] == 0.4
    assert cov["enforceable"] is False  # doar bool real e valid → sub prag
    # numeric: "n/a" prezent dar nu valid
    num = FacetSpec("spf", "number", ("gte",))
    assert facet_coverage([{"spf": 30}] * 10, num)["valid"] == 10
    cov2 = facet_coverage([{"spf": "n/a"}] * 10, num)
    assert cov2["present"] == 10 and cov2["valid"] == 0 and cov2["enforceable"] is False
    # Codex R6: string bool cunoscut („da") e VALID (aliniat cu Match Gate — divergență închisă)
    assert facet_coverage([{"fragrance_free": "da"}] * 10, b)["valid"] == 10
    # Codex R6: NaN NU e număr valid (era considerat valid)
    assert facet_coverage([{"spf": math.nan}] * 10, num)["valid"] == 0
