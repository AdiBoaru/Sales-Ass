"""Teste integration pentru search_products (DB real, 500 produse demo).

Marcate `integration` → excluse din CI. Rulează local: pytest -m integration
"""

import pytest

from src.db.connection import close_pool, get_pool, tenant_conn
from src.db.queries.catalog import get_products_by_ids, search_cheaper_than, search_products

pytestmark = pytest.mark.integration

DEMO_BIZ = "6098812a-50fc-44bd-a1ba-bc77e6399158"
OTHER_BIZ = "00000000-0000-0000-0000-000000000000"

FIELDS = {
    "id",
    "name",
    "brand",
    "price",
    "url",
    "ai_summary",
    "stock",
    "availability",
    "image",
    "rating",
    "review_count",
    "review_pro",
    "top_pros",
    "on_sale",  # NX-113 (ranking tie-break)
    "concerns",  # NX-124 (taxonomy filter)
    "variants",  # NX-118 (per-variant hydration)
}


@pytest.fixture
async def pool():
    p = await get_pool()
    yield p
    await close_pool()


async def test_returns_limited_eight_field_shape(pool):
    async with tenant_conn(DEMO_BIZ) as conn:
        rows = await search_products(conn, DEMO_BIZ, limit=3)
    assert 0 < len(rows) <= 3
    for r in rows:
        assert set(r.keys()) == FIELDS
        assert isinstance(r["variants"], list)  # NX-118: variants decodate la list[dict] (sau [])


async def test_hard_cap_six(pool):
    async with tenant_conn(DEMO_BIZ) as conn:
        rows = await search_products(conn, DEMO_BIZ, limit=50)
    assert len(rows) <= 6


async def test_price_max_filter(pool):
    async with tenant_conn(DEMO_BIZ) as conn:
        rows = await search_products(conn, DEMO_BIZ, price_max=50, limit=6)
    assert rows
    assert all(r["price"] <= 50 for r in rows)


async def test_query_text_filter(pool):
    async with tenant_conn(DEMO_BIZ) as conn:
        rows = await search_products(conn, DEMO_BIZ, query_text="Pensula", limit=6)
    assert rows
    assert all("pensula" in r["name"].lower() for r in rows)


async def test_tenant_isolation_no_leak(pool):
    async with tenant_conn(OTHER_BIZ) as conn:
        rows = await search_products(conn, OTHER_BIZ, limit=6)
    assert rows == []


async def test_price_reflects_variant_not_product(pool):
    """T037: prețul returnat = min preț variantă, nu products.price."""
    async with tenant_conn(DEMO_BIZ) as conn:
        rows = await search_products(conn, DEMO_BIZ, limit=3)
        for r in rows:
            min_variant = await conn.fetchval(
                "select min(coalesce(sale_price, price))::float8 "
                "from product_variants where product_id = $1",
                r["id"],
            )
            assert r["price"] == min_variant


# --- P0/P1 ARCH-product-retrieval: sortare pe intenție + cheaper + ordine -----


async def test_sort_mode_price_asc_surfaces_global_cheapest(pool):
    """price_asc → preț crescător ȘI rândul 1 = cel mai ieftin produs ACTIV (bug-ul 18.99)."""
    async with tenant_conn(DEMO_BIZ) as conn:
        rows = await search_products(conn, DEMO_BIZ, sort_mode="price_asc", limit=6)
        assert rows
        prices = [r["price"] for r in rows]
        assert prices == sorted(prices)  # crescător
        global_min = await conn.fetchval(
            "select min(coalesce(vp.price, p.sale_price, p.price))::float8 "
            "from products p left join lateral ("
            "  select min(coalesce(v.sale_price, v.price)) as price from product_variants v"
            "  where v.product_id = p.id) vp on true "
            "where p.business_id = $1 and p.status = 'active'",
            DEMO_BIZ,
        )
        assert rows[0]["price"] == pytest.approx(global_min)


async def test_sort_mode_deterministic_tiebreak(pool):
    """Tie-break `p.id` → apeluri repetate byte-identice (cache + golden stabile)."""
    async with tenant_conn(DEMO_BIZ) as conn:
        a = await search_products(conn, DEMO_BIZ, sort_mode="price_asc", limit=6)
        b = await search_products(conn, DEMO_BIZ, sort_mode="price_asc", limit=6)
    assert [r["id"] for r in a] == [r["id"] for r in b]


async def test_search_cheaper_than_strictly_cheaper_ascending(pool):
    """search_cheaper_than → DOAR produse strict mai ieftine decât baseline, preț crescător."""
    async with tenant_conn(DEMO_BIZ) as conn:
        base = await search_products(conn, DEMO_BIZ, limit=3)
        assert base
        ref_ids = [r["id"] for r in base]
        baseline = min(r["price"] for r in base)
        cheaper = await search_cheaper_than(conn, DEMO_BIZ, ref_ids, baseline, limit=6)
        assert all(c["price"] < baseline for c in cheaper)  # STRICT mai ieftin (zero padding)
        prices = [c["price"] for c in cheaper]
        assert prices == sorted(prices)


async def test_get_products_by_ids_preserves_input_order(pool):
    """Deixis ordinal („a doua") → ordinea cerută e PĂSTRATĂ (array_position)."""
    async with tenant_conn(DEMO_BIZ) as conn:
        base = await search_products(conn, DEMO_BIZ, limit=3)
        ids = [r["id"] for r in base]
        rev = list(reversed(ids))
        rows = await get_products_by_ids(conn, DEMO_BIZ, rev, limit=6)
        assert [r["id"] for r in rows] == rev
