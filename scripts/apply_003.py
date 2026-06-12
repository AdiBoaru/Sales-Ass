"""Aplică docs/003_bot_runtime_role.sql pe Supabase și verifică izolarea RLS.

Rulează: python scripts/apply_003.py
Testează cu tabela `products` (500 rânduri reale pt business-ul demo).
"""

import asyncio
import os
import socket
import ssl
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

import asyncpg
from dotenv import load_dotenv

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    sys.stdout.reconfigure(encoding="utf-8")

load_dotenv()

DSN = os.environ.get("SUPABASE_DB_URL") or os.environ.get("DATABASE_URL")
DEMO_BIZ = "6098812a-50fc-44bd-a1ba-bc77e6399158"
OTHER_BIZ = "00000000-0000-0000-0000-000000000000"
SQL_FILE = Path(__file__).parent.parent / "docs" / "003_bot_runtime_role.sql"


async def connect():
    p = urlparse(DSN)
    ip = socket.getaddrinfo(p.hostname, p.port or 5432, socket.AF_INET, socket.SOCK_STREAM)[0][4][0]
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return await asyncpg.connect(
        host=ip,
        port=p.port or 5432,
        user=unquote(p.username),
        password=unquote(p.password),
        database=(p.path or "/postgres").lstrip("/"),
        ssl=ctx,
    )


def check(label, got, expected):
    ok = got == expected
    print(f"  [{'OK ' if ok else 'FAIL'}] {label}: {got} (aștept {expected})")
    return ok


async def main():
    conn = await connect()
    results = []
    try:
        print("1. Aplic 003_bot_runtime_role.sql ...")
        sql = SQL_FILE.read_text(encoding="utf-8")
        await conn.execute(sql)
        print("   aplicat fără erori.")

        role_exists = await conn.fetchval(
            "select exists(select 1 from pg_roles where rolname='bot_runtime')"
        )
        results.append(check("rol bot_runtime există", role_exists, True))

        constraint_exists = await conn.fetchval(
            "select exists(select 1 from pg_constraint where conname='chk_state_size')"
        )
        results.append(check("CHECK chk_state_size pe conversations", constraint_exists, True))

        print("\n2. Test izolare RLS ca bot_runtime (tabela products = 500 rânduri demo):")
        await conn.execute("set role bot_runtime")

        # fără app.business_id setat → 0 rânduri
        c0 = await conn.fetchval("select count(*) from products")
        results.append(check("fără business_id setat → produse vizibile", c0, 0))

        # business_id demo → toate cele 500
        await conn.execute(f"set app.business_id = '{DEMO_BIZ}'")
        c1 = await conn.fetchval("select count(*) from products")
        results.append(check("business_id demo → produse vizibile", c1, 500))

        # alt business_id → 0
        await conn.execute(f"set app.business_id = '{OTHER_BIZ}'")
        c2 = await conn.fetchval("select count(*) from products")
        results.append(check("alt business_id → produse vizibile", c2, 0))

        # businesses (policy pe id, nu business_id)
        await conn.execute(f"set app.business_id = '{DEMO_BIZ}'")
        b1 = await conn.fetchval("select count(*) from businesses")
        results.append(check("businesses: demo → vizibil", b1, 1))
        await conn.execute(f"set app.business_id = '{OTHER_BIZ}'")
        b0 = await conn.fetchval("select count(*) from businesses")
        results.append(check("businesses: alt id → vizibil", b0, 0))

        print("\n3. Test negativ: insert analytics_events cu ALT business_id (WITH CHECK):")
        await conn.execute(f"set app.business_id = '{DEMO_BIZ}'")
        rejected = False
        tx = conn.transaction()
        await tx.start()
        try:
            await conn.execute(
                "insert into analytics_events(business_id, event_type) values ($1, 'rls_test')",
                OTHER_BIZ,
            )
        except asyncpg.PostgresError:
            rejected = True
        finally:
            await tx.rollback()
        results.append(check("insert cu business_id străin → respins", rejected, True))

        await conn.execute("reset role")
    finally:
        await conn.close()

    print("\n" + ("TOATE TESTELE TREC ✓" if all(results) else "EXISTĂ TESTE PICATE ✗"))
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    asyncio.run(main())
