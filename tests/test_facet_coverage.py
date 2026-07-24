"""NX-186 — logica de coverage (pură, fără DB): denominator, insuficiență, 3 stări provenance."""

from scripts.facet_coverage import compute_coverage
from src.domain.facets import FacetSource, FacetType, TypedFacet

_PRICE = TypedFacet(
    key="price",
    value_type=FacetType.NUMBER,
    source=FacetSource.COLUMN,
    source_key="price",
    operators=("lte",),
    provenance="structural",
    min_coverage=0.9,
)
_FF = TypedFacet(
    key="fragrance_free",
    value_type=FacetType.BOOL,
    source=FacetSource.ATTRIBUTE,
    source_key="fragrance_free",
    operators=("eq",),
    provenance="claim",
    min_coverage=0.4,
)
_ING = TypedFacet(
    key="key_ingredients",
    value_type=FacetType.LIST,
    source=FacetSource.ATTRIBUTE,
    source_key="key_ingredients",
    operators=("contains",),
    provenance="claim",
    min_coverage=0.5,
)


def _prod(price, attrs):
    return {"price": price, "category_slug": "seruri", "attributes": attrs}


def _row(rows, facet):
    return next(r for r in rows if r["facet"] == facet)


def test_denominator_and_coverage():
    prods = [
        _prod(50, {"fragrance_free": True}),
        _prod(80, {"fragrance_free": False}),
        _prod(120, {}),  # fără fragrance_free
    ]
    rows = compute_coverage([_PRICE, _FF], {"seruri": prods}, min_products=3)
    price = _row(rows, "price")
    assert price["denominator"] == 3 and price["valid"] == 3 and price["coverage"] == 1.0
    ff = _row(rows, "fragrance_free")
    assert ff["present"] == 2 and ff["valid"] == 2  # al 3-lea n-are valoarea
    assert ff["coverage"] == round(2 / 3, 3)
    assert ff["unknown_rate"] == round(1 - 2 / 3, 3)  # 1 - coverage


def test_insufficient_data_below_min():
    # DoD: fațetă cu 3 produse în categorie, prag 5 → „date insuficiente", nu 100% fals
    prods = [_prod(50, {"fragrance_free": True})] * 3
    rows = compute_coverage([_FF], {"seruri": prods}, min_products=5)
    ff = _row(rows, "fragrance_free")
    assert ff["denominator"] == 3
    assert ff["insufficient_data"] is True
    assert ff["enforce_ready"] is False  # niciodată enforce pe date insuficiente


def test_provenance_three_states_distinct():
    # structural (price) → verified == valid; claim fără provenance → verified 0;
    # claim CU provenance (kind=ingredient) → verified > 0.
    prods = [
        _prod(
            50,
            {
                "fragrance_free": True,
                "key_ingredients": ["niacinamida"],
                "claim_provenance": [{"kind": "ingredient", "verified_at": "2026-07-16"}],
            },
        ),
        _prod(80, {"fragrance_free": True, "key_ingredients": ["retinol"]}),  # fără provenance
    ]
    rows = compute_coverage([_PRICE, _FF, _ING], {"seruri": prods}, min_products=1)
    assert _row(rows, "price")["verified"] == 2  # structural: verified = valid
    assert _row(rows, "fragrance_free")["verified"] == 0  # claim fără provenance → 0
    ing = _row(rows, "key_ingredients")
    assert (
        ing["present"] == 2 and ing["valid"] == 2 and ing["verified"] == 1
    )  # doar 1 are provenance


def test_enforce_ready_uses_per_facet_threshold():
    # coverage 0.5 pe fragrance_free (prag 0.4) → ready; pe price (prag 0.9) 0.5 → NOT ready.
    prods = [_prod(50, {"fragrance_free": True}), _prod(None, {})]
    rows = compute_coverage([_PRICE, _FF], {"seruri": prods}, min_products=1)
    assert _row(rows, "fragrance_free")["coverage"] == 0.5
    assert _row(rows, "fragrance_free")["enforce_ready"] is True
    assert _row(rows, "price")["coverage"] == 0.5
    assert _row(rows, "price")["enforce_ready"] is False  # sub pragul 0.9


def test_value_distribution_for_list():
    prods = [
        _prod(50, {"key_ingredients": ["niacinamida", "acid hialuronic"]}),
        _prod(60, {"key_ingredients": ["niacinamida"]}),
    ]
    rows = compute_coverage([_ING], {"seruri": prods}, min_products=1)
    dist = _row(rows, "key_ingredients")["value_distribution"]
    assert dist["niacinamida"] == 2 and dist["acid hialuronic"] == 1
