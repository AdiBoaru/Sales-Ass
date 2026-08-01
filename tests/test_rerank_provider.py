import pytest

from src.agent.query_spec import Constraint, RuntimeQuerySpec
from src.domain.rerank_policy import RerankDecision
from src.domain.rerank_provider import apply_adaptive_rerank, build_rerank_candidates
from src.tools import search_entities_shadow as shadow


def _requested() -> RerankDecision:
    return RerankDecision(requested=True, reasons=("multiple_constraints",))


@pytest.mark.asyncio
async def test_rerank_provider_receives_only_safe_query_and_catalog_facets():
    seen = {}

    class Provider:
        async def rerank(self, query, candidates):
            seen["query"] = query
            seen["candidates"] = candidates
            return ["p-2"]

    products = [
        {
            "id": "p-1",
            "name": "Ion Popescu personal serum",
            "brand": "Brand with private note",
            "attributes": {"concerns": ["oily"], "finish": "matte"},
        },
        {
            "id": "p-2",
            "attributes": {
                "key_ingredients": ["niacinamide"],
                "usage": ["daily"],
                "private_note": "ion.popescu@example.com",
            },
        },
    ]

    result = await apply_adaptive_rerank(
        products,
        _requested(),
        normalized_query="ser pentru ten gras",
        raw_query="ser pentru ten gras",
        provider=Provider(),
    )

    assert [product["id"] for product in result.products] == ["p-2", "p-1"]
    assert result.degradation is None
    assert seen["query"] == "ser pentru ten gras"
    assert [candidate.product_id for candidate in seen["candidates"]] == ["p-1", "p-2"]
    assert seen["candidates"][0].document == "concerns: oily; finish: matte"
    assert seen["candidates"][1].document == "key_ingredients: niacinamide; usage: daily"
    assert all("ion" not in candidate.document for candidate in seen["candidates"])


@pytest.mark.asyncio
async def test_pii_blocks_provider_before_invocation():
    called = False

    class Provider:
        async def rerank(self, _query, _candidates):
            nonlocal called
            called = True
            return ["p-2", "p-1"]

    products = [{"id": "p-1", "attributes": {}}, {"id": "p-2", "attributes": {}}]
    result = await apply_adaptive_rerank(
        products,
        _requested(),
        normalized_query="ser pentru ten gras",
        raw_query="Sunt Ion Popescu, caut un ser",
        provider=Provider(),
    )

    assert [product["id"] for product in result.products] == ["p-1", "p-2"]
    assert result.degradation == "rerank_blocked_pii"
    assert called is False


@pytest.mark.asyncio
async def test_invalid_provider_order_falls_back_without_dropping_candidates():
    class Provider:
        async def rerank(self, _query, _candidates):
            return ["unknown-product", "p-1"]

    products = [{"id": "p-1", "attributes": {}}, {"id": "p-2", "attributes": {}}]
    result = await apply_adaptive_rerank(
        products,
        _requested(),
        normalized_query="ser",
        raw_query="ser",
        provider=Provider(),
    )

    assert [product["id"] for product in result.products] == ["p-1", "p-2"]
    assert result.degradation == "rerank_invalid_response"


@pytest.mark.asyncio
async def test_provider_failure_falls_back_to_retrieval_order():
    class Provider:
        async def rerank(self, _query, _candidates):
            raise RuntimeError("service unavailable")

    products = [{"id": "p-1", "attributes": {}}, {"id": "p-2", "attributes": {}}]
    result = await apply_adaptive_rerank(
        products,
        _requested(),
        normalized_query="ser",
        raw_query="ser",
        provider=Provider(),
    )

    assert [product["id"] for product in result.products] == ["p-1", "p-2"]
    assert result.degradation == "rerank_failed"


@pytest.mark.asyncio
async def test_rerank_is_not_called_when_policy_skips_it():
    called = False

    class Provider:
        async def rerank(self, _query, _candidates):
            nonlocal called
            called = True
            return ["p-2", "p-1"]

    products = [{"id": "p-1", "attributes": {}}, {"id": "p-2", "attributes": {}}]
    result = await apply_adaptive_rerank(
        products,
        RerankDecision(requested=False, reasons=("exact_identifier",)),
        normalized_query="ser",
        raw_query="ser",
        provider=Provider(),
    )

    assert [product["id"] for product in result.products] == ["p-1", "p-2"]
    assert result.degradation is None
    assert called is False


def test_rerank_document_allowlist_ignores_free_form_product_fields():
    candidates = build_rerank_candidates(
        [
            {
                "id": "p-1",
                "name": "private product title",
                "brand": "private brand",
                "ai_summary": "private description",
                "attributes": {"finish": "matte", "free_text": "do not export"},
            }
        ]
    )

    assert candidates[0].document == "finish: matte"


@pytest.mark.asyncio
async def test_shadow_applies_provider_order_before_final_output_cap(monkeypatch):
    seen = {}

    async def fake_fts(*_args, **_kwargs):
        return [f"p-{index}" for index in range(1, 8)]

    async def fake_products(_conn, _business_id, ids, **_kwargs):
        return [{"id": product_id, "attributes": {"concerns": ["oily"]}} for product_id in ids]

    async def fake_refs(*_args, **_kwargs):
        return {}

    async def no_identifiers(*_args):
        return []

    class Provider:
        async def rerank(self, _query, candidates):
            seen["candidates"] = candidates
            return [f"p-{index}" for index in range(7, 0, -1)]

    monkeypatch.setattr(shadow, "search_shadow_fts", fake_fts)
    monkeypatch.setattr(shadow, "get_products_by_ids", fake_products)
    monkeypatch.setattr(shadow, "load_evidence_references", fake_refs)
    monkeypatch.setattr(shadow, "load_identifier_candidates", no_identifiers)

    result = await shadow.search_entities_shadow(
        object(),
        "business-1",
        RuntimeQuerySpec(
            "ser cu ten gras",
            "ser cu ten gras",
            "ser cu ten gras",
            constraints=(
                Constraint(facet="concern", op="contains", value="oily"),
                Constraint(facet="concern", op="contains", value="dry"),
            ),
        ),
        {},
        reranker=Provider(),
    )

    assert [candidate.product_id for candidate in result.candidates] == [
        "p-7",
        "p-6",
        "p-5",
        "p-4",
        "p-3",
        "p-2",
    ]
    assert [candidate.product_id for candidate in seen["candidates"]] == [
        f"p-{index}" for index in range(1, 8)
    ]
    assert result.degradations == ("semantic_skipped_no_llm",)
