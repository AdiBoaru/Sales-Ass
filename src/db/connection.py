"""Pool asyncpg + izolare multi-tenant per conexiune.

Modelul de securitate (vezi docs/003_bot_runtime_role.sql + schema_reference.md):
pe Supabase ne conectăm prin pooler ca `postgres`, dar la fiecare conexiune
coborâm la rolul `bot_runtime` (fără bypassrls) și setăm `app.business_id`.
Astfel RLS devine plasă: un query fără filtru de tenant → zero rezultate, nu
datele altui client (principiul 7).

Folosire:
    pool = await get_pool()
    async with tenant_conn(pool, business_id) as conn:
        rows = await conn.fetch("select * from products limit 5")
        # rândurile sunt DEJA filtrate la business_id de RLS
"""

import socket
import ssl
import sys
from contextlib import asynccontextmanager
from urllib.parse import unquote, urlparse

import asyncpg

from src.config import get_settings

_pool: asyncpg.Pool | None = None


def _connect_kwargs(dsn: str) -> dict:
    """Pe Windows, resolverul async al asyncpg (getaddrinfo) e flaky, iar
    conexiunea directă Supabase nu se rezolvă pe IPv4. Rezolvăm host-ul în IPv4
    sincron și conectăm pe IP, cu SSL fără verificare de hostname.
    Pe Linux/VPS (prod) folosim DSN-ul direct, curat.
    """
    if sys.platform != "win32":
        return {"dsn": dsn}

    p = urlparse(dsn)
    ipv4 = socket.getaddrinfo(p.hostname, p.port or 5432, socket.AF_INET, socket.SOCK_STREAM)[0][4][
        0
    ]
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return {
        "host": ipv4,
        "port": p.port or 5432,
        "user": unquote(p.username),
        "password": unquote(p.password),
        "database": (p.path or "/postgres").lstrip("/"),
        "ssl": ctx,
    }


async def get_pool() -> asyncpg.Pool:
    """Pool singleton per proces. Lazy-init la primul apel."""
    global _pool
    if _pool is None:
        dsn = get_settings().supabase_db_url
        _pool = await asyncpg.create_pool(
            **_connect_kwargs(dsn),
            min_size=1,
            max_size=10,
            # fără prepared statement cache: pooler-ul Supabase (pgbouncer)
            # nu garantează aceeași sesiune backend între statement-uri
            statement_cache_size=0,
        )
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


@asynccontextmanager
async def tenant_conn(pool: asyncpg.Pool, business_id: str):
    """Conexiune cu izolare de tenant activă.

    Coboară la rolul bot_runtime (RLS activ) și setează app.business_id pentru
    durata acestei conexiuni. La final, resetează ca să nu „murdărim" conexiunea
    întoarsă în pool.
    """
    async with pool.acquire() as conn:
        await conn.execute("set role bot_runtime")
        # set_config(..., is_local=false) → ține pe sesiune; quoting safe pe param
        await conn.execute("select set_config('app.business_id', $1, false)", business_id)
        try:
            yield conn
        finally:
            await conn.execute("reset role")
            await conn.execute("select set_config('app.business_id', '', false)")
