"""Tenantul-fixtură al testelor de integrare.

De ce există: ~80 de fișiere din `tests/` au UUID-ul demo scris în clar
(`6098812a-50fc-44bd-a1ba-bc77e6399158`) și îl folosesc ca ancoră pentru rândurile pe care le
creează singure (canal throwaway, conversație, evenimente) și le curăță după. Pe proiectul
Supabase v3 tenantul acela nu mai există, deci testele cad cu `channels_business_id_fkey` — nu
pentru că izolarea ar fi ruptă, ci pentru că lipsește CUIUL de care își agață datele.

Alternativa ar fi fost să parametrizez UUID-ul în toate cele ~80 de fișiere. Nu merită: valoarea
lor e ce verifică (RLS, cache, analytics), nu de unde își iau tenantul. Rândul de aici e explicit
un artefact de TEST — `status='paused'` (vocabularul schemei n-are `inactive`), ca să nu fie servit
din greșeală de vreo cale de producție, și numit ca atare.

NU seedează catalog: testele care au nevoie de produse și le creează singure. Un catalog fals
lângă cel real ar fi exact balastul pe care proiectul nou l-a scăpat.

    python scripts/seed_test_tenant.py            # creează dacă lipsește (idempotent)
    python scripts/seed_test_tenant.py --check    # doar raportează, nu scrie
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
TEST_BIZ = "6098812a-50fc-44bd-a1ba-bc77e6399158"


async def _connect() -> asyncpg.Connection:
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
        sys.exit("SUPABASE_DB_URL lipsește în .env")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true", help="raportează, nu scrie")
    args = ap.parse_args()

    conn = await _connect()
    try:
        row = await conn.fetchrow("select slug, status from businesses where id = $1", TEST_BIZ)
        if row is not None:
            print(f"există deja: {TEST_BIZ} slug={row['slug']!r} status={row['status']!r}")
            return
        if args.check:
            print(f"LIPSEȘTE: {TEST_BIZ} (testele de integrare vor cădea pe cheia străină)")
            sys.exit(1)
        await conn.execute(
            """
            insert into businesses (id, name, slug, vertical, status, default_locale)
            values ($1, 'Test fixture tenant', 'test-fixture', 'ecommerce', 'paused', 'ro')
            """,
            TEST_BIZ,
        )
        print(f"creat: {TEST_BIZ} slug='test-fixture' status='paused'")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
