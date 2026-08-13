"""NX-234 — rehidratarea contextului pe Postgres REAL (doi tenanți, ID-uri colizionabile).

Exclus din CI fast (`-m "not integration"`). Aici se dovedesc exact garanțiile pe care un fake
nu le poate dovedi:

  • `business_id = $1` pe fiecare ramură a UNION-ului: DOI tenanți cu ACELAȘI `external_id` — cel
    mai realist vector de coliziune, fiindcă cheia platformei e aleasă de magazin, nu de noi;
  • un UUID valid al altui tenant e indistinct de unul inexistent (zero existence leak);
  • un produs `draft`/nepublicat nu devine evidence doar fiindcă browserul l-a afirmat;
  • o variantă atârnată de ALT produs nu se poate strecura ca „variantă validă";
  • prețul de context e EXACT prețul pe care îl vede validatorul pe orice altă cale (min pe
    variante, cu fereastra de promoție a produsului);
  • numărul de query-uri NU depinde de numărul de referințe (1/6/10 → un round-trip);
  • RLS-ul pe `bot_runtime` e plasa: chiar cu un query greșit, rezultatul e „zero rânduri".
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from src.catalog import context_resolver as cr
from src.db.connection import admin_conn, close_pool, get_pool
from src.db.provider import static_db
from src.db.queries.catalog import load_context_entities
from src.web import context as wc
from src.web.contracts_v2 import PageContextClaim

pytestmark = [pytest.mark.integration]

SHARED_EXTERNAL_ID = "SHOP-PRODUCT-4471"  # aceeași cheie de platformă la AMBII tenanți


async def _make_tenant(conn, *, price: float, name: str) -> dict:
    bid = str(uuid4())
    await conn.execute(
        "insert into businesses (id, slug, name, vertical, status, default_locale) "
        "values ($1, $2, 'NX-234 context', 'beauty_salon', 'active', 'ro')",
        bid,
        f"nx234-{uuid4().hex[:8]}",
    )
    parent = await conn.fetchval(
        "insert into categories (business_id, slug, name, path) "
        "values ($1, 'ingrijire', 'Îngrijire', 'ingrijire') returning id",
        bid,
    )
    cat = await conn.fetchval(
        "insert into categories (business_id, parent_id, slug, name, path) "
        "values ($1, $2, 'seruri', 'Seruri', 'ingrijire/seruri') returning id",
        bid,
        parent,
    )
    other_cat = await conn.fetchval(
        "insert into categories (business_id, slug, name, path) "
        "values ($1, 'rujuri', 'Rujuri', 'machiaj/rujuri') returning id",
        bid,
    )
    pid = await conn.fetchval(
        "insert into products (business_id, primary_category_id, external_id, slug, name, "
        " price, currency, availability, stock_total, rating, review_count, status, "
        " content_status, product_url) "
        "values ($1,$2,$3,$4,$5,$6,'RON','in_stock',7,0,0,'active','published',$7) returning id",
        bid,
        cat,
        SHARED_EXTERNAL_ID,
        f"ser-{uuid4().hex[:6]}",
        name,
        price,
        f"https://{bid[:8]}.example.com/p/ser",
    )
    vid = await conn.fetchval(
        "insert into product_variants (business_id, product_id, label, sku, price, stock) "
        "values ($1,$2,'30 ml',$3,$4,4) returning id",
        bid,
        pid,
        f"SKU-{uuid4().hex[:8]}",
        price - 10,
    )
    draft = await conn.fetchval(
        "insert into products (business_id, primary_category_id, slug, name, price, status) "
        "values ($1,$2,$3,'Produs nepublicat',10,'draft') returning id",
        bid,
        cat,
        f"draft-{uuid4().hex[:6]}",
    )
    second = await conn.fetchval(
        "insert into products (business_id, primary_category_id, slug, name, price, status) "
        "values ($1,$2,$3,'Al doilea produs',20,'active') returning id",
        bid,
        cat,
        f"second-{uuid4().hex[:6]}",
    )
    second_variant = await conn.fetchval(
        "insert into product_variants (business_id, product_id, label, sku, price, stock) "
        "values ($1,$2,'50 ml',$3,20,2) returning id",
        bid,
        second,
        f"SKU-{uuid4().hex[:8]}",
    )
    return {
        "business_id": bid,
        "product_id": str(pid),
        "variant_id": str(vid),
        "category_id": str(cat),
        "other_category_id": str(other_cat),
        "draft_id": str(draft),
        "second_product_id": str(second),
        "second_variant_id": str(second_variant),
    }


@pytest.fixture
async def shops():
    """Doi tenanți throwaway cu ACELAȘI `external_id` de produs (coliziunea realistă)."""
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        a = await _make_tenant(conn, price=89.0, name="Ser tenant A")
        b = await _make_tenant(conn, price=999.0, name="Ser tenant B")
    try:
        yield a, b
    finally:
        async with admin_conn(pool) as conn:
            for shop in (a, b):
                bid = shop["business_id"]
                await conn.execute("delete from product_variants where business_id=$1", bid)
                await conn.execute("delete from products where business_id=$1", bid)
                await conn.execute("delete from categories where business_id=$1", bid)
                await conn.execute("delete from businesses where id=$1", bid)
        await close_pool()


def _claim(**kw):
    return wc.normalize_context(PageContextClaim(**kw))


class _Counting:
    """Wrapper care numără `fetch`-urile — asertul de N+1 e pe contorul ăsta, nu pe intuiție."""

    def __init__(self, conn):
        self._conn = conn
        self.fetches = 0

    async def fetch(self, sql, *params):
        self.fetches += 1
        return await self._conn.fetch(sql, *params)

    def __getattr__(self, name):
        return getattr(self._conn, name)


# ── Izolare de tenant ────────────────────────────────────────────────────────


async def test_the_same_platform_key_resolves_to_each_tenants_own_product(shops):
    a, b = shops
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        ctx_a = await cr.resolve_surface_context(
            static_db(conn),
            a["business_id"],
            _claim(surface="product", product_id=SHARED_EXTERNAL_ID),
        )
        ctx_b = await cr.resolve_surface_context(
            static_db(conn),
            b["business_id"],
            _claim(surface="product", product_id=SHARED_EXTERNAL_ID),
        )
    assert ctx_a.product.name == "Ser tenant A" and ctx_a.product.price == 79.0
    assert ctx_b.product.name == "Ser tenant B" and ctx_b.product.price == 989.0


async def test_a_valid_uuid_of_another_tenant_is_simply_not_found(shops):
    a, b = shops
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        ctx = await cr.resolve_surface_context(
            static_db(conn), a["business_id"], _claim(surface="product", product_id=b["product_id"])
        )
    assert ctx.product is None
    assert ctx.status == "partial" and "anchor_not_found" in ctx.reasons


async def test_unpublished_product_is_not_evidence(shops):
    a, _ = shops
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        ctx = await cr.resolve_surface_context(
            static_db(conn), a["business_id"], _claim(surface="product", product_id=a["draft_id"])
        )
    assert ctx.product is None
    # Semantica externă e IDENTICĂ cu „alt tenant" și cu „inexistent" — fără oracol.
    assert "anchor_not_found" in ctx.reasons


# ── Relații ──────────────────────────────────────────────────────────────────


async def test_variant_of_another_product_invalidates_the_context(shops):
    a, _ = shops
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        ctx = await cr.resolve_surface_context(
            static_db(conn),
            a["business_id"],
            _claim(
                surface="product",
                product_id=a["product_id"],
                variant_id=a["second_variant_id"],
            ),
        )
    assert ctx.status == "invalid"
    assert ctx.product is None and ctx.variant is None
    assert ("variant_product", "cross_product") in ctx.relation_rejections


async def test_variant_of_another_tenant_is_not_found(shops):
    a, b = shops
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        ctx = await cr.resolve_surface_context(
            static_db(conn),
            a["business_id"],
            _claim(surface="product", product_id=a["product_id"], variant_id=b["variant_id"]),
        )
    assert ctx.variant is None and "variant_not_found" in ctx.reasons
    assert ctx.product is not None  # produsul legitim rămâne ancoră


async def test_incompatible_category_is_dropped_product_survives(shops):
    a, _ = shops
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        ctx = await cr.resolve_surface_context(
            static_db(conn),
            a["business_id"],
            _claim(
                surface="product", product_id=a["product_id"], category_id=a["other_category_id"]
            ),
        )
    assert ctx.category is None and ctx.product is not None
    assert ctx.status == "partial"
    assert ("category_product", "incompatible") in ctx.relation_rejections


async def test_category_slug_is_the_platform_key_for_categories(shops):
    a, _ = shops
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        ctx = await cr.resolve_surface_context(
            static_db(conn), a["business_id"], _claim(surface="category", category_id="seruri")
        )
    assert ctx.category is not None and ctx.category.slug == "seruri"
    assert ctx.status == "resolved"


# ── Preț canonic + freshness ─────────────────────────────────────────────────


async def test_context_price_is_the_effective_price_the_validator_also_sees(shops):
    """Prețul REAL e pe variantă (min), nu pe produs — dacă contextul ar raporta altceva decât
    restul catalogului, validatorul și clientul ar vedea două cifre diferite."""
    a, _ = shops
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        ctx = await cr.resolve_surface_context(
            static_db(conn), a["business_id"], _claim(surface="product", product_id=a["product_id"])
        )
    assert ctx.product.price == 79.0  # varianta, nu products.price (89)
    assert ctx.product.price_source == "variant_min"
    assert ctx.product.currency == "RON"


async def test_missing_reviews_are_unknown_not_zero_stars(shops):
    a, _ = shops
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        ctx = await cr.resolve_surface_context(
            static_db(conn), a["business_id"], _claim(surface="product", product_id=a["product_id"])
        )
    assert ctx.product.rating is None
    assert "rating" in ctx.product.unknown


async def test_freshness_is_measured_against_the_row_not_assumed(shops):
    a, _ = shops
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        fresh = await cr.resolve_surface_context(
            static_db(conn), a["business_id"], _claim(surface="product", product_id=a["product_id"])
        )
        future = datetime.now(UTC) + timedelta(days=40)
        stale = await cr.resolve_surface_context(
            static_db(conn),
            a["business_id"],
            _claim(surface="product", product_id=a["product_id"]),
            now=future,
        )
    assert fresh.product.freshness.stale is False and fresh.status == "resolved"
    assert stale.product.freshness.stale is True and stale.status == "stale"
    assert stale.product.price == 79.0  # stale MARCHEAZĂ, nu ascunde


# ── Buget de query-uri ───────────────────────────────────────────────────────


@pytest.mark.parametrize("n_refs", [1, 6, 10])
async def test_batch_hydration_stays_at_one_round_trip(shops, n_refs):
    a, _ = shops
    refs = [wc.ContextRef(a["product_id"], "uuid")] + [
        wc.ContextRef(str(uuid4()), "uuid") for _ in range(n_refs - 1)
    ]
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        counting = _Counting(conn)
        out = await cr.hydrate_refs(static_db(counting), a["business_id"], products=refs)
    assert counting.fetches == 1
    assert a["product_id"] in out["product"]


async def test_full_page_context_is_still_one_query(shops):
    """Produs + variantă + categorie: trei tipuri de entități, un singur round-trip."""
    a, _ = shops
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        counting = _Counting(conn)
        await cr.resolve_surface_context(
            static_db(counting),
            a["business_id"],
            _claim(
                surface="product",
                product_id=a["product_id"],
                variant_id=a["variant_id"],
                category_id=a["category_id"],
            ),
        )
    assert counting.fetches == 1


async def test_mixed_uuid_and_platform_keys_do_not_break_the_uuid_cast(shops):
    """Un `::uuid[]` cu un string non-UUID ar ridica `DataError`: separarea pe tipuri e o
    condiție de corectitudine, nu un stil."""
    a, _ = shops
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        out = await load_context_entities(
            conn,
            a["business_id"],
            product_uuids=[a["product_id"]],
            product_keys=[SHARED_EXTERNAL_ID],
        )
    kinds = {(r["kind"], r["ref"]) for r in out}
    assert ("product", a["product_id"]) in kinds
    assert ("product", SHARED_EXTERNAL_ID) in kinds


# ── RLS: plasa, nu mecanismul ────────────────────────────────────────────────


async def test_rls_turns_a_wrong_query_into_zero_rows(shops):
    a, b = shops
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        try:
            await conn.execute("set role bot_runtime")
            await conn.execute("select set_config('app.business_id', $1, false)", a["business_id"])
            # Query „greșit": cerem produsul lui B, dar cu business_id-ul lui B — predicatul din
            # cod ar întoarce rândul; RLS-ul îl face invizibil.
            rows = await load_context_entities(
                conn, b["business_id"], product_uuids=[b["product_id"]]
            )
            assert rows == []
        finally:
            await conn.execute("reset role")
