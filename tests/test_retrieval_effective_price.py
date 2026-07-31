"""NX-203 — snapshotul trebuie să poarte preţul EFECTIV, nu preţul de listă.

Blocantul reparat aici: `load_catalog` citea `p.price`, în timp ce retrieval-ul şi clientul văd
`_EFFECTIVE_PRICE` (promoţie în fereastră + minimul variantelor). Un produs de 100 lei vândut cu 60
încălca aparent pragul „sub 90", iar raportul ieşea `verified` cu `forbidden_rate = 1.0`. Un
fals-pozitiv e mai rău decât o stare neverificată: e un răspuns greşit dat cu încredere.

Lanţul întreg, nu doar proiecţia: `load_catalog` → `constraints.evaluate` → `run_benchmark`. Un test
doar pe SQL ar fi trecut şi cu un snapshot corect pe care harness-ul îl foloseşte greşit.

Integration (businesses throwaway, curăţate la teardown).
"""

from datetime import date, timedelta
from uuid import uuid4

import pytest

from src.db.connection import admin_conn, close_pool, get_pool
from src.evals.retrieval.catalog import load_catalog
from src.evals.retrieval.constraints import SATISFIES, VIOLATES, evaluate
from src.evals.retrieval.harness import VERIFIED, RunConfig, run_benchmark
from src.evals.retrieval.schema import HardConstraint, Provenance, QrelsQuery, QrelsSet

pytestmark = [pytest.mark.integration]

LIST = 100
EFFECTIV = 60
PRAG = 90  # între cele două: separă „preţ de listă" de „preţ real"

IERI = date.today() - timedelta(days=1)
ALALTAIERI = date.today() - timedelta(days=2)
MAINE = date.today() + timedelta(days=1)


async def _product(conn, bid, cat, slug, *, sale=None, window=(None, None), variant=None):
    pid = await conn.fetchval(
        "insert into products (business_id, primary_category_id, slug, name, price, sale_price, "
        "sale_start, sale_end, status, content_status) "
        "values ($1,$2,$3,$3,$4,$5,$6,$7,'active','published') returning id",
        bid,
        cat,
        slug,
        LIST,
        sale,
        window[0],
        window[1],
    )
    if variant is not None:
        await conn.execute(
            "insert into product_variants (business_id, product_id, label, sku, price, stock) "
            "values ($1,$2,'V',$3,$4,5)",
            bid,
            pid,
            f"SKU-{uuid4().hex[:8]}",
            variant,
        )
    return str(pid)


@pytest.fixture
async def shop():
    """Patru produse cu ACELAŞI preţ de listă (100), dar preţuri efective diferite."""
    pool = await get_pool()
    bid = str(uuid4())
    async with admin_conn(pool) as conn:
        await conn.execute(
            "insert into businesses (id, slug, name, vertical, status, default_locale) "
            "values ($1,$2,'NX-203 pret efectiv','beauty_salon','active','ro')",
            bid,
            f"nx203-{uuid4().hex[:8]}",
        )
        cat = await conn.fetchval(
            "insert into categories (business_id, slug, name) values ($1,'creme','Creme') "
            "returning id",
            bid,
        )
        ids = {
            # promoţie ACTIVĂ → efectiv 60
            "promo": await _product(conn, bid, cat, "promo", sale=EFFECTIV, window=(IERI, MAINE)),
            # fără promoţie, dar VARIANTĂ mai ieftină → efectiv 60
            "varianta": await _product(conn, bid, cat, "varianta", variant=EFFECTIV),
            # promoţie EXPIRATĂ → efectiv rămâne 100
            "expirat": await _product(
                conn, bid, cat, "expirat", sale=EFFECTIV, window=(ALALTAIERI, IERI)
            ),
            # nimic → 100
            "listat": await _product(conn, bid, cat, "listat"),
        }
    try:
        yield bid, ids, cat
    finally:
        async with admin_conn(pool) as conn:
            await conn.execute("delete from product_variants where business_id=$1", bid)
            await conn.execute("delete from products where business_id=$1", bid)
            await conn.execute("delete from categories where business_id=$1", bid)
            await conn.execute("delete from businesses where id=$1", bid)
        await close_pool()


async def test_snapshot_carries_effective_price_not_list_price(shop):
    bid, ids, _cat = shop
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        catalog = await load_catalog(conn, bid)

    assert catalog.products[ids["promo"]]["price"] == EFFECTIV
    assert catalog.products[ids["varianta"]]["price"] == EFFECTIV
    assert catalog.products[ids["expirat"]]["price"] == LIST
    assert catalog.products[ids["listat"]]["price"] == LIST
    # preţul de listă rămâne, ca un fals-pozitiv pe preţ să fie investigabil
    assert catalog.products[ids["promo"]]["list_price"] == LIST


async def test_effective_price_satisfies_threshold_that_list_price_would_violate(shop):
    """Miezul: prag 90, listă 100, efectiv 60 → SATISFIES, nu VIOLATES."""
    bid, ids, _cat = shop
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        catalog = await load_catalog(conn, bid)

    prag = {"facet": "price", "op": "lte", "value": PRAG}
    assert evaluate(catalog.products[ids["promo"]], prag) == SATISFIES
    assert evaluate(catalog.products[ids["varianta"]], prag) == SATISFIES
    # contra-proba: promoţia expirată NU salvează produsul
    assert evaluate(catalog.products[ids["expirat"]], prag) == VIOLATES
    assert evaluate(catalog.products[ids["listat"]], prag) == VIOLATES


async def test_benchmark_does_not_report_false_violation_on_discounted_products(shop):
    """Lanţul complet. Retrieval-ul întoarce cele două produse ieftinite ⇒ zero încălcări."""
    bid, ids, _cat = shop
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        catalog = await load_catalog(conn, bid)

    qset = QrelsSet(
        business_id=bid,
        queries=[
            QrelsQuery(
                id="q-buget",
                query="crema sub 90 de lei",
                provenance=Provenance.synthetic,
                catalog_version=catalog.version,
                hard_constraints=[HardConstraint(facet="price", op="lte", value=PRAG)],
            )
        ],
    )
    ieftine = [ids["promo"], ids["varianta"]]
    report = run_benchmark(qset, lambda _q: ieftine, RunConfig(label="reduse"), catalog)

    assert report.constraint_validation == VERIFIED
    assert report.forbidden_violation_rate == 0.0, (
        "produse de 100 lei vândute cu 60 nu încalcă pragul de 90 — snapshotul pe preţ de listă "
        "raporta 1.0 aici, marcat ca verificat"
    )

    # contra-proba pe acelaşi lanţ: produsul cu promoţia expirată CHIAR încalcă pragul
    scump = run_benchmark(qset, lambda _q: [ids["expirat"]], RunConfig(label="expirat"), catalog)
    assert scump.forbidden_violation_rate == 1.0
