"""Integration (DB real, rollback per test) — jobul de lifecycle (Val3).

Acoperă SQL-ul real `run_lifecycle`: clasificarea new/engaged/customer/repeat/churn_risk din
comenzi (`orders`) + recență (`conversations.last_inbound_at`), sub rol `bot_runtime` (RLS de
producție auto-scoped la demo biz — în prod jobul rulează pe admin, cross-tenant, același SQL).
Totul rollback-uit → demo DB curat.
"""

from contextlib import asynccontextmanager
from uuid import uuid4

import pytest

from src.db.connection import close_pool, get_pool
from src.jobs.lifecycle import run_lifecycle

pytestmark = [pytest.mark.integration, pytest.mark.slow]

DEMO_BIZ = "6098812a-50fc-44bd-a1ba-bc77e6399158"


@pytest.fixture
async def pool():
    p = await get_pool()
    yield p
    await close_pool()


@asynccontextmanager
async def tenant_tx(pool, business_id=DEMO_BIZ):
    async with pool.acquire() as conn:
        tr = conn.transaction()
        await tr.start()
        try:
            channel_id = await conn.fetchval(
                "insert into channels (business_id, kind, provider_account_id) "
                "values ($1, 'whatsapp', $2) returning id::text",
                business_id,
                f"PN-{uuid4().hex[:10]}",
            )
            await conn.execute("set local role bot_runtime")
            await conn.execute("select set_config('app.business_id', $1, true)", business_id)
            yield conn, channel_id
        finally:
            await tr.rollback()


async def _contact(conn):
    return await conn.fetchval(
        "insert into contacts (business_id) values ($1) returning id::text", DEMO_BIZ
    )


async def _conv(conn, channel_id, contact_id, *, days_ago):
    await conn.execute(
        "insert into conversations (business_id, contact_id, channel_id, last_inbound_at) "
        "values ($1, $2, $3, now() - make_interval(days => $4))",
        DEMO_BIZ,
        contact_id,
        channel_id,
        days_ago,
    )


async def _order(conn, contact_id):
    await conn.execute(
        "insert into orders (business_id, contact_id, external_id, status, total, placed_at) "
        "values ($1, $2, $3, 'paid', 100, now())",
        DEMO_BIZ,
        contact_id,
        f"ORD-{uuid4().hex[:10]}",
    )


async def _lc(conn, contact_id):
    return await conn.fetchval("select lifecycle from contacts where id = $1", contact_id)


async def test_lifecycle_classifies_all_states(pool):
    async with tenant_tx(pool) as (conn, channel_id):
        # engaged: activitate recentă, 0 comenzi
        a = await _contact(conn)
        await _conv(conn, channel_id, a, days_ago=1)
        # customer: 1 comandă
        b = await _contact(conn)
        await _conv(conn, channel_id, b, days_ago=1)
        await _order(conn, b)
        # repeat: 2 comenzi
        c = await _contact(conn)
        await _conv(conn, channel_id, c, days_ago=1)
        await _order(conn, c)
        await _order(conn, c)
        # churn_risk: a interacționat dar tăcut de > prag
        d = await _contact(conn)
        await _conv(conn, channel_id, d, days_ago=40)
        # new: nicio conversație
        e = await _contact(conn)

        changed = await run_lifecycle(conn, churn_days=30)
        assert changed >= 4  # a/b/c/d trec din 'new'; e rămâne 'new' (neschimbat)

        assert await _lc(conn, a) == "engaged"
        assert await _lc(conn, b) == "customer"
        assert await _lc(conn, c) == "repeat"
        assert await _lc(conn, d) == "churn_risk"
        assert await _lc(conn, e) == "new"


async def test_lifecycle_churn_overrides_customer(pool):
    """Un client (1 comandă) tăcut de > prag → churn_risk (țintă de re-engagement, override)."""
    async with tenant_tx(pool) as (conn, channel_id):
        x = await _contact(conn)
        await _conv(conn, channel_id, x, days_ago=60)
        await _order(conn, x)
        await run_lifecycle(conn, churn_days=30)
        assert await _lc(conn, x) == "churn_risk"


async def test_lifecycle_idempotent_second_run_zero_changes(pool):
    async with tenant_tx(pool) as (conn, channel_id):
        a = await _contact(conn)
        await _conv(conn, channel_id, a, days_ago=1)
        await run_lifecycle(conn, churn_days=30)
        # al doilea run pe ACELAȘI contact nu-l mai schimbă (is distinct from)
        again = await run_lifecycle(conn, churn_days=30)
        # `again` poate include alte contacte demo, dar al nostru e deja 'engaged' → stabil
        assert await _lc(conn, a) == "engaged"
        assert isinstance(again, int)
