"""NX-186 — registru de fațete tipizate: construcție validă + fail-closed pe config nesigur.

Pur (fără DB/LLM). Invariantul central: nicio sursă/cheie nesigură nu devine TypedFacet (nimic din
config nu se poate interpola în SQL)."""

from src.domain.facets import (
    FacetSource,
    FacetType,
    TypedFacet,
    build_facets,
    extract_value,
    is_valid_value,
)

_OK = [
    {
        "key": "price",
        "value_type": "number",
        "source": "column",
        "source_key": "price",
        "operators": ["lte", "gte"],
        "min_coverage": 0.9,
        "labels": {"ro": "Preț"},
    },
    {
        "key": "fragrance_free",
        "value_type": "bool",
        "source": "attribute",
        "source_key": "fragrance_free",
        "provenance": "claim",
    },
    {
        "key": "concerns",
        "value_type": "list",
        "source": "attribute",
        "source_key": "concerns",
        "operators": ["contains"],
        "values": ["oily", "dry"],
        "list_semantics": "any",
    },
    {"key": "category", "value_type": "enum", "source": "category", "operators": ["eq"]},
]


# --- happy ------------------------------------------------------------------


def test_valid_facets_built_with_types_and_ops():
    facets = {f.key: f for f in build_facets(_OK)}
    assert facets["price"].value_type is FacetType.NUMBER
    assert set(facets["price"].operators) == {"lte", "gte"}
    assert facets["fragrance_free"].value_type is FacetType.BOOL
    assert facets["concerns"].value_type is FacetType.LIST
    assert facets["concerns"].values == ("oily", "dry")
    assert facets["category"].source is FacetSource.CATEGORY
    assert facets["category"].source_key == "primary_category_id"  # fix, nu din config


def test_default_operators_when_omitted():
    # fragrance_free fără `operators` → default-ul tipului bool ({eq})
    ff = {f.key: f for f in build_facets(_OK)}["fragrance_free"]
    assert ff.operators == ("eq",)


def test_label_fallback_to_key():
    price = {f.key: f for f in build_facets(_OK)}["price"]
    assert price.label("ro") == "Preț"
    assert price.label("hu") == "Preț"  # fallback ro
    assert {f.key: f for f in build_facets(_OK)}["concerns"].label(
        "en"
    ) == "concerns"  # fallback key


# --- fail-closed (config nesigur RESPINS la load) ---------------------------


def test_json_path_source_key_rejected():
    # DoD: „config cu JSON path interpolat → respins la load"
    out = build_facets(
        [
            {
                "key": "x",
                "value_type": "text",
                "source": "attribute",
                "source_key": "attributes->'a'->>'b'",
            }
        ]
    )
    assert out == ()


def test_sql_injection_source_key_rejected():
    out = build_facets(
        [
            {
                "key": "x",
                "value_type": "text",
                "source": "attribute",
                "source_key": "name); drop table products;--",
            }
        ]
    )
    assert out == ()


def test_non_whitelisted_column_rejected():
    out = build_facets(
        [
            {
                "key": "secret",
                "value_type": "text",
                "source": "column",
                "source_key": "credentials_ref",
            }
        ]
    )
    assert out == ()


def test_operator_not_allowed_for_type_rejected():
    # `contains` nu e permis pe bool
    out = build_facets(
        [
            {
                "key": "ff",
                "value_type": "bool",
                "source": "attribute",
                "source_key": "fragrance_free",
                "operators": ["contains"],
            }
        ]
    )
    assert out == ()


def test_bad_min_coverage_rejected():
    out = build_facets(
        [
            {
                "key": "p",
                "value_type": "number",
                "source": "column",
                "source_key": "price",
                "min_coverage": 1.5,
            }
        ]
    )
    assert out == ()


def test_one_bad_facet_does_not_kill_the_rest():
    out = build_facets(
        _OK + [{"key": "evil", "value_type": "text", "source": "column", "source_key": "evil_col"}]
    )
    keys = {f.key for f in out}
    assert "evil" not in keys and "price" in keys  # fail-closed per fațetă, restul supraviețuiește


def test_duplicate_key_keeps_first():
    out = build_facets(
        [
            _OK[0],
            {"key": "price", "value_type": "number", "source": "column", "source_key": "rating"},
        ]
    )
    assert len([f for f in out if f.key == "price"]) == 1
    assert next(f for f in out if f.key == "price").source_key == "price"


# --- extract_value + is_valid_value -----------------------------------------


def _spec(**kw):
    base = dict(
        key="k",
        value_type=FacetType.TEXT,
        source=FacetSource.ATTRIBUTE,
        source_key="k",
        operators=("eq",),
    )
    base.update(kw)
    return TypedFacet(**base)


def test_extract_from_column_attribute_category():
    prod = {"price": 99.0, "category_slug": "seruri-pentru-ten"}
    attrs = {"fragrance_free": True}
    assert extract_value(_spec(source=FacetSource.COLUMN, source_key="price"), prod, attrs) == 99.0
    assert extract_value(_spec(source=FacetSource.CATEGORY), prod, attrs) == "seruri-pentru-ten"
    assert extract_value(_spec(source_key="fragrance_free"), prod, attrs) is True
    assert extract_value(_spec(source_key="missing"), prod, attrs) is None


def test_is_valid_value_by_type():
    assert is_valid_value(_spec(value_type=FacetType.BOOL), True)
    assert not is_valid_value(_spec(value_type=FacetType.BOOL), "true")  # string ≠ bool
    assert is_valid_value(_spec(value_type=FacetType.NUMBER), 30)
    assert not is_valid_value(_spec(value_type=FacetType.NUMBER), True)  # bool nu e number
    assert is_valid_value(_spec(value_type=FacetType.LIST, values=("oily",)), ["oily"])
    assert not is_valid_value(_spec(value_type=FacetType.LIST, values=("oily",)), ["unknown_val"])
    assert not is_valid_value(_spec(value_type=FacetType.LIST), [])  # listă goală = nevalid
    assert is_valid_value(_spec(value_type=FacetType.ENUM, values=("matte",)), "matte")
    assert not is_valid_value(_spec(value_type=FacetType.ENUM, values=("matte",)), "glossy")
    assert not is_valid_value(_spec(), None)
