"""Aplică docs/006_semantic_cache_v2.sql pe Supabase și verifică coloanele + indexul.

Rulează: python scripts/apply_006.py
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
SQL_FILE = Path(__file__).parent.parent / "docs" / "006_semantic_cache_v2.sql"


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
    if not DSN:
        print("EROARE: SUPABASE_DB_URL lipsește din .env")
        sys.exit(2)

    conn = await connect()
    results = []
    try:
        print("1. Aplic 006_semantic_cache_v2.sql ...")
        await conn.execute(SQL_FILE.read_text(encoding="utf-8"))
        print("   aplicat fără erori.")

        cols = await conn.fetch(
            "select column_name from information_schema.columns where table_name = 'semantic_cache'"
        )
        names = {r["column_name"] for r in cols}
        for col in ("canonical_hash", "volatility_class", "embedding_model", "is_curated"):
            results.append(check(f"coloana {col}", col in names, True))

        idx = await conn.fetchval(
            "select indisunique from pg_indexes pi "
            "join pg_class c on c.relname = pi.indexname "
            "join pg_index i on i.indexrelid = c.oid "
            "where pi.indexname = 'idx_semcache_exact'"
        )
        results.append(check("index idx_semcache_exact unic", idx, True))
    finally:
        await conn.close()

    print("\n" + ("TOATE TESTELE TREC ✓" if all(results) else "EXISTĂ TESTE PICATE ✗"))
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    asyncio.run(main())
