"""Job de cleanup pentru `inbound_dedupe` (NX-51).

Șterge markerele mai vechi decât fereastra de retry Meta (48h default). Rulat de
scheduler zilnic. Mentenanță cross-tenant → conexiune admin (markerele sunt
non-PII; bot_runtime/RLS ar purja doar tenantul curent).

Rulează standalone: python -m src.jobs.cleanup_dedupe
"""

import asyncio
import logging

from src.db.connection import admin_conn, close_pool, get_pool
from src.db.queries.inbound_dedupe import cleanup_inbound_dedupe

log = logging.getLogger(__name__)


async def run(older_than_hours: int = 48) -> int:
    """Purjează markerele vechi. Întoarce câte rânduri a șters."""
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        deleted = await cleanup_inbound_dedupe(conn, older_than_hours=older_than_hours)
    log.info("cleanup inbound_dedupe: %d markere șterse (>%dh)", deleted, older_than_hours)
    return deleted


async def _main() -> None:
    logging.basicConfig(level=logging.INFO)
    try:
        await run()
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(_main())
