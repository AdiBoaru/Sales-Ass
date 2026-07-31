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


# --- P1 review #251: quality-gate în BAZIN, nu după trunchiere ---------------


class _SqlConn:
    def __init__(self):
        self.queries = []

    async def fetch(self, sql, *params):
        self.queries.append((sql, params))
        return []


@pytest.mark.asyncio
async def test_content_status_filters_the_pool_not_the_hydration():
    """`get_products_by_ids` taie lista la ≤6 ÎNAINTE să-și aplice filtrul de content_status
    (catalog.py: `product_ids[:limit]`). Deci un filtru aplicat doar la hidratare producea ZERO
    rezultate când primii candidați erau `draft`, deși mai jos în bazin existau produse publicate.
    Filtrul trebuie să fie acolo unde se alege ordinea, nu după ce s-a tăiat din ea."""
    conn = _SqlConn()
    await search_shadow_fts(conn, "b", "ser pentru ten gras")

    sql = conn.queries[0][0]
    assert "content_status" in sql
    # filtrul intră în WHERE, înainte de order/limit — altfel n-ar schimba ce candidați rămân
    assert sql.index("content_status") < sql.index("order by rank desc")


@pytest.mark.asyncio
async def test_identifier_candidates_respect_the_same_quality_gate():
    """Altfel identificatorul ar fi poarta din spate: un SKU exact ar rezolva un produs nepublicat
    pe care căutarea nu are voie să-l arate."""
    conn = _SqlConn()
    await load_identifier_candidates(conn, "b")

    sql = conn.queries[0][0]
    assert "content_status" in sql
    assert "'{}'::text[]" in sql  # f-string-ul nu a mâncat literalul de array gol
