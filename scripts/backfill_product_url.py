"""NX-98 — backfill `products.product_url` pentru un tenant (admin, NU worker).

Validatorul de link (P8) acceptă DOAR linkuri din catalog (`products.product_url`).
Dacă URL-ul e NULL, agentul nu poate emite niciun link → bucla de bani e ruptă din
capăt. Sursa reală de URL = feed-ul magazinului (sync de catalog); pe demo nu există,
deci îl DERIVĂM determinist din `slug` + base-url-ul tenantului.

Base-url vine din `businesses.settings->>'store_base_url'` (cheie jsonb, fără migrare).
Dacă lipsește → scriptul NU inventează (lasă NULL, raportează N rămase). Idempotent:
`where product_url is null` → a doua rulare actualizează 0 rânduri. Catalogul e
read-only pentru bot, deci rulăm ca ADMIN (SUPABASE_DB_URL), scoped pe business_id.

Rulează:
    python scripts/backfill_product_url.py <business_id>
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


async def connect() -> asyncpg.Connection:
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


async def backfill(conn: asyncpg.Connection, business_id: str) -> tuple[int, int]:
    """Derivă product_url din slug + store_base_url. Întoarce (actualizate, rămase_null)."""
    base = await conn.fetchval(
        "select settings->>'store_base_url' from businesses where id = $1",
        business_id,
    )
    if not base:
        # Fără base-url NU inventăm URL-uri (ar produce linkuri moarte). Raportăm și ieșim.
        remaining = await conn.fetchval(
            "select count(*) from products where business_id = $1 and product_url is null",
            business_id,
        )
        return 0, int(remaining or 0)

    base = base.rstrip("/")
    # Scoped pe business_id (P7), doar pe NULL (idempotent), doar cu slug (altfel n-avem cale).
    status = await conn.execute(
        "update products set product_url = $2 || '/p/' || slug "
        "where business_id = $1 and product_url is null and slug is not null",
        business_id,
        base,
    )
    updated = int(status.split()[-1])  # "UPDATE <n>"
    remaining = await conn.fetchval(
        "select count(*) from products where business_id = $1 and product_url is null",
        business_id,
    )
    return updated, int(remaining or 0)


async def main() -> None:
    if not DSN:
        print("EROARE: SUPABASE_DB_URL lipsește din .env")
        sys.exit(2)
    if len(sys.argv) != 2:
        print("Utilizare: python scripts/backfill_product_url.py <business_id>")
        sys.exit(2)
    business_id = sys.argv[1]

    conn = await connect()
    try:
        updated, remaining = await backfill(conn, business_id)
    finally:
        await conn.close()

    if updated == 0 and remaining > 0:
        print(
            f"⚠️  store_base_url absent SAU nimic de actualizat. {remaining} produse rămân "
            f"FĂRĂ product_url (agentul nu va pune link). Setează "
            f"businesses.settings->>'store_base_url' și rulează din nou."
        )
    else:
        print(f"OK ✓ — {updated} produse actualizate; {remaining} rămase NULL (slug absent).")
    sys.exit(0)


if __name__ == "__main__":
    asyncio.run(main())
