# DEPRECAT (NX-123): foloseste `python scripts/migrate.py` (runner ordonat + tracking).
# Pastrat doar pentru istoric — NU mai rula manual apply_0NN.py.
"""Aplică docs/007_semantic_cache_invalidation.sql pe Supabase și verifică coloanele.

Rulează: python scripts/apply_007.py
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
SQL_FILE = Path(__file__).parent.parent / "docs" / "007_semantic_cache_invalidation.sql"


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


async def _cols(conn, table):
    rows = await conn.fetch(
        "select column_name from information_schema.columns where table_name = $1", table
    )
    return {r["column_name"] for r in rows}


async def main():
    if not DSN:
        print("EROARE: SUPABASE_DB_URL lipsește din .env")
        sys.exit(2)

    conn = await connect()
    results = []
    try:
        print("1. Aplic 007_semantic_cache_invalidation.sql ...")
        await conn.execute(SQL_FILE.read_text(encoding="utf-8"))
        print("   aplicat fără erori.")

        semcache = await _cols(conn, "semantic_cache")
        for col in ("retrieval_signature", "data_version"):
            results.append(check(f"semantic_cache.{col}", col in semcache, True))

        businesses = await _cols(conn, "businesses")
        results.append(check("businesses.data_version", "data_version" in businesses, True))
    finally:
        await conn.close()

    print("\n" + ("TOATE TESTELE TREC ✓" if all(results) else "EXISTĂ TESTE PICATE ✗"))
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    asyncio.run(main())
