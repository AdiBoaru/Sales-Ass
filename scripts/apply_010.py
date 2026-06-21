# DEPRECAT (NX-123): foloseste `python scripts/migrate.py` (runner ordonat + tracking).
# Pastrat doar pentru istoric — NU mai rula manual apply_0NN.py.
"""Aplică docs/010_conversations_one_open.sql (NX-87): index unic parțial pentru
„o singură conversație deschisă per (business, contact, canal)".

Detectează ÎNTÂI duplicatele preexistente (CREATE INDEX ar eșua pe ele) și raportează —
NU le merge-uiește automat (decizie de date). Apoi aplică indexul + verifică existența.

Rulează: python scripts/apply_010.py
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
SQL_FILE = Path(__file__).parent.parent / "docs" / "010_conversations_one_open.sql"


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


async def main():
    conn = await connect()
    try:
        print("1. Verific duplicate de conversații DESCHISE (indexul ar eșua pe ele) ...")
        dups = await conn.fetch(
            """
            select business_id::text, contact_id::text, channel_id::text, count(*) as n
            from conversations
            where status = 'open'
            group by 1, 2, 3
            having count(*) > 1
            order by n desc
            """
        )
        if dups:
            print(f"   [ABORT] {len(dups)} chei cu >1 conversație deschisă — rezolvă manual întâi:")
            for r in dups[:10]:
                print(f"     business={r['business_id']} contact={r['contact_id']} n={r['n']}")
            print("   (închide duplicatele: păstrează cea mai recentă 'open', restul → 'closed'.)")
            sys.exit(1)
        print("   zero duplicate — se poate aplica.")

        print("2. Aplic 010_conversations_one_open.sql ...")
        await conn.execute(SQL_FILE.read_text(encoding="utf-8"))
        exists = await conn.fetchval(
            "select count(*) from pg_indexes where indexname = 'uq_conversations_one_open'"
        )
        ok = exists == 1
        print(f"   [{'OK ' if ok else 'FAIL'}] index uq_conversations_one_open prezent: {exists}")
        sys.exit(0 if ok else 1)
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
