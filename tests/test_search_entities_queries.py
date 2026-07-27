import pytest

from src.db.queries.search_entities import (
    load_evidence_references,
    load_identifier_candidates,
    search_shadow_fts,
)


class _Conn:
    def __init__(self):
        self.calls = []

    async def fetch(self, query, *args):
        self.calls.append((query, args))
        if "product_evidence_chunks" in query:
            return [
                {"evidence_id": "ev-2", "product_id": "p-2", "role": "warning"},
                {"evidence_id": "ev-1", "product_id": "p-1", "role": "benefit"},
            ]
        if "intent_aliases" in query:
            return [{"product_id": "p-1", "name": "Ser X", "skus": ["SER-X"], "aliases": ["ser x"]}]
        return [{"product_id": "p-2"}, {"product_id": "p-1"}]


@pytest.mark.asyncio
async def test_shadow_fts_is_explicitly_scoped_to_tenant_locale_and_version():
    conn = _Conn()

    ids = await search_shadow_fts(
        conn,
        "business-1",
        "ser fara parfum",
        locale="ro",
        document_version=1,
    )

    query, args = conn.calls[0]
    assert ids == ["p-2", "p-1"]
    assert "d.business_id = $1" in query
    assert "p.business_id = d.business_id" in query
    assert "d.locale = $3" in query and "d.document_version = $4" in query
    assert args == ("business-1", "ser fara parfum", "ro", 1, 50)


@pytest.mark.asyncio
async def test_evidence_hydration_is_batch_scoped_and_keeps_references_per_product():
    conn = _Conn()

    refs = await load_evidence_references(conn, "business-1", ["p-1", "p-2"], locale="ro")

    query, args = conn.calls[0]
    assert "where business_id = $1 and product_id = any($2::uuid[]) and locale = $3" in query
    assert args == ("business-1", ["p-1", "p-2"], "ro")
    assert refs["p-1"][0].evidence_id == "ev-1"
    assert refs["p-2"][0].role == "warning"


@pytest.mark.asyncio
async def test_empty_query_or_products_avoid_database_call():
    conn = _Conn()

    assert await search_shadow_fts(conn, "business-1", "   ") == []
    assert await load_evidence_references(conn, "business-1", []) == {}
    assert conn.calls == []


@pytest.mark.asyncio
async def test_identifier_candidates_are_active_approved_and_tenant_scoped():
    conn = _Conn()

    candidates = await load_identifier_candidates(conn, "business-1")

    query, args = conn.calls[0]
    assert "where p.business_id = $1 and p.status = 'active'" in query
    assert "ia.business_id = p.business_id" in query
    assert "ia.target_kind = 'product'" in query and "ia.status = 'approved'" in query
    assert args == ("business-1",)
    assert candidates[0].skus == ("SER-X",)
    assert candidates[0].aliases == ("ser x",)
