import pytest

from src.agent.query_spec import RuntimeQuerySpec
from src.domain.facets import FacetSource, FacetType, TypedFacet
from src.domain.search_entities import EvidenceReference
from src.tools import search_entities_shadow as shadow
from src.tools.base import TOOL_REGISTRY

_PRICE = TypedFacet(
    key="price",
    value_type=FacetType.NUMBER,
    source=FacetSource.COLUMN,
    source_key="price",
    operators=("lte",),
)


@pytest.mark.asyncio
async def test_shadow_orchestrator_uses_search_text_and_is_not_a_registered_live_tool(monkeypatch):
    calls = []

    async def fake_fts(conn, business_id, text, **kwargs):
        calls.append(("fts", conn, business_id, text, kwargs))
        return ["p-2", "p-1"]

    async def fake_products(conn, business_id, ids, **kwargs):
        calls.append(("products", conn, business_id, ids, kwargs))
        return [
            {"id": "p-2", "price": 50, "attributes": {}},
            {"id": "p-1", "price": 120, "attributes": {}},
        ]

    async def fake_evidence(conn, business_id, ids, **kwargs):
        calls.append(("evidence", conn, business_id, ids, kwargs))
        return {"p-2": (EvidenceReference("ev-2", "p-2", "benefit"),)}

    monkeypatch.setattr(shadow, "search_shadow_fts", fake_fts)
    monkeypatch.setattr(shadow, "get_products_by_ids", fake_products)
    monkeypatch.setattr(shadow, "load_evidence_references", fake_evidence)
    spec = RuntimeQuerySpec(
        raw_query="telefon 0712345678 ser sub 80",
        normalized_query="telefon 0712345678 ser sub 80",
        search_text="ser sub 80",
        constraints=(),
    )

    result = await shadow.search_entities_shadow(
        object(), "business-1", spec, {"price": _PRICE}, locale="ro"
    )

    assert calls[0][0] == "fts" and calls[0][2:4] == ("business-1", "ser sub 80")
    assert calls[1][3] == ["p-2", "p-1"]
    assert calls[2][3] == ["p-2", "p-1"]
    assert [candidate.product_id for candidate in result.candidates] == ["p-2", "p-1"]
    assert result.candidates[0].evidence_ids == ("ev-2",)
    assert "search_entities_shadow" not in TOOL_REGISTRY


@pytest.mark.asyncio
async def test_shadow_orchestrator_fuses_explicit_shadow_embeddings_and_falls_back_to_fts(
    monkeypatch,
):
    calls = []

    async def fake_fts(*_args, **_kwargs):
        return ["p-2", "p-1"]

    async def fake_products(*_args, **_kwargs):
        return [
            {"id": "p-2", "price": 50, "attributes": {}},
            {"id": "p-1", "price": 60, "attributes": {}},
        ]

    async def fake_refs(*_args, **_kwargs):
        return {}

    async def fake_has_embeddings(_conn, _business_id, **kwargs):
        calls.append(("has_embeddings", kwargs))
        return True

    async def fake_semantic(_conn, _business_id, embedding, **kwargs):
        calls.append(("semantic", embedding, kwargs))
        return [{"id": "p-3", "price": 40, "attributes": {}}]

    def fake_fuse(fts, semantic, **kwargs):
        calls.append(
            ("fuse", [item["id"] for item in fts], [item["id"] for item in semantic], kwargs)
        )
        return [*semantic, *fts]

    class _LLM:
        async def embed(self, texts):
            assert texts == ["ser sub 80"]
            return [[0.1, 0.2]]

    monkeypatch.setattr(shadow, "search_shadow_fts", fake_fts)
    monkeypatch.setattr(shadow, "get_products_by_ids", fake_products)
    monkeypatch.setattr(shadow, "load_evidence_references", fake_refs)
    monkeypatch.setattr(shadow, "has_embeddings", fake_has_embeddings)
    monkeypatch.setattr(shadow, "search_products_semantic", fake_semantic)
    monkeypatch.setattr(shadow, "fuse_candidates", fake_fuse)
    spec = RuntimeQuerySpec(
        raw_query="ser sub 80",
        normalized_query="ser sub 80",
        search_text="ser sub 80",
    )

    result = await shadow.search_entities_shadow(
        object(), "business-1", spec, {"price": _PRICE}, llm=_LLM()
    )

    assert calls[0] == ("has_embeddings", {"embedding_doc_type": "search_document_v1"})
    assert calls[1][0] == "semantic"
    assert calls[1][2]["embedding_doc_type"] == "search_document_v1"
    assert calls[2][0:3] == ("fuse", ["p-2", "p-1"], ["p-3"])
    assert [candidate.product_id for candidate in result.candidates] == ["p-3", "p-2", "p-1"]
