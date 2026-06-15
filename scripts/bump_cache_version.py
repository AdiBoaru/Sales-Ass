"""Invalidare manuală a cache-ului dynamic pentru un business (G5b-2).

Pentru intervenții de preț în masă când jobul de sync de catalog nu e încă wired:
incrementează `businesses.data_version` → toate entry-urile cache `dynamic` vechi devin
instant inaccesibile la următorul lookup (cele `static` rămân). Opțional, purjă completă.

Rulează:
  python scripts/bump_cache_version.py <business_id>
  python scripts/bump_cache_version.py <business_id> --purge   # + șterge tot cache-ul
"""

import argparse
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


async def main(business_id: str, purge: bool) -> None:
    if not DSN:
        print("EROARE: SUPABASE_DB_URL lipsește din .env")
        sys.exit(2)

    # Import aici (după ce .env e încărcat) — query-uri tenant-scoped.
    from src.db.queries.businesses import bump_data_version
    from src.db.queries.semantic_cache import purge_business

    conn = await connect()
    try:
        new_version = await bump_data_version(conn, business_id)
        print(f"data_version → {new_version} (cache dynamic vechi invalidat) pt {business_id}")
        if purge:
            count = await purge_business(conn, business_id)
            print(f"purjă completă: {count} entry-uri șterse")
    finally:
        await conn.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Invalidare cache dynamic per business (G5b-2)")
    ap.add_argument("business_id", help="UUID-ul businessului")
    ap.add_argument("--purge", action="store_true", help="șterge TOT cache-ul (nu doar bump)")
    args = ap.parse_args()
    asyncio.run(main(args.business_id, args.purge))
