"""NX-99 — setează `store_base_url` + `checkout_url` pe `businesses.settings` (admin, NU worker).

Bucla de bani (`checkout_link` → comandă cu `?ref=` → atribuire → `usage_daily.revenue_attributed`)
e codată și testată, dar pe demo lipsea CONFIGUL: `settings` n-avea nici `checkout_url` (pt linkul
de coș), nici `store_base_url` (pt linkurile de produs, NX-98) → `checkout_link` întorcea
`no_checkout_url`. Acest script setează ambele chei jsonb, scoped pe `business_id`, idempotent.

`store_base_url` (ex. https://shop.sole-demo.ro) alimentează backfill_product_url.py (linkuri de
produs). `checkout_url` (ex. https://shop.sole-demo.ro/cart) alimentează `_checkout_base` (linkul de
coș cu `?ref=`). Dacă `checkout_url` lipsește din argumente → derivat determinist din
`store_base_url` + '/cart' (NU lăsat gol). Catalog/config read ca ADMIN (SUPABASE_DB_URL).

Rulează:
    python scripts/set_store_config.py <business_id> <store_base_url> [checkout_url]
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

# jsonb_set imbricat → setează ambele chei pe settings (coalesce pt settings NULL/absent).
# Scoped pe id = $1 (P7). Idempotent: jsonb_set suprascrie (nu append) → re-rulare = idem.
_SQL = """
update businesses
   set settings = jsonb_set(
         jsonb_set(coalesce(settings, '{}'::jsonb),
                   '{store_base_url}', to_jsonb($2::text), true),
         '{checkout_url}', to_jsonb($3::text), true)
 where id = $1
"""


def _derive(store_base_url: str, checkout_url: str | None) -> tuple[str, str]:
    """Normalizează: store fără `/` final; checkout = arg sau `store + '/cart'` (fără `//cart`)."""
    store = store_base_url.rstrip("/")
    checkout = (checkout_url or f"{store}/cart").rstrip("/")
    return store, checkout


async def set_store_config(
    conn: asyncpg.Connection, business_id: str, store_base_url: str, checkout_url: str | None = None
) -> int:
    """Setează cele două chei jsonb scoped pe `business_id`. Întoarce nr. de rânduri afectate
    (0 = business_id greșit). Pur asupra `businesses.settings` — niciun query pe date de client."""
    store, checkout = _derive(store_base_url, checkout_url)
    result = await conn.execute(_SQL, business_id, store, checkout)
    return int(result.split()[-1])  # "UPDATE N" → N


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


async def main() -> None:
    if not DSN:
        print("EROARE: SUPABASE_DB_URL lipsește din .env")
        sys.exit(2)
    if len(sys.argv) not in (3, 4):
        print(
            "Folosire: python scripts/set_store_config.py "
            "<business_id> <store_base_url> [checkout_url]"
        )
        sys.exit(2)
    business_id = sys.argv[1]
    store_base_url = sys.argv[2]
    checkout_url = sys.argv[3] if len(sys.argv) == 4 else None

    conn = await connect()
    try:
        store, checkout = _derive(store_base_url, checkout_url)
        n = await set_store_config(conn, business_id, store_base_url, checkout_url)
        if n == 0:
            print(f"⚠️  0 rânduri afectate — business_id greșit? ({business_id})")
            sys.exit(1)
        print(
            f"✅ settings setat pe {business_id}: store_base_url={store} · checkout_url={checkout}"
        )
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
