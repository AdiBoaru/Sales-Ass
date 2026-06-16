"""Job nocturn — rollup `usage_daily` (F2-3).

Agregă o zi în `usage_daily` (sursa unică pt dashboard + facturare) pentru fiecare business
activ. Cross-tenant + citește `analytics_events` (bot_runtime n-are SELECT) → conn ADMIN, ca
`cleanup_dedupe`. Un business care crapă e logat și sărit (nu oprește restul).

Standalone:
    python -m src.jobs.rollup_usage            # ziua de ieri (UTC)
    python -m src.jobs.rollup_usage 2026-06-15 # o zi anume
"""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import UTC, date, datetime, timedelta

from src.db.connection import admin_conn, close_pool, get_pool
from src.db.queries.usage import list_active_business_ids, rollup_usage_day

log = logging.getLogger(__name__)


def yesterday_utc() -> date:
    """Ziua țintă implicită: ieri (UTC) — rollup-ul rulează după miezul nopții pe ziua încheiată."""
    return (datetime.now(UTC) - timedelta(days=1)).date()


def parse_day(argv: list[str]) -> date:
    """Ziua din argv (`YYYY-MM-DD`) sau ieri (UTC) dacă lipsește. ValueError la format greșit."""
    if not argv:
        return yesterday_utc()
    return date.fromisoformat(argv[0])


async def run_rollup(conn, *, day: date) -> dict[str, int]:
    """Rulează rollup-ul pe `day` pentru toate businessurile active. Întoarce {processed, failed}.
    Un business care aruncă e logat și sărit — restul continuă (un tenant nu blochează raportul)."""
    business_ids = await list_active_business_ids(conn)
    processed = 0
    failed = 0
    for business_id in business_ids:
        try:
            await rollup_usage_day(conn, business_id, day)
            processed += 1
        except Exception:  # noqa: BLE001 — un business nu trebuie să oprească restul rollup-ului
            log.exception("rollup usage_daily eșuat (business=%s day=%s)", business_id, day)
            failed += 1
    log.info("rollup usage_daily %s: processed=%d failed=%d", day, processed, failed)
    return {"processed": processed, "failed": failed}


async def _main() -> None:
    logging.basicConfig(level=logging.INFO)
    day = parse_day(sys.argv[1:])
    pool = await get_pool()
    try:
        async with admin_conn(pool) as conn:
            await run_rollup(conn, day=day)
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(_main())
