"""Aplică docs/004_inbound_dedupe.sql pe Supabase și verifică tabelul + RLS.

Rulează: python scripts/apply_004.py
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
SQL_FILE = Path(__file__).parent.parent / "docs" / "004_inbound_dedupe.sql"


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
        print("1. Aplic 004_inbound_dedupe.sql ...")
        await conn.execute(SQL_FILE.read_text(encoding="utf-8"))
        print("   aplicat fără erori.")

        table_exists = await conn.fetchval(
            "select exists(select 1 from information_schema.tables "
            "where table_schema='public' and table_name='inbound_dedupe')"
        )
        results.append(check("tabel inbound_dedupe există", table_exists, True))

        rls_on = await conn.fetchval(
            "select relrowsecurity from pg_class where relname='inbound_dedupe'"
        )
        results.append(check("RLS activat", rls_on, True))

        print("\n2. Test izolare ca bot_runtime:")
        await conn.execute("set role bot_runtime")
        await conn.execute(f"set app.business_id = '{DEMO_BIZ}'")

        # claim un marker pentru demo
        claimed = await conn.fetchval(
            "insert into inbound_dedupe (business_id, provider_msg_id) "
            "values ($1, 'apply004-probe') on conflict do nothing returning 1",
            DEMO_BIZ,
        )
        results.append(check("claim nou întoarce rând", claimed, 1))

        # re-claim → conflict → None
        again = await conn.fetchval(
            "insert into inbound_dedupe (business_id, provider_msg_id) "
            "values ($1, 'apply004-probe') on conflict do nothing returning 1",
            DEMO_BIZ,
        )
        results.append(check("re-claim → fără rând (dedupe)", again, None))

        # alt tenant nu vede markerul demo
        await conn.execute(f"set app.business_id = '{OTHER_BIZ}'")
        visible = await conn.fetchval("select count(*) from inbound_dedupe")
        results.append(check("alt tenant → 0 markere vizibile", visible, 0))

        # cleanup probe
        await conn.execute(f"set app.business_id = '{DEMO_BIZ}'")
        await conn.execute("delete from inbound_dedupe where provider_msg_id = 'apply004-probe'")
        await conn.execute("reset role")
    finally:
        await conn.close()

    print("\n" + ("TOATE TESTELE TREC ✓" if all(results) else "EXISTĂ TESTE PICATE ✗"))
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    asyncio.run(main())
