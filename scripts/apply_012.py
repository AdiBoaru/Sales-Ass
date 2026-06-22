# DEPRECAT (NX-123): foloseste `python scripts/migrate.py` (runner ordonat + tracking).
# Pastrat doar pentru istoric — NU mai rula manual apply_0NN.py.
"""Aplică docs/012_inbound_dedupe_completion.sql (NX-86): watermark claimed_at/completed_at
pe inbound_dedupe + index orfani + GRANT UPDATE pentru bot_runtime.

Rulează: python scripts/apply_012.py
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
SQL_FILE = Path(__file__).parent.parent / "docs" / "012_inbound_dedupe_completion.sql"


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
        print("1. Aplic 012_inbound_dedupe_completion.sql ...")
        await conn.execute(SQL_FILE.read_text(encoding="utf-8"))
        print("   aplicat fără erori.")

        cols = await conn.fetchval(
            "select count(*) from information_schema.columns "
            "where table_name = 'inbound_dedupe' and column_name in ('claimed_at','completed_at')"
        )
        results.append(check("coloanele claimed_at + completed_at există", cols, 2))

        idx = await conn.fetchval(
            "select count(*) from pg_indexes where indexname = 'idx_inbound_dedupe_orphan'"
        )
        results.append(check("indexul de orfani există", idx, 1))

        has_update = await conn.fetchval(
            "select has_table_privilege('bot_runtime', 'inbound_dedupe', 'UPDATE')"
        )
        results.append(check("bot_runtime are UPDATE pe inbound_dedupe", has_update, True))
    finally:
        await conn.close()

    print("\n" + ("TOATE TESTELE TREC ✓" if all(results) else "EXISTĂ TESTE PICATE ✗"))
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    asyncio.run(main())
