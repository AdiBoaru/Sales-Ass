"""NX-171b/c/d — teste INTEGRATION (DB reală, businesses throwaway, COMMIT + cleanup la teardown).

Exclus din CI fast (`-m "not integration"`); rulează local / nightly. Acoperă exact garanțiile de
securitate/corectitudine care nu pot fi dovedite fără DB:
  - 171b: FK compus respinge relația CROSS-TENANT (adversarial) + constrângeri comerciale
          (self-relation / duplicat / position<0) + get_complementary_products relations-first.
  - 171c: filtrul `published` (flag per-tenant) ascunde 'draft'; test-plasă visible_count > 0.
  - 171d: join versionat pe embeddings → UN singur rând/produs (2 doc_type → fără duplicat).
"""

from uuid import uuid4

import asyncpg
import pytest

from src.config import get_settings
from src.db.connection import admin_conn, close_pool, get_pool, register_vector_codec
from src.db.queries.catalog import (
    get_complementary_products,
    search_products,
    search_products_semantic,
)

pytestmark = [pytest.mark.integration]


async def _make_business(conn, bid: str) -> None:
    await conn.execute(
        "insert into businesses (id, slug, name, vertical, status, default_locale) "
        "values ($1, $2, 'NX-171 iso', 'beauty_salon', 'active', 'ro')",
        bid,
        f"nx171-{uuid4().hex[:8]}",
    )


async def _make_product(conn, bid: str, *, name: str, price: float = 50.0, **cols) -> str:
    """Inserează un produs minimal; `cols` suprascrie coloane (content_status/availability/...)."""
    base = {"content_status": "published", "availability": "in_stock", "status": "active"}
    base.update(cols)
    keys = list(base)
    ph = ", ".join(f"${i + 5}" for i in range(len(keys)))  # $1..$4 = business_id/slug/name/price
    return await conn.fetchval(
        f"insert into products (business_id, slug, name, price, {', '.join(keys)}) "
        f"values ($1, $2, $3, $4, {ph}) returning id::text",
        bid,
        f"p-{uuid4().hex[:8]}",
        name,
        price,
        *[base[k] for k in keys],
    )


@pytest.fixture
async def shop():
    """Un business throwaway (cu cleanup)."""
    pool = await get_pool()
    bid = str(uuid4())
    async with admin_conn(pool) as conn:
        await _make_business(conn, bid)
    try:
        yield bid
    finally:
        async with admin_conn(pool) as conn:
            await conn.execute("delete from product_relations where business_id=$1", bid)
            await conn.execute("delete from product_embeddings where business_id=$1", bid)
            await conn.execute("delete from products where business_id=$1", bid)
            await conn.execute("delete from businesses where id=$1", bid)
        await close_pool()


# --- 171b: product_relations ------------------------------------------------------------------


async def test_cross_tenant_relation_rejected_by_fk():
    """ADVERSARIAL (securitate): o relație cu `product_id` din tenant A și `related_id` din tenant B
    e respinsă STRUCTURAL de FK-ul compus (nu doar de cod). business_id=A → (A, related_B) nu există
    în products → violare de cheie străină."""
    pool = await get_pool()
    a, b = str(uuid4()), str(uuid4())
    async with admin_conn(pool) as conn:
        try:
            await _make_business(conn, a)
            await _make_business(conn, b)
            pa = await _make_product(conn, a, name="A prod")
            pb = await _make_product(conn, b, name="B prod")
            with pytest.raises(asyncpg.ForeignKeyViolationError):
                await conn.execute(
                    "insert into product_relations (business_id, product_id, related_id, kind) "
                    "values ($1, $2, $3, 'complement')",
                    a,
                    pa,
                    pb,  # related dintr-un ALT tenant → FK (a, pb) inexistent
                )
        finally:
            for bid in (a, b):
                await conn.execute("delete from product_relations where business_id=$1", bid)
                await conn.execute("delete from products where business_id=$1", bid)
                await conn.execute("delete from businesses where id=$1", bid)
    await close_pool()


