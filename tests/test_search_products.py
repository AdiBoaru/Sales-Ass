"""Teste integration pentru search_products (DB real, 500 produse demo).

Marcate `integration` → excluse din CI. Rulează local: pytest -m integration
"""

import pytest

from src.db.connection import close_pool, get_pool, tenant_conn
from src.db.queries.catalog import search_products

pytestmark = pytest.mark.integration

DEMO_BIZ = "6098812a-50fc-44bd-a1ba-bc77e6399158"
OTHER_BIZ = "00000000-0000-0000-0000-000000000000"

EIGHT_FIELDS = {"id", "name", "brand", "price", "url", "ai_summary", "stock", "availability"}


@pytest.fixture
async def pool():
    p = await get_pool()
    yield p
    await close_pool()


async def test_returns_limited_eight_field_shape(pool):
    async with tenant_conn(pool, DEMO_BIZ) as conn:
        rows = await search_products(conn, DEMO_BIZ, limit=3)
    assert 0 < len(rows) <= 3
    for r in rows:
        assert set(r.keys()) == EIGHT_FIELDS


async def test_hard_cap_six(pool):
    async with tenant_conn(pool, DEMO_BIZ) as conn:
        rows = await search_products(conn, DEMO_BIZ, limit=50)
    assert len(rows) <= 6


async def test_price_max_filter(pool):
    async with tenant_conn(pool, DEMO_BIZ) as conn:
        rows = await search_products(conn, DEMO_BIZ, price_max=50, limit=6)
    assert rows
    assert all(r["price"] <= 50 for r in rows)


async def test_query_text_filter(pool):
    async with tenant_conn(pool, DEMO_BIZ) as conn:
        rows = await search_products(conn, DEMO_BIZ, query_text="Pensula", limit=6)
    assert rows
    assert all("pensula" in r["name"].lower() for r in rows)


async def test_tenant_isolation_no_leak(pool):
    async with tenant_conn(pool, OTHER_BIZ) as conn:
        rows = await search_products(conn, OTHER_BIZ, limit=6)
    assert rows == []


async def test_price_reflects_variant_not_product(pool):
    """T037: prețul returnat = min preț variantă, nu products.price."""
    async with tenant_conn(pool, DEMO_BIZ) as conn:
        rows = await search_products(conn, DEMO_BIZ, limit=3)
        for r in rows:
            min_variant = await conn.fetchval(
                "select min(coalesce(sale_price, price))::float8 "
                "from product_variants where product_id = $1",
                r["id"],
            )
            assert r["price"] == min_variant
