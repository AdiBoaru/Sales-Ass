# DEPRECAT (NX-123): foloseste `python scripts/migrate.py` (runner ordonat + tracking).
# Pastrat doar pentru istoric — NU mai rula manual apply_0NN.py.
"""Aplică docs/013_usage_cached_tokens.sql pe Supabase și verifică coloana (NX-78/NX-103 cost obs).

Adaugă `usage_daily.cached_tokens` (bigint, default 0). Aditiv, idempotent — rulabil de
două ori fără eroare.

Rulează: python scripts/apply_013.py
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
SQL_FILE = Path(__file__).parent.parent / "docs" / "013_usage_cached_tokens.sql"


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
        print("1. Aplic 013_usage_cached_tokens.sql (de două ori — idempotent) ...")
        sql = SQL_FILE.read_text(encoding="utf-8")
        await conn.execute(sql)
        await conn.execute(sql)  # a doua oară NU trebuie să crape
        print("   aplicat de două ori fără erori.")

        col = await conn.fetchval(
            "select data_type from information_schema.columns "
            "where table_name = 'usage_daily' and column_name = 'cached_tokens'"
        )
        results.append(check("usage_daily.cached_tokens există", col, "bigint"))
    finally:
        await conn.close()

    print("\n" + ("TOATE TESTELE TREC ✓" if all(results) else "EXISTĂ TESTE PICATE ✗"))
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    asyncio.run(main())