async def test_commercial_constraints_rejected(shop):
    """Constrângeri comerciale: self-relation, duplicat (pereche+kind), position<0 — respinse."""
    bid = shop
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        p1 = await _make_product(conn, bid, name="P1")
        p2 = await _make_product(conn, bid, name="P2")
        # self-relation
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "insert into product_relations (business_id, product_id, related_id, kind) "
                "values ($1, $2, $2, 'complement')",
                bid,
                p1,
            )
        # position negativ
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "insert into product_relations "
                "(business_id, product_id, related_id, kind, position) "
                "values ($1, $2, $3, 'complement', -1)",
                bid,
                p1,
                p2,
            )
        # duplicat (aceeași pereche + kind)
        await conn.execute(
            "insert into product_relations (business_id, product_id, related_id, kind) "
            "values ($1, $2, $3, 'complement')",
            bid,
            p1,
            p2,
        )
        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute(
                "insert into product_relations (business_id, product_id, related_id, kind) "
                "values ($1, $2, $3, 'complement')",
                bid,
                p1,
                p2,
            )


async def test_complementary_relations_first(shop):
    """get_complementary_products citește `product_relations` (relations-first): întoarce produsul
    legat explicit prin `complement`, nu heuristica. Fallback: fără nicio relație → heuristică."""
    bid = shop
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        anchor = await _make_product(conn, bid, name="Anchor")
        related = await _make_product(conn, bid, name="Related")
        _noise = await _make_product(conn, bid, name="Noise")
        await conn.execute(
            "insert into product_relations (business_id, product_id, related_id, kind, position) "
            "values ($1, $2, $3, 'complement', 0)",
            bid,
            anchor,
            related,
        )
        rows = await get_complementary_products(conn, bid, anchor, limit=4)
        ids = [r["id"] for r in rows]
        assert related in ids, "produsul legat explicit trebuie să apară (relations-first)"
        # ancora nu se auto-recomandă
        assert anchor not in ids


# --- 171c: content_status filter --------------------------------------------------------------


async def test_published_filter_hides_draft_when_tenant_opted_in(shop):
    """Cu flagul per-tenant ON, search întoarce DOAR 'published'; 'draft' e ascuns. Test-plasă:
    visible_count > 0 (nu golim catalogul)."""
    bid = shop
    pool = await get_pool()
    if not get_settings().content_status_filter_enabled:
        pytest.skip("kill-switch global OFF")
    async with admin_conn(pool) as conn:
        pub = await _make_product(conn, bid, name="Published one", content_status="published")
        draft = await _make_product(conn, bid, name="Draft one", content_status="draft")
        # activează flagul per-tenant
        await conn.execute(
            "update businesses set settings = coalesce(settings, '{}'::jsonb) "
            "|| jsonb_build_object('content_status_filter', true) where id=$1",
            bid,
        )
        rows = await search_products(conn, bid, limit=10)
        ids = {r["id"] for r in rows}
        assert pub in ids, "produsul 'published' trebuie servit"
        assert draft not in ids, "produsul 'draft' NU trebuie servit"
        assert len(ids) > 0, "test-plasă: catalogul nu se golește"


async def test_no_filter_when_tenant_not_opted_in(shop):
    """Fără opt-in per-tenant (flag absent/false), filtrul e inactiv → chiar 'draft' e vizibil
    (zero risc de catalog gol pentru tenanții ne-backfilluiți)."""
    bid = shop
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        draft = await _make_product(conn, bid, name="Draft visible", content_status="draft")
        rows = await search_products(conn, bid, limit=10)
        assert draft in {r["id"] for r in rows}


# --- 171d: embeddings versionate --------------------------------------------------------------


async def test_versioned_embeddings_no_duplicate(shop):
    """2 rânduri de embedding pentru ACELAȘI produs (doc_type 'product' + 'review') → join-ul
    versionat (doc_type + model activ) întoarce produsul O SINGURĂ DATĂ (nu dublat)."""
    bid = shop
    pool = await get_pool()
    model = get_settings().model_embed
    vec = "[" + ",".join(["0.001"] * 1536) + "]"
    async with admin_conn(pool) as conn:
        await register_vector_codec(conn)  # list[float] → ::vector pe această conexiune
        prod = await _make_product(conn, bid, name="Embedded prod")
        for doc in ("product", "review"):
            await conn.execute(
                "insert into product_embeddings "
                "(product_id, business_id, model, doc_type, embedding, content_hash) "
                "values ($1, $2, $3, $4, $5::vector, $6)",
                prod,
                bid,
                model,
                doc,
                vec,
                f"h-{doc}",
            )
        rows = await search_products_semantic(conn, bid, [0.001] * 1536, limit=10)
        ids = [r["id"] for r in rows]
        assert ids.count(prod) == 1, f"produs duplicat în retrieval: {ids}"
