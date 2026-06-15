"""NX-04 — assert de izolare la checkout din bot_pool.

Două straturi:
  • UNIT (CI, fără DB): logica `_check_isolation` (rol greșit, GUC nesetat,
    mismatch) + flag-ul `_isolation_enabled`.
  • INTEGRATION (DB real): assertul rulează pe drumul real al lui `tenant_conn`
    cu valorile corecte (happy), iar `DB_ISOLATION_ASSERT=off` îl sare.
"""

import pytest

from src.config import get_settings
from src.db import connection as conn_mod
from src.db.connection import _check_isolation, _isolation_enabled, close_pool, tenant_conn
from src.db.errors import IsolationError

DEMO_BIZ = "6098812a-50fc-44bd-a1ba-bc77e6399158"
OTHER_BIZ = "00000000-0000-0000-0000-000000000000"


# --- UNIT: logica de assert (fără DB) -----------------------------------------


def test_check_isolation_happy():
    _check_isolation("bot_runtime", DEMO_BIZ, DEMO_BIZ)  # nu ridică


def test_check_isolation_wrong_role():
    """Failure: rol ≠ bot_runtime → IsolationError."""
    with pytest.raises(IsolationError, match="bot_runtime"):
        _check_isolation("postgres", DEMO_BIZ, DEMO_BIZ)


def test_check_isolation_guc_empty():
    """GUC nesetat (gol) → IsolationError."""
    with pytest.raises(IsolationError, match="app.business_id"):
        _check_isolation("bot_runtime", "", DEMO_BIZ)


def test_check_isolation_guc_none():
    with pytest.raises(IsolationError, match="app.business_id"):
        _check_isolation("bot_runtime", None, DEMO_BIZ)


def test_check_isolation_mismatch():
    """Edge: GUC pe ALT business decât cel cerut → reuse murdar de conexiune."""
    with pytest.raises(IsolationError, match="reuse murdar"):
        _check_isolation("bot_runtime", OTHER_BIZ, DEMO_BIZ)


def test_isolation_enabled_default(monkeypatch):
    monkeypatch.setattr(get_settings(), "db_isolation_assert", "strict")
    assert _isolation_enabled() is True


def test_isolation_disabled_flag(monkeypatch):
    monkeypatch.setattr(get_settings(), "db_isolation_assert", "off")
    assert _isolation_enabled() is False


# --- INTEGRATION: wiring pe DB real -------------------------------------------


@pytest.fixture
async def _pools():
    yield
    await close_pool()


@pytest.mark.integration
async def test_tenant_conn_runs_assert_happy(_pools, monkeypatch):
    """Happy: pe DB real, tenant_conn cheamă assertul cu (bot_runtime, DEMO, DEMO)
    și dă conexiunea fără excepție; query-ul rulează."""
    calls = []
    real = conn_mod._check_isolation

    def spy(user, biz, expected):
        calls.append((user, biz, expected))
        real(user, biz, expected)

    monkeypatch.setattr(conn_mod, "_check_isolation", spy)
    async with tenant_conn(DEMO_BIZ) as c:
        n = await c.fetchval("select count(*) from products")
    assert n == 500
    assert calls == [("bot_runtime", DEMO_BIZ, DEMO_BIZ)]


@pytest.mark.integration
async def test_flag_off_skips_assert(_pools, monkeypatch):
    """Failure-mode flag: DB_ISOLATION_ASSERT=off → assertul NU rulează."""
    monkeypatch.setattr(get_settings(), "db_isolation_assert", "off")
    calls = []
    monkeypatch.setattr(conn_mod, "_check_isolation", lambda *a: calls.append(a))
    async with tenant_conn(DEMO_BIZ) as c:
        await c.fetchval("select 1")
    assert calls == []
