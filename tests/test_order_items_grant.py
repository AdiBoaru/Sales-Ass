"""Migrarea 008 — INSERT pe order_items pt bot_runtime, izolat prin orders (gaură F2-2).

Test `@integration` (DB real, rol bot_runtime): o linie de comandă se poate scrie DOAR într-o
comandă a businessului curent; alt tenant e blocat de politica RLS. Necesită 008 aplicat
(scripts/apply_008.py). Tranzacție rollback-uită → zero date demo poluate."""

from uuid import uuid4

import asyncpg
import pytest

from src.db.connection import close_pool, get_pool

pytestmark = pytest.mark.integration

DEMO_BIZ = "6098812a-50fc-44bd-a1ba-bc77e6399158"
OTHER_BIZ = "00000000-0000-0000-0000-000000000000"


@pytest.fixture
async def pool():
    p = await get_pool()
    yield p
    await close_pool()


async def test_order_items_insert_is_contact_business_scoped(pool):
    async with pool.acquire() as conn:
        tr = conn.transaction()
        await tr.start()
        try:
            # comandă-probă pt businessul demo (ca rol privilegiat)
            order_id = await conn.fetchval(
                "insert into orders (business_id, external_id, status, total, attribution, "
                "placed_at) values ($1, $2, 'x', 0, 'none', now()) returning id",
                DEMO_BIZ,
                f"probe-{uuid4().hex[:8]}",
            )

            await conn.execute("set local role bot_runtime")

            # business propriu → INSERT permis (grant + RLS with check trec)
            await conn.execute("select set_config('app.business_id', $1, true)", DEMO_BIZ)
            own = await conn.fetchval(
                "insert into order_items (order_id, name, quantity, unit_price) "
                "values ($1, 'probe', 1, 0) returning 1",
                order_id,
            )
            assert own == 1

            # alt tenant → INSERT în comanda demo respins de WITH CHECK (izolare prin orders)
            await conn.execute("select set_config('app.business_id', $1, true)", OTHER_BIZ)
            with pytest.raises(asyncpg.PostgresError):
                await conn.execute(
                    "insert into order_items (order_id, name, quantity, unit_price) "
                    "values ($1, 'evil', 1, 0)",
                    order_id,
                )
        finally:
            await tr.rollback()
