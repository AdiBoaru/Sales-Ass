"""Teste integration pentru search_products (DB real; catalogul demo crește — NU asertăm numărul).

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
    "list_price",  # IZI-anchor: prețul de listă (tăiat pe card la reducere reală)
    "attributes",  # NX-169/170: faptele canonice v3 → proiecție + reason_codes + gate siguranță
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
    # NX-177: „Pensula" (fără diacritice) întorcea 0 — nu fiindcă filtrul e rupt, ci fiindcă
    # produsele active se numesc „Pensulă" iar căutarea e diacritic-SENSITIVE (unaccent neinstalat,
    # FTS pe config `english`). Aici testăm filtrul pe un termen fără diacritice din catalog, ca
    # testul să fie despre `query_text`, nu despre diacritice. Defectul de diacritice (52% din
    # catalogul activ invizibil la scriere fără diacritice) are cardul lui — vezi tasks/NX-178.md.
    async with tenant_conn(DEMO_BIZ) as conn:
        rows = await search_products(conn, DEMO_BIZ, query_text="Mascara", limit=6)
    assert rows
    assert all("mascara" in r["name"].lower() for r in rows)


async def test_tenant_isolation_no_leak(pool):
    async with tenant_conn(OTHER_BIZ) as conn:
        rows = await search_products(conn, OTHER_BIZ, limit=6)
    assert rows == []


async def test_price_reflects_variant_not_product(pool):
    """T037: prețul returnat = min preț variantă CÂND produsul are variante.

    NX-177: testul presupunea că TOATE produsele au variante — în catalogul actual 104/654 n-au
    (doar 46/150 dintre cele active au), deci `min_variant` ieșea None și assertul pica pe un
    produs perfect valid. Contractul real e CONDIȚIONAT: cu variante → min-ul lor; fără →
    products.price. Aici verificăm proiecția pe ce întoarce search-ul; ramura CU variante e
    țintită explicit în testul de mai jos (setul default n-are variante, deci aici ar trece
    vacuu)."""
    async with tenant_conn(DEMO_BIZ) as conn:
        rows = await search_products(conn, DEMO_BIZ, limit=6)
        assert rows
        for r in rows:
            min_variant = await conn.fetchval(
                "select min(coalesce(sale_price, price))::float8 "
                "from product_variants where product_id = $1",
                r["id"],
            )
            expected = min_variant
            if expected is None:  # fără variante → prețul produsului
                expected = await conn.fetchval(
                    "select coalesce(sale_price, price)::float8 from products where id = $1",
                    r["id"],
                )
            assert r["price"] == expected


async def test_price_is_min_variant_when_product_has_variants(pool):
    """Ramura CU variante, țintită: alegem un produs care CHIAR are variante.

    NX-177: fără țintire, contractul „prețul = min varianta" nu era exercitat deloc — setul
    default al search-ului n-are variante, deci vechiul assert ar fi trecut din întâmplare pe
    ramura greșită."""
    async with tenant_conn(DEMO_BIZ) as conn:
        row = await conn.fetchrow(
            "select p.id::text id, min(coalesce(v.sale_price, v.price))::float8 mn "
            "from products p join product_variants v on v.product_id = p.id "
            "where p.business_id = $1 and p.status = 'active' "
            "group by p.id having count(v.id) > 1 limit 1",
            DEMO_BIZ,
        )
        assert row is not None, "catalogul demo n-are niciun produs activ cu ≥2 variante"
        [prod] = await get_products_by_ids(conn, DEMO_BIZ, [row["id"]], limit=1)
    assert prod["price"] == row["mn"]


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
