"""Job de retenție pentru `web_turns` (NX-232).

Șterge (1) turele TERMINALE mai vechi decât fereastra de replay (default 7 zile —
`WEB_TURNS_RETENTION_HOURS`) și (2) turele NE-terminale abandonate (accept fără
follow-through / crash nerecuperat, default 7 zile — `WEB_TURNS_STALE_DAYS`).

BOUNDED: `cleanup_web_turns` șterge în batch-uri; jobul repetă până la 0, ca o rulare
zilnică pe un backlog mare să nu țină un DELETE gigant. Mentenanță cross-tenant →
conexiune admin (ca `cleanup_dedupe`; bot_runtime/RLS ar purja doar tenantul curent și
oricum nu are DELETE pe tabel).

Rulează standalone: python -m src.jobs.cleanup_web_turns
"""

import asyncio
import logging

from src.config import get_settings
from src.db.connection import admin_conn, close_pool, get_pool
from src.db.queries.web_turns import cleanup_web_turns

log = logging.getLogger(__name__)


async def run(
    terminal_older_than_hours: int | None = None,
    stale_older_than_days: int | None = None,
) -> int:
    """Purjează ledgerul în batch-uri. Întoarce câte rânduri a șters în total."""
    s = get_settings()
    hours = terminal_older_than_hours or s.web_turns_retention_hours
    days = stale_older_than_days or s.web_turns_stale_days
    pool = await get_pool()
    total = 0
    while True:
        async with admin_conn(pool) as conn:
            deleted = await cleanup_web_turns(
                conn, terminal_older_than_hours=hours, stale_older_than_days=days
            )
        total += deleted
        if deleted == 0:
            break
    log.info("cleanup web_turns: %d rânduri șterse (terminal >%dh, stale >%dz)", total, hours, days)
    return total


async def _main() -> None:
    logging.basicConfig(level=logging.INFO)
    try:
        await run()
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(_main())
