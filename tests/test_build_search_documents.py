import pytest

from src.domain.search_documents import build_search_artifacts
from src.jobs.build_search_documents import build_for_business, upsert_artifacts


class _Conn:
    def __init__(self, rows=()):
        self.rows, self.calls = rows, []

    async def fetch(self, sql, *params):
        self.calls.append((sql, params))
        return self.rows

    async def execute(self, sql, *params):
        self.calls.append((sql, params))


def _product():
    return {
        "id": "00000000-0000-0000-0000-000000000001",
        "slug": "p",
        "name": "Ser X",
        "brandSlug": "b",
        "primaryCategorySlug": "seruri-pentru-ten",
        "attributes": {},
        "variants": [],
    }


@pytest.mark.asyncio
async def test_writer_is_tenant_scoped_and_uses_weighted_shadow_fts():
    conn = _Conn()
    artifacts = build_search_artifacts(_product(), business_id="biz", locale="ro")
    await upsert_artifacts(conn, artifacts)
    sql = "\n".join(call[0] for call in conn.calls)
    assert "where business_id=$1 and product_id=$2::uuid" in sql
    assert "to_tsvector('romanian', unaccent($7))" in sql
    assert "setweight" in sql and "product_embeddings" not in sql
    assert all(call[1][0] == "biz" for call in conn.calls)


@pytest.mark.asyncio
async def test_build_loads_only_active_products_for_requested_tenant():
    conn = _Conn([_product()])
    assert await build_for_business(conn, "biz") == 1
    assert "where p.business_id = $1 and p.status = 'active'" in conn.calls[0][0]
    assert conn.calls[0][1] == ("biz",)
