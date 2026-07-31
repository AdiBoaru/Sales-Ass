"""NX-191 — fereastra promoţiei, verificată cu ORACOL INDEPENDENT (cifre scrise de mână).

De ce separat de `test_search_products.py`: acolo oracolele importă expresiile din producţie, ca să
nu se contrazică cu regula la fiecare schimbare de dată. Bun contra driftului, dar inutil pentru
regula însăşi — dacă fereastra ar dispărea din producţie, oracolul ar dispărea odată cu ea şi
testul ar trece. Aici preţurile aşteptate sunt constante, deci regula chiar e verificată.

Lipsa acestui test s-a văzut pe 2026-07-31: promoţia demo expirase pe 24, producţia întorcea corect
preţul de listă, iar trei teste picau fiindcă oracolele lor cereau preţul de promoţie. Regula era
implementată şi neacoperită — singurul semnal a fost un eşec care arăta ca o regresie.

Integration (businesses throwaway, curăţate la teardown).
"""

from datetime import date, timedelta
from uuid import uuid4

import pytest

from src.db.connection import admin_conn, close_pool, get_pool
from src.db.queries.catalog import get_products_by_ids

pytestmark = [pytest.mark.integration]

LIST_PRICE = 100
SALE_PRICE = 60


async def _make_product(conn, bid, *, sale_start, sale_end, with_variant: bool):
    await conn.execute(
        "insert into businesses (id, slug, name, vertical, status, default_locale) "
        "values ($1,$2,'NX-191 fereastra','beauty_salon','active','ro')",
        bid,
        f"nx191-{uuid4().hex[:8]}",
    )
    cat = await conn.fetchval(
        "insert into categories (business_id, slug, name) values ($1,'cat','Cat') returning id", bid
    )
    pid = await conn.fetchval(
        "insert into products (business_id, primary_category_id, slug, name, price, sale_price, "
        "sale_start, sale_end, status) "
        "values ($1,$2,'prod','Prod',$3,$4,$5,$6,'active') returning id",
        bid,
        cat,
        LIST_PRICE,
        SALE_PRICE,
        sale_start,
        sale_end,
    )
    if with_variant:
        # Varianta poartă aceeaşi promoţie, dar NU are fereastră proprie — o moşteneşte pe a
        # produsului. Cazul care contează: promoţie expirată pe produs ⇒ variantă la preţ de listă.
        await conn.execute(
            "insert into product_variants (business_id, product_id, label, sku, price, sale_price, "
            "stock) values ($1,$2,'V',$3,$4,$5,5)",
            bid,
            pid,
            f"SKU-{uuid4().hex[:8]}",
            LIST_PRICE,
            SALE_PRICE,
        )
    return pid


@pytest.fixture
async def shop(request):
    """(sale_start, sale_end, with_variant) → (business_id, product_id)."""
    sale_start, sale_end, with_variant = request.param
    pool = await get_pool()
    bid = str(uuid4())
    async with admin_conn(pool) as conn:
        pid = await _make_product(
            conn, bid, sale_start=sale_start, sale_end=sale_end, with_variant=with_variant
        )
    try:
        yield bid, pid
    finally:
        async with admin_conn(pool) as conn:
            await conn.execute("delete from product_variants where business_id=$1", bid)
            await conn.execute("delete from products where business_id=$1", bid)
            await conn.execute("delete from categories where business_id=$1", bid)
            await conn.execute("delete from businesses where id=$1", bid)
        await close_pool()


IERI = date.today() - timedelta(days=1)
ALALTAIERI = date.today() - timedelta(days=2)
MAINE = date.today() + timedelta(days=1)


@pytest.mark.parametrize(
    ("shop", "expected", "de_ce"),
    [
        pytest.param(
            (ALALTAIERI, IERI, False),
            LIST_PRICE,
            "promoţie ÎNCHEIATĂ ieri — un preţ de promoţie afişat acum e o minciună comercială",
            id="expirata",
        ),
        pytest.param(
            (MAINE, MAINE, False),
            LIST_PRICE,
            "promoţie care ÎNCEPE mâine — nu se anticipează",
            id="viitoare",
        ),
        pytest.param(
            (IERI, MAINE, False),
            SALE_PRICE,
            "promoţie ÎN fereastră — preţul de promoţie e cel real",
            id="activa",
        ),
        pytest.param(
            (None, None, False),
            SALE_PRICE,
            "fereastră deschisă (ambele NULL) = promoţie permanentă; rândurile vechi rămân valide",
            id="fara-fereastra",
        ),
        pytest.param(
            (ALALTAIERI, IERI, True),
            LIST_PRICE,
            "varianta moşteneşte fereastra produsului — altfel promoţia expirată supravieţuieşte "
            "exact pe preţul EFECTIV, cel pe care îl vede clientul",
            id="expirata-cu-varianta",
        ),
        pytest.param(
            (IERI, MAINE, True),
            SALE_PRICE,
            "varianta în fereastră — preţul de promoţie al variantei",
            id="activa-cu-varianta",
        ),
    ],
    indirect=["shop"],
)
async def test_effective_price_respects_sale_window(shop, expected, de_ce):
    bid, pid = shop
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        [prod] = await get_products_by_ids(conn, bid, [pid], limit=1)
    assert prod["price"] == expected, de_ce
