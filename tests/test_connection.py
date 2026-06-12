"""Teste integration pentru pool + izolare tenant (RLS).

Ating DB-ul real Supabase → marcate `integration`, EXCLUSE din CI
(`pytest -m "not integration"`). Rulează local: pytest -m integration
Necesită SUPABASE_DB_URL în .env + 003 aplicat.
"""

import pytest

from src.db.connection import close_pool, get_pool, tenant_conn

pytestmark = pytest.mark.integration

DEMO_BIZ = "6098812a-50fc-44bd-a1ba-bc77e6399158"
OTHER_BIZ = "00000000-0000-0000-0000-000000000000"


@pytest.fixture
async def pool():
    p = await get_pool()
    yield p
    await close_pool()


async def test_tenant_sees_own_products(pool):
    async with tenant_conn(pool, DEMO_BIZ) as conn:
        count = await conn.fetchval("select count(*) from products")
    assert count == 500


async def test_tenant_isolation_blocks_other(pool):
    async with tenant_conn(pool, OTHER_BIZ) as conn:
        count = await conn.fetchval("select count(*) from products")
    assert count == 0


async def test_role_is_bot_runtime(pool):
    async with tenant_conn(pool, DEMO_BIZ) as conn:
        role = await conn.fetchval("select current_user")
    assert role == "bot_runtime"


async def test_connection_resets_after_use(pool):
    """După tenant_conn, conexiunea întoarsă în pool nu mai are rolul/scope-ul."""
    async with tenant_conn(pool, DEMO_BIZ) as conn:
        pass
    # aceeași conexiune, reutilizată: rolul trebuie să fie iar postgres
    async with pool.acquire() as conn:
        role = await conn.fetchval("select current_user")
    assert role != "bot_runtime"
