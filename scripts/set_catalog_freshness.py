"""Declară cum se judecă în timp catalogul unui tenant + repară `products.synced_at`.

Două operații care merg împreună, fiindcă răspund la aceeași întrebare pusă din două părți:

  1. **Declarația** (`businesses.settings.catalog_freshness`) — acest catalog e alimentat de un
     sync care îl reconfruntă cu sursa (`synced`, cu prag), sau e o fotografie importată o dată
     (`static_snapshot`, fără prag)? Vezi `src/catalog/freshness.py` pentru consecințe.

  2. **Reparația lui `synced_at`** (`--backfill`) — coloana pe care se sprijină prospețimea a fost
     scrisă cu `now()` la import, adică momentul IMPORTULUI, nu al citirii sursei. Adevărul e în
     `source_products_raw.scraped_at`, per produs, pus acolo de același import. Backfill-ul îl
     mută unde trebuie. Măsurat pe `sole-ro`: sursa citită 27.08 14:05→18:23 (întins pe scrape),
     `synced_at` scris uniform 28.08 10:06 — cu 20 de ore mai optimist decât realitatea.

De ce declarația nu e opțională când catalogul e static: pragul global (`COMMERCE_FACTS_SLA_S`)
e o variabilă de MEDIU, deci una pentru toți clienții. Ridicat ca să nu se stingă un snapshot, ar
scuti tăcut de verificare și primul client cu feed real. Declarația e per tenant și se vede.

DRY-RUN implicit: fără `--apply` nu scrie nimic, doar arată ce s-ar schimba.

    python scripts/set_catalog_freshness.py --business sole-ro --mode static_snapshot --backfill
    python scripts/set_catalog_freshness.py --business sole-ro --mode static_snapshot --apply
    python scripts/set_catalog_freshness.py --business <slug> --mode synced --sla-s 86400 --apply

(`--backfill` se combină cu `--apply`; fără `--apply` orice combinație rămâne dry-run.)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import ssl
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

import asyncpg
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    sys.stdout.reconfigure(encoding="utf-8")

load_dotenv()

from src.catalog.freshness import MODE_STATIC, MODE_SYNCED, SETTINGS_KEY  # noqa: E402

DSN = os.environ.get("SUPABASE_DB_URL") or os.environ.get("DATABASE_URL")

_SET_SQL = f"""
update businesses
   set settings = jsonb_set(coalesce(settings, '{{}}'::jsonb),
                            '{{{SETTINGS_KEY}}}', $2::jsonb, true)
 where id = $1
"""

# `synced_at` ← `scraped_at`, prin `source_url` (cheia pe care ancora lossless o are cu products).
# Doar rândurile care CHIAR diferă: un backfill care „atinge" tot ar mișca `updated_at` degeaba.
_BACKFILL_SQL = """
update products p
   set synced_at = r.scraped_at
  from source_products_raw r
 where p.business_id = $1
   and r.business_id = p.business_id
   and r.source_url = p.product_url
   and r.scraped_at is not null
   and (p.synced_at is distinct from r.scraped_at)
"""

_PREVIEW_SQL = """
select count(*) filter (where p.synced_at is distinct from r.scraped_at) as de_reparat,
       count(*)                                                         as cu_ancora,
       min(r.scraped_at) as sursa_min, max(r.scraped_at) as sursa_max,
       min(p.synced_at)  as acum_min,  max(p.synced_at)  as acum_max
  from products p
  join source_products_raw r
    on r.business_id = p.business_id and r.source_url = p.product_url
 where p.business_id = $1 and r.scraped_at is not null
"""


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
        statement_cache_size=0,
    )


async def _resolve_business(conn: asyncpg.Connection, ref: str) -> tuple[str, str] | None:
    row = await conn.fetchrow(
        "select id::text, slug from businesses where slug = $1 or id::text = $1", ref
    )
    return (row["id"], row["slug"]) if row else None


def _declaration(mode: str, sla_s: int | None) -> dict[str, object]:
    return {"mode": mode} if mode == MODE_STATIC else {"mode": mode, "sla_s": sla_s}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--business", required=True, help="slug sau business_id")
    ap.add_argument("--mode", choices=[MODE_STATIC, MODE_SYNCED], required=True)
    ap.add_argument("--sla-s", type=int, default=86400, help="doar pentru --mode synced")
    ap.add_argument("--backfill", action="store_true", help="repară products.synced_at din ancoră")
    ap.add_argument("--apply", action="store_true", help="chiar scrie (fără el: doar arată)")
    args = ap.parse_args()

    if not DSN:
        sys.stderr.write("EROARE: SUPABASE_DB_URL lipsește din .env\n")
        sys.exit(2)
    if args.mode == MODE_SYNCED and args.sla_s <= 0:
        sys.stderr.write("EROARE: --sla-s trebuie să fie > 0 pentru --mode synced\n")
        sys.exit(2)

    conn = await connect()
    try:
        found = await _resolve_business(conn, args.business)
        if found is None:
            sys.stderr.write(f"EROARE: tenant inexistent: {args.business!r}\n")
            sys.exit(1)
        business_id, slug = found
        declaration = _declaration(args.mode, args.sla_s)

        current = await conn.fetchval(
            "select settings -> $2 from businesses where id = $1", business_id, SETTINGS_KEY
        )
        print(f"tenant     : {slug} ({business_id})")
        print(f"declarație : {current or '(absentă)'}  →  {json.dumps(declaration)}")

        if args.backfill:
            row = await conn.fetchrow(_PREVIEW_SQL, business_id)
            print(
                f"synced_at  : {row['de_reparat']} de reparat din {row['cu_ancora']} cu ancoră\n"
                f"             acum   {row['acum_min']} .. {row['acum_max']}\n"
                f"             sursa  {row['sursa_min']} .. {row['sursa_max']}"
            )

        if not args.apply:
            print("\nDRY-RUN: nimic nu s-a scris. Adaugă --apply.")
            return

        async with conn.transaction():
            result = await conn.execute(_SET_SQL, business_id, json.dumps(declaration))
            if int(result.split()[-1]) == 0:
                raise RuntimeError(f"0 rânduri actualizate pentru {business_id}")
            if args.backfill:
                back = await conn.execute(_BACKFILL_SQL, business_id)
                print(f"synced_at  : {int(back.split()[-1])} rânduri reparate")
        print("scris.")
    finally:
        await conn.close()


asyncio.run(main())
