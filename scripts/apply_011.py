# DEPRECAT (NX-123): foloseste `python scripts/migrate.py` (runner ordonat + tracking).
# Pastrat doar pentru istoric — NU mai rula manual apply_0NN.py.
"""Aplică docs/011_bot_runtime_read_aliases_faqs.sql pe Supabase și verifică citirea.

Fix pre-producție: bot_runtime poate CITI intent_aliases + faqs (altfel sales/order
degradau la fallback). Aditiv, idempotent.

Rulează: python scripts/apply_011.py
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
SQL_FILE = Path(__file__).parent.parent / "docs" / "011_bot_runtime_read_aliases_faqs.sql"
DEMO_BIZ = "6098812a-50fc-44bd-a1ba-bc77e6399158"


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
        print("1. Aplic 011 (de două ori — idempotent) ...")
        sql = SQL_FILE.read_text(encoding="utf-8")
        await conn.execute(sql)
        await conn.execute(sql)
        print("   aplicat de două ori fără erori.")

        has_sel = await conn.fetchval(
            "select has_table_privilege('bot_runtime', 'intent_aliases', 'SELECT')"
        )
        results.append(check("bot_runtime are SELECT pe intent_aliases", has_sel, True))

        # Citire reală ca bot_runtime + app.business_id (exact calea de producție).
        await conn.execute("set role bot_runtime")
        await conn.execute("select set_config('app.business_id', $1, true)", DEMO_BIZ)
        alias_ok = faq_ok = False
        try:
            await conn.fetchval("select count(*) from intent_aliases")
            alias_ok = True
        except Exception as e:  # noqa: BLE001
            print(f"   intent_aliases citire eșuată: {type(e).__name__}")
        try:
            await conn.fetchval("select count(*) from faqs")
            faq_ok = True
        except Exception as e:  # noqa: BLE001
            print(f"   faqs citire eșuată: {type(e).__name__}")
        await conn.execute("reset role")
        results.append(check("SELECT intent_aliases ca bot_runtime reușește", alias_ok, True))
        results.append(check("SELECT faqs ca bot_runtime reușește", faq_ok, True))
    finally:
        await conn.close()

    print("\n" + ("TOATE TESTELE TREC ✓" if all(results) else "EXISTĂ TESTE PICATE ✗"))
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    asyncio.run(main())
