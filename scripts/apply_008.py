"""Aplică docs/008_order_items_insert.sql pe Supabase și verifică grant + RLS INSERT.

Adaugă INSERT pe order_items pentru bot_runtime (gaură F2-2): liniile unei comenzi se
pot scrie DOAR într-o comandă a businessului curent (izolare tranzitivă prin orders).

Rulează: python scripts/apply_008.py
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
SQL_FILE = Path(__file__).parent.parent / "docs" / "008_order_items_insert.sql"


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
    probe_order = None
    try:
        print("1. Aplic 008_order_items_insert.sql ...")
        await conn.execute(SQL_FILE.read_text(encoding="utf-8"))
        print("   aplicat fără erori.")

        has_insert = await conn.fetchval(
            "select has_table_privilege('bot_runtime', 'order_items', 'INSERT')"
        )
        results.append(check("bot_runtime are INSERT pe order_items", has_insert, True))

        policy = await conn.fetchval(
            "select count(*) from pg_policies where tablename='order_items' "
            "and policyname='bot_runtime_order_items_insert'"
        )
        results.append(check("politica RLS de insert există", policy, 1))

        print("\n2. Test izolare ca bot_runtime:")
        # Comandă-probă (ca rol privilegiat) pentru businessul demo.
        probe_order = await conn.fetchval(
            "insert into orders (business_id, external_id, status, total, attribution, placed_at) "
            "values ($1, 'apply008-probe', 'x', 0, 'none', now()) returning id",
            DEMO_BIZ,
        )
        await conn.execute("set role bot_runtime")

        await conn.execute(f"set app.business_id = '{DEMO_BIZ}'")
        own = await conn.fetchval(
            "insert into order_items (order_id, name, quantity, unit_price) "
            "values ($1, 'probe', 1, 0) returning 1",
            probe_order,
        )
        results.append(check("insert linie în comanda PROPRIE", own, 1))

        # Alt tenant NU poate atașa linii la comanda demo (WITH CHECK pe orders.business_id).
        await conn.execute(f"set app.business_id = '{OTHER_BIZ}'")
        blocked = False
        try:
            await conn.execute(
                "insert into order_items (order_id, name, quantity, unit_price) "
                "values ($1, 'evil', 1, 0)",
                probe_order,
            )
        except asyncpg.PostgresError:
            blocked = True
        results.append(check("insert din alt tenant → blocat de RLS", blocked, True))

        await conn.execute("reset role")
    finally:
        # cleanup probă (ca rol privilegiat)
        try:
            await conn.execute("reset role")
            if probe_order is not None:
                await conn.execute("delete from order_items where order_id = $1", probe_order)
                await conn.execute("delete from orders where id = $1", probe_order)
        finally:
            await conn.close()

    print("\n" + ("TOATE TESTELE TREC ✓" if all(results) else "EXISTĂ TESTE PICATE ✗"))
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    asyncio.run(main())
