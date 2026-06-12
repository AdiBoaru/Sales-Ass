"""Verificare read-only a conexiunii Supabase + starea schemei.

Rulează: python scripts/db_check.py
Citește SUPABASE_DB_URL (sau DATABASE_URL) din .env. NU scrie nimic în DB.
Nu afișează niciodată parola.
"""

import asyncio
import os
import socket
import ssl
import sys
from urllib.parse import unquote, urlparse

import asyncpg
from dotenv import load_dotenv

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    sys.stdout.reconfigure(encoding="utf-8")

load_dotenv()

DSN = os.environ.get("SUPABASE_DB_URL") or os.environ.get("DATABASE_URL")


async def connect(dsn: str) -> asyncpg.Connection:
    """Conectare robustă pe Windows: rezolvă host-ul în IPv4 sincron și conectează
    pe IP (resolverul async al asyncpg e flaky pe Windows). SSL fără verificare de
    hostname (conectăm pe IP). Pe Linux/VPS s-ar folosi direct asyncpg.connect(dsn).
    """
    p = urlparse(dsn)
    ipv4 = socket.getaddrinfo(p.hostname, p.port or 5432, socket.AF_INET, socket.SOCK_STREAM)[0][4][
        0
    ]
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return await asyncpg.connect(
        host=ipv4,
        port=p.port or 5432,
        user=unquote(p.username),
        password=unquote(p.password),
        database=(p.path or "/postgres").lstrip("/"),
        ssl=ctx,
    )


KEY_TABLES = [
    "businesses",
    "contacts",
    "channel_identities",
    "conversations",
    "messages",
    "products",
    "product_embeddings",
    "faqs",
    "semantic_cache",
    "intent_aliases",
    "orders",
    "outbox",
    "usage_daily",
]


async def main():
    if not DSN:
        print("EROARE: nici SUPABASE_DB_URL nici DATABASE_URL nu sunt setate în .env")
        return

    conn = await connect(DSN)
    try:
        ver = await conn.fetchval("select version()")
        print(f"Conectat: {ver.split(',')[0]}")

        # extensii
        exts = await conn.fetch(
            "select extname from pg_extension "
            "where extname in ('vector','pg_trgm','pgcrypto') order by extname"
        )
        print(f"Extensii: {', '.join(e['extname'] for e in exts) or 'NICIUNA'}")

        # câte tabele în public
        ntables = await conn.fetchval("select count(*) from pg_tables where schemaname='public'")
        print(f"Tabele în public: {ntables}")

        # roluri relevante
        roles = await conn.fetch(
            "select rolname from pg_roles "
            "where rolname in ('bot_runtime','service_role','authenticated') order by rolname"
        )
        print(f"Roluri: {', '.join(r['rolname'] for r in roles) or 'niciunul relevant'}")

        # prezența + count pe tabelele cheie
        print("\nTabele cheie (există / nr. rânduri):")
        for t in KEY_TABLES:
            exists = await conn.fetchval("select to_regclass($1)", f"public.{t}")
            if exists is None:
                print(f"  {t:28} LIPSEȘTE")
            else:
                cnt = await conn.fetchval(f"select count(*) from {t}")
                print(f"  {t:28} {cnt}")

        # business demo
        biz = await conn.fetch("select id, slug, name from businesses order by created_at limit 5")
        print("\nBusinesses:")
        for b in biz:
            print(f"  {b['id']}  {b['slug']}  ({b['name']})")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
