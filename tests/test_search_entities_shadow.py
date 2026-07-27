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
