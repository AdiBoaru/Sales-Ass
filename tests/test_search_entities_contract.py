from src.agent.query_spec import Constraint
from src.domain.facets import FacetSource, FacetType, TypedFacet
from src.domain.search_entities import EvidenceReference, build_search_entities_result

_PRICE = TypedFacet(
    key="price",
    value_type=FacetType.NUMBER,
    source=FacetSource.COLUMN,
    source_key="price",
    operators=("lte",),
)
_FRAGRANCE_FREE = TypedFacet(
    key="fragrance_free",
    value_type=FacetType.BOOL,
    source=FacetSource.ATTRIBUTE,
    source_key="fragrance_free",
    operators=("eq",),
)
_FACETS = {facet.key: facet for facet in (_PRICE, _FRAGRANCE_FREE)}


def _hard(facet, op, value):
    return Constraint(facet=facet, op=op, value=value, strength="hard")


def test_contract_preserves_per_candidate_verdicts_and_evidence_order():
    products = [
        {"id": "exact", "price": 50, "attributes": {"fragrance_free": True}},
        {"id": "alternative", "price": 50, "attributes": {}},
        {"id": "rejected", "price": 120, "attributes": {"fragrance_free": True}},
    ]
    evidence = {
        "exact": [EvidenceReference("ev-1", "exact", "benefit")],
        "alternative": [EvidenceReference("ev-2", "alternative", "warning")],
    }

    result = build_search_entities_result(
        products,
        [_hard("price", "lte", 80), _hard("fragrance_free", "eq", True)],
        _FACETS,
        evidence_by_product=evidence,
    )

    assert [candidate.product_id for candidate in result.candidates] == [
        "exact",
        "alternative",
        "rejected",
    ]
    assert [candidate.match_class for candidate in result.candidates] == [
        "exact",
        "alternative",
        "rejected",
    ]
    assert result.candidates[0].evidence_ids == ("ev-1",)
    assert result.candidates[1].evidence_ids == ("ev-2",)
    assert result.candidates[2].evidence_ids == ()
    assert [item.evidence_id for item in result.evidence] == ["ev-1", "ev-2"]


def test_unknown_is_reported_as_missing_information_not_mismatch():
    result = build_search_entities_result(
        [{"id": "unknown", "price": 50, "attributes": {}}],
        [_hard("fragrance_free", "eq", True)],
        _FACETS,
    )

    candidate = result.candidates[0]
    assert candidate.match_class == "alternative"
    assert candidate.constraint_results[0].status == "UNKNOWN"
    assert result.missing_information == ("fragrance_free",)
    assert result.needs_refinement is True
