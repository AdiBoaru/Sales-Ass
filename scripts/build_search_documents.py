"""Rulează explicit writerul shadow NX-207; nu este scheduler și nu schimbă read-path-ul.

python scripts/build_search_documents.py BUSINESS_ID --dry-run
python scripts/build_search_documents.py BUSINESS_ID --locale ro
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from src.db.connection import admin_conn, close_pool, get_pool
from src.jobs.build_search_documents import build_for_business, plan_for_business


async def run(business_id: str, *, locale: str, dry_run: bool) -> int:
    pool = await get_pool()
    try:
        async with admin_conn(pool) as conn:
            if dry_run:
                artifacts = await plan_for_business(conn, business_id, locale=locale)
                print(f"DRY-RUN: {len(artifacts)} artefacte shadow generate; zero scrieri.")
                return 0
            count = await build_for_business(conn, business_id, locale=locale)
            print(f"SHADOW: {count} artefacte persistate; retrieval-ul vechi rămâne activ.")
            return 0
    finally:
        await close_pool()


def main() -> int:
    if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("business_id")
    parser.add_argument("--locale", default="ro")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    return asyncio.run(run(args.business_id, locale=args.locale, dry_run=args.dry_run))


if __name__ == "__main__":
    raise SystemExit(main())
