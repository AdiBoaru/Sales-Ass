"""NX-191 — setează config-ul COMERCIAL pe `businesses.settings` (admin, NU worker).

Livrarea, pragul de transport gratuit, TVA-ul, returul și metodele de plată n-au existat nicăieri:
la „în cât timp ajunge?" botul n-avea ce citi, iar pragul de transport gratuit trăia doar ca text
de FAQ (deci inutilizabil pentru upsell-ul „mai adaugă X lei"). Scriptul scrie cheile ca jsonb
structurat, scoped pe business_id, idempotent (jsonb_set suprascrie, nu adaugă).

Nu atinge cheile existente (store_base_url, checkout_url, domain_pack, content_status_filter).

    python scripts/set_commerce_config.py <business_id>            # valorile demo de mai jos
    python scripts/set_commerce_config.py <business_id> --show     # doar afișează, nu scrie
"""

import asyncio
import json
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

# Politica demo (decizii luate explicit cu userul):
#  • prețurile afișate includ TVA;
#  • oră-limită 14:00, luni-vineri, FĂRĂ calendar de sărbători (l-am întreține degeaba pe demo);
#  • transport 19,99 lei, gratuit de la 199 lei → deblochează „mai adaugă X lei și ai transportul
#    gratuit" (upsell pe fapte, nu pe insistență);
#  • retur 14 zile de la LIVRARE, la nivel de comandă; plată card sau ramburs.
COMMERCE = {
    "prices_include_vat": True,
    "shipping": {
        "cutoff_hour": 14,
        "working_days": [1, 2, 3, 4, 5],
        "cost": 19.99,
        "free_threshold": 199.0,
        "courier": "Cargus",
        # clasă → (min, max) zile LUCRĂTOARE. `next_day` nu apare: depinde de ora-limită.
        "class_days": {"standard": [2, 4], "supplier": [5, 7], "preorder": [10, 14]},
    },
    "returns": {"days": 14, "from": "delivery"},
    "payment": {"methods": ["card", "ramburs"]},
}

_SQL = """
update businesses
   set settings = coalesce(settings, '{}'::jsonb) || $2::jsonb
 where id = $1
returning settings
"""


async def connect(dsn: str) -> asyncpg.Connection:
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


async def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    business_id = sys.argv[1]
    show_only = "--show" in sys.argv
    if not DSN:
        print("EROARE: SUPABASE_DB_URL / DATABASE_URL lipsesc din .env")
        return 1

    conn = await connect(DSN)
    try:
        if show_only:
            cur = await conn.fetchval("select settings from businesses where id=$1", business_id)
            cur = json.loads(cur) if isinstance(cur, str) else (cur or {})
            print(json.dumps(cur, ensure_ascii=False, indent=2))
            return 0

        row = await conn.fetchval(_SQL, business_id, json.dumps(COMMERCE, ensure_ascii=False))
        if row is None:
            print(f"EROARE: business {business_id} nu există")
            return 1
        merged = json.loads(row) if isinstance(row, str) else row
        print("✓ config comercial scris. Chei acum:", ", ".join(sorted(merged)))
        print(
            json.dumps(
                {k: merged[k] for k in COMMERCE if k in merged}, ensure_ascii=False, indent=2
            )
        )
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
