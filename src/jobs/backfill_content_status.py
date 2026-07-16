"""NX-171c: backfill `content_status` per tenant (JOB — deliberat NU în migrare).

SQL nu poate ști dacă un produs trece auditul de coerență (168d) → clasificarea e un job Python.
Rulează `audit(contract="v3")` O DATĂ pe catalogul COMPLET al tenantului (auditul verifică duplicate
cross-produs — SKU/GTIN/nume — deci NU se poate rula per produs) → produsele care apar în ≥1
violation = 'draft', restul = 'published'. Idempotent (UPDATE re-rulabil). Citește
`entry['product_slugs']` MACHINE-READABLE, NU parsează textul CLI.

Flagul per-tenant (`businesses.settings->>'content_status_filter'`) se activează SEPARAT
(`--activate`) DOAR după ce backfill-ul a rulat + test-plasa `visible_count > 0` trece (altfel gol).

    python -m src.jobs.backfill_content_status              # tenant demo (db/seed/catalog_v2.json)
    python -m src.jobs.backfill_content_status --activate    # + activează flagul (după test-plasă)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[2]
DEMO_BIZ = "6098812a-50fc-44bd-a1ba-bc77e6399158"
DEMO_CATALOG = ROOT / "db" / "seed" / "catalog_v2.json"


def classify_content_status(data: dict[str, Any]) -> dict[str, str]:
    """Pur (fără DB): audit v3 pe snapshot-ul COMPLET → `{slug: 'draft'|'published'}`. Un slug care
    apare în ≥1 violation (orice regulă) = 'draft'; restul = 'published'. Sursa de „validat" =
    `audit(contract='v3')` (168d), citind `entry['product_slugs']` machine-readable. Fără DB."""
    from scripts.audit_catalog_v2 import audit  # lazy: modulul rămâne ușor la import

    result = audit(data, contract="v3")
    flagged: set[str] = set()
    for entries in result["violations"].values():
        for entry in entries:
            for slug in entry.get("product_slugs") or []:
                if slug:
                    flagged.add(slug)
    out: dict[str, str] = {}
    for p in data.get("products") or []:
        slug = p.get("slug")
        if isinstance(slug, str) and slug:
            out[slug] = "draft" if slug in flagged else "published"
    return out


async def backfill(conn, business_id: str, data: dict[str, Any]) -> dict[str, int]:
    """Aplică clasificarea în DB pentru UN tenant. Produsele din snapshot primesc statusul din audit
    (published → + schema_version=3 + verified_at); produsele DIN DB dar ABSENTE din snapshot →
    'draft' (necurate/nevalidate v3, conservator). Idempotent. Întoarce contorii (inclusiv
    `visible` = câte 'published' — semnalul test-plasei)."""
    mapping = classify_content_status(data)
    db_rows = await conn.fetch(
        "select id::text as id, slug from products where business_id=$1", business_id
    )
    n_pub = n_draft = 0
    async with conn.transaction():
        for r in db_rows:
            status = mapping.get(r["slug"], "draft")  # absent din snapshot → nevalidat
            if status == "published":
                await conn.execute(
                    "update products set content_status='published', schema_version=3, "
                    "verified_at=now() where id=$1 and business_id=$2",
                    r["id"],
                    business_id,
                )
                n_pub += 1
            else:
                await conn.execute(
                    "update products set content_status='draft' where id=$1 and business_id=$2",
                    r["id"],
                    business_id,
                )
                n_draft += 1
    visible = int(
        await conn.fetchval(
            "select count(*) from products where business_id=$1 and content_status='published'",
            business_id,
        )
    )
    log.info(
        "backfill %s: %d published, %d draft (visible=%d)", business_id, n_pub, n_draft, visible
    )
    return {"published": n_pub, "draft": n_draft, "visible": visible}


async def activate_flag(conn, business_id: str) -> None:
    """Setează flagul per-tenant `content_status_filter=true` în businesses.settings. De apelat DOAR
    după backfill + test-plasă (visible > 0)."""
    await conn.execute(
        "update businesses set settings = coalesce(settings, '{}'::jsonb) "
        "|| jsonb_build_object('content_status_filter', true) where id=$1",
        business_id,
    )


async def _main() -> None:
    logging.basicConfig(level=logging.INFO)
    ap = argparse.ArgumentParser()
    ap.add_argument("--business", default=DEMO_BIZ)
    ap.add_argument("--catalog", default=str(DEMO_CATALOG))
    ap.add_argument(
        "--activate", action="store_true", help="setează flagul per-tenant post-backfill"
    )
    args = ap.parse_args()

    from src.db.connection import admin_conn, close_pool, get_pool  # noqa: PLC0415

    data = json.loads(Path(args.catalog).read_text(encoding="utf-8"))
    pool = await get_pool()
    try:
        async with admin_conn(pool) as conn:
            stats = await backfill(conn, args.business, data)
            if args.activate:
                if stats["visible"] <= 0:
                    log.error(
                        "visible=0 → NU activez flagul (ar goli catalogul); verifică backfill"
                    )
                    return
                await activate_flag(conn, args.business)
                log.info(
                    "flag content_status_filter activat pt %s (visible=%d)",
                    args.business,
                    stats["visible"],
                )
    finally:
        await close_pool()


if __name__ == "__main__":
    import sys

    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(_main())
