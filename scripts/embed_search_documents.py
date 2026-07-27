"""Rulează explicit backfill-ul shadow NX-207, fără a modifica retrieval-ul live.

python scripts/embed_search_documents.py BUSINESS_ID --limit 10
python scripts/embed_search_documents.py BUSINESS_ID --force
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from src.agent.llm import get_llm
from src.db.connection import admin_conn, close_pool, get_pool
from src.jobs.embed_products import embed_shadow_pending


async def run(business_id: str, *, force: bool, limit: int) -> int:
    llm = get_llm()
    if llm is None:
        print("OPENAI_API_KEY lipsește — nu pot genera embeddings shadow.", file=sys.stderr)
        return 2

    pool = await get_pool()
    try:
        async with admin_conn(pool) as conn:
            count = await embed_shadow_pending(
                conn,
                llm,
                business_id,
                force=force,
                limit=limit,
            )
    finally:
        await close_pool()

    print(
        f"SHADOW: {count} embeddings search_document_v1 persistate; "
        "retrieval-ul vechi rămâne activ."
    )
    return 0


def main() -> int:
    if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("business_id")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()
    return asyncio.run(run(args.business_id, force=args.force, limit=args.limit))


if __name__ == "__main__":
    raise SystemExit(main())
