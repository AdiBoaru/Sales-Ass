"""Driver rezilient pentru `src/jobs/build_search_documents.py` pe cataloage mari.

Jobul în sine e corect; ce nu suportă e scara. Pe 2.758 de produse, cu o tranzacție și mai multe
drumuri dus-întors per produs către o bază aflată în alt centru de date, o rulare ține o singură
conexiune zeci de minute — iar poolerul Supabase o închide sub picioare:
`InterfaceError: the underlying connection is closed`, la jumătate.

Driverul NU reimplementează jobul: îi folosește exact funcțiile (`load_active_products`,
`build_search_artifacts`, `upsert_artifacts`). Schimbă doar cum sunt conduse:

  • **sare produsele deja curente FĂRĂ niciun drum la bază.** Artefactele se construiesc pur, în
    memorie, iar `content_hash` se compară cu ce e deja scris, citit o singură dată în bloc. O
    reluare după o cădere costă aproape nimic, în loc să reparcurgă tot;
  • **conexiune proaspătă la fiecare felie**, deci nicio conexiune nu trăiește destul cât s-o
    închidă poolerul;
  • **reia de unde a rămas** la eroare de conexiune, fără să piardă munca feliei precedente.

Rulare:
    TARGET_DB_URL=postgresql://... python scripts/build_sole_search_docs.py --slug sole-ro
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

import asyncpg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.domain.search_documents import build_search_artifacts  # noqa: E402
from src.jobs.build_search_documents import (  # noqa: E402
    load_active_products,
    upsert_artifacts,
)

CHUNK = 100  # produse per conexiune
MAX_RETRIES = 5
OK, WARN, BAD = "  ok  ", " ATENTIE ", " ESUAT "


async def existing_hashes(conn: asyncpg.Connection, biz: str, locale: str) -> dict[str, str]:
    rows = await conn.fetch(
        "select product_id::text, content_hash from product_search_documents "
        "where business_id = $1 and locale = $2",
        biz,
        locale,
    )
    return {r["product_id"]: r["content_hash"] for r in rows}


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slug", default="sole-ro")
    ap.add_argument("--locale", default="ro")
    args = ap.parse_args()

    dsn = os.environ.get("TARGET_DB_URL")
    if not dsn:
        sys.stderr.write("TARGET_DB_URL lipseste\n")
        return 2

    conn = await asyncpg.connect(dsn, statement_cache_size=0)
    biz = str(await conn.fetchval("select id from businesses where slug = $1", args.slug))
    products = await load_active_products(conn, biz)
    done = await existing_hashes(conn, biz, args.locale)
    await conn.close()
    print(f"[{OK}] {len(products)} produse active, {len(done)} deja au document")

    # Construcția e PURĂ: se face o dată, în memorie, fără nicio conexiune deschisă.
    pending = []
    for product in products:
        art = build_search_artifacts(product, business_id=biz, locale=args.locale)
        if done.get(art.product_id) != art.content_hash:
            pending.append((art, product.get("source_version")))
    print(
        f"[{OK}] de scris: {len(pending)} (sarite ca fiind curente: {len(products) - len(pending)})"
    )
    if not pending:
        return 0

    written = failed = 0
    index = 0
    retries = 0
    while index < len(pending):
        slice_ = pending[index : index + CHUNK]
        try:
            conn = await asyncpg.connect(dsn, statement_cache_size=0, timeout=30)
        except Exception as exc:
            retries += 1
            if retries > MAX_RETRIES:
                print(f"[{BAD}] nu ma pot reconecta: {exc}")
                return 1
            await asyncio.sleep(2 * retries)
            continue
        try:
            for art, source_version in slice_:
                if await upsert_artifacts(conn, art, source_version=source_version):
                    written += 1
                else:
                    failed += 1
            index += len(slice_)
            retries = 0
            pct = 100 * index / len(pending)
            print(f"[{OK}] {index}/{len(pending)} ({pct:.0f}%) scrise={written}", flush=True)
        except (asyncpg.PostgresConnectionError, asyncpg.exceptions._base.InterfaceError) as exc:
            # Felia curentă se reia integral: `upsert_artifacts` e idempotent pe `content_hash`,
            # deci re-scrierea unui produs deja făcut e un no-op, nu un duplicat.
            retries += 1
            print(f"[{WARN}] conexiune pierduta ({type(exc).__name__}), reiau felia")
            if retries > MAX_RETRIES:
                print(f"[{BAD}] prea multe caderi consecutive")
                return 1
            await asyncio.sleep(2 * retries)
        finally:
            if not conn.is_closed():
                await conn.close()

    print(f"\n[{OK}] gata: {written} scrise, {failed} sarite (produsul s-a schimbat sub snapshot)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
