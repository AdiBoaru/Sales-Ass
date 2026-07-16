"""NX-171a — izolare multi-tenant pe coloanele comerciale de variantă (gtin/net_content/image_url/
price_per_unit NU scapă între tenanți) + expunerea lor în read-path (_DETAIL_SELECT/_VARIANTS_AGG).

Integration (creează 2 businesses throwaway, COMMIT, curăță la teardown). Exclus din CI fast
(`-m "not integration"`); rulează local / nightly.
"""

from uuid import uuid4

import pytest

from src.db.connection import admin_conn, close_pool, get_pool
from src.db.queries.catalog import get_products_by_ids

pytestmark = [pytest.mark.integration]


async def _make_shop(conn, bid, *, gtin, ncv, ncu, image):
    await conn.execute(
        "insert into businesses (id, slug, name, vertical, status, default_locale) "
        "values ($1,$2,'NX-171a iso','beauty_salon','active','ro')",
        bid,
        f"nx171a-{uuid4().hex[:8]}",
    )
    cat = await conn.fetchval(
        "insert into categories (business_id, slug, name) values ($1,'cat','Cat') returning id", bid
    )
    pid = await conn.fetchval(
        "insert into products (business_id, primary_category_id, slug, name, price) "
        "values ($1,$2,'prod','Prod',50) returning id",
        bid,
        cat,
    )
    await conn.execute(
        "insert into product_variants "
        "(business_id, product_id, label, sku, price, stock, gtin, net_content_value, "
        " net_content_unit, image_url) values ($1,$2,'V',$3,50,1,$4,$5,$6,$7)",
        bid,
        pid,
        f"SKU-{uuid4().hex[:8]}",
        gtin,
        ncv,
        ncu,
        image,
    )
    return pid


@pytest.fixture
async def two_shops():
    pool = await get_pool()
    a, b = str(uuid4()), str(uuid4())
    async with admin_conn(pool) as conn:
        pa = await _make_shop(
            conn, a, gtin="4006381333931", ncv=50, ncu="ml", image="http://a/v.jpg"
        )
        pb = await _make_shop(conn, b, gtin=None, ncv=30, ncu="ml", image="http://b/v.jpg")
    try:
        yield a, b, pa, pb
    finally:
        async with admin_conn(pool) as conn:
            for bid in (a, b):
                await conn.execute("delete from product_variants where business_id=$1", bid)
                await conn.execute("delete from products where business_id=$1", bid)
                await conn.execute("delete from categories where business_id=$1", bid)
                await conn.execute("delete from businesses where id=$1", bid)
        await close_pool()


async def test_variant_commercial_fields_scoped_to_tenant(two_shops):
    a, b, pa, pb = two_shops
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        # business A vede variantele LUI + câmpurile comerciale noi în payload (read-path)
        prods = await get_products_by_ids(conn, a, [pa], limit=1)
        assert len(prods) == 1
        v = prods[0]["variants"][0]
        assert v["net_content_value"] == 50 and v["net_content_unit"] == "ml"
        assert v["price_per_unit"] == 100.0  # 50 lei / 50ml * 100 (generated column)
        assert v["gtin"] == "4006381333931"  # GTIN expus în payload
        assert v["image_url"] == "http://a/v.jpg"  # imaginea proprie expusă

        # TENANT ISOLATION: A NU poate vedea produsul/varianta lui B (filtru business_id în SQL)
        leak = await get_products_by_ids(conn, a, [pb], limit=1)
        assert leak == []
        # și invers, ca dovadă simetrică
        assert await get_products_by_ids(conn, b, [pa], limit=1) == []


async def test_rogue_variant_wrong_business_does_not_leak_into_payload(two_shops):
    """Adversarial (review PR #226): `_VARIANTS_AGG` corela varianta DOAR pe `product_id = p.id`.
    Cum `products.id` e UUID global-unic, un rând de variantă cu `business_id=B` dar
    `product_id`=produsul lui A trece FK-ul și, fără filtrul pe business_id, ar intra în payload-ul
    lui A. Injectăm exact acel rând rogue și cerem ca A să vadă DOAR varianta lui legitimă."""
    a, b, pa, _pb = two_shops
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        # varianta rogue: tenantul B, dar atârnată de produsul lui A
        rogue_sku = f"ROGUE-{uuid4().hex[:8]}"
        await conn.execute(
            "insert into product_variants "
            "(business_id, product_id, label, sku, price, stock, gtin, net_content_value, "
            " net_content_unit, image_url) "
            "values ($1,$2,'ROGUE',$3,9,1,'4006381333931',10,'ml','http://b/rogue.jpg')",
            b,  # business_id GREȘIT (B), pe product_id-ul lui A
            pa,
            rogue_sku,
        )
        try:
            prods = await get_products_by_ids(conn, a, [pa], limit=1)
            assert len(prods) == 1
            skus = {v["sku"] for v in prods[0]["variants"]}
            assert rogue_sku not in skus, "variantă cu business_id greșit scăpată în payload A"
            # A vede exact varianta lui legitimă (una singură), nu cea rogue
            assert all(v["label"] != "ROGUE" for v in prods[0]["variants"])
        finally:
            await conn.execute("delete from product_variants where sku=$1", rogue_sku)
