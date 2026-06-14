"""Teste pentru pool-uri + izolare tenant (NX-50: rol de LOGIN bot_runtime).

Două straturi:
  • UNIT (fără DB, rulează în CI): plasa de rol la boot — `_assert_bot_role`
    refuză orice identitate ≠ bot_runtime (failure path: parolă/DSN greșit →
    eroare explicită la pornire, nu un drum superuser tăcut).
  • INTEGRATION (DB real Supabase, excluse din CI cu `-m "not integration"`):
    izolarea efectivă pe tenant prin RLS. Rulează local: pytest -m integration
    Necesită SUPABASE_DB_URL (+ opțional DATABASE_URL_BOT) în .env, 003+004 aplicate.
"""

import pytest

from src.db.connection import _assert_bot_role, close_pool, get_bot_pool, tenant_conn

DEMO_BIZ = "6098812a-50fc-44bd-a1ba-bc77e6399158"
OTHER_BIZ = "00000000-0000-0000-0000-000000000000"


# --- UNIT: plasa de rol la boot (failure path), fără DB -----------------------


class _FakeConn:
    """Stub minimal: `_assert_bot_role` cere doar `fetchval('select current_user')`."""

    def __init__(self, user: str) -> None:
        self._user = user

    async def fetchval(self, _query: str) -> str:
        return self._user


async def test_assert_bot_role_rejects_non_bot():
    """DSN greșit → rol efectiv ≠ bot_runtime → RuntimeError explicit (la boot)."""
    with pytest.raises(RuntimeError, match="bot_runtime"):
        await _assert_bot_role(_FakeConn("postgres"))


async def test_assert_bot_role_accepts_bot():
    await _assert_bot_role(_FakeConn("bot_runtime"))  # nu ridică


# --- INTEGRATION: izolare reală prin RLS --------------------------------------


@pytest.fixture
async def _pools():
    yield
    await close_pool()


@pytest.mark.integration
async def test_tenant_sees_own_products(_pools):
    """Happy: checkout pe A → doar datele lui A."""
    async with tenant_conn(DEMO_BIZ) as conn:
        count = await conn.fetchval("select count(*) from products")
    assert count == 500


@pytest.mark.integration
async def test_tenant_isolation_blocks_other(_pools):
    async with tenant_conn(OTHER_BIZ) as conn:
        count = await conn.fetchval("select count(*) from products")
    assert count == 0


@pytest.mark.integration
async def test_role_is_bot_runtime(_pools):
    """DoD: orice query path → current_user = bot_runtime."""
    async with tenant_conn(DEMO_BIZ) as conn:
        role = await conn.fetchval("select current_user")
    assert role == "bot_runtime"


@pytest.mark.integration
async def test_fail_closed_without_business_id(_pools):
    """DoD: conexiune bot fără app.business_id setat → SELECT pe tabel RLS = 0 rânduri."""
    pool = await get_bot_pool()
    async with pool.acquire() as conn:
        await conn.execute("select set_config('app.business_id', '', false)")
        count = await conn.fetchval("select count(*) from products")
    assert count == 0


@pytest.mark.integration
async def test_business_id_reset_after_checkout(_pools):
    """Edge (RESET verificat): după checkout pe DEMO, GUC-ul e golit → o conexiune
    din pool nu mai poartă scope-ul precedent."""
    async with tenant_conn(DEMO_BIZ) as conn:
        assert await conn.fetchval("select current_setting('app.business_id', true)") == DEMO_BIZ
    pool = await get_bot_pool()
    async with pool.acquire() as conn:
        leftover = await conn.fetchval("select current_setting('app.business_id', true)")
    assert leftover in ("", None)


@pytest.mark.integration
async def test_two_consecutive_checkouts_isolated(_pools):
    """Edge: două checkout-uri consecutive, tenanți diferiți → al doilea NU vede
    datele primului."""
    async with tenant_conn(DEMO_BIZ) as conn:
        demo = await conn.fetchval("select count(*) from products")
    async with tenant_conn(OTHER_BIZ) as conn:
        other = await conn.fetchval("select count(*) from products")
    assert demo == 500
    assert other == 0
