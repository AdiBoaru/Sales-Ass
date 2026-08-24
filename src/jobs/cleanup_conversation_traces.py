"""Job de retenție pentru `conversation_traces` (NX-256).

Șterge capturile de diagnoză mai vechi decât fereastra de retenție (default 30 de zile —
`CONVERSATION_TRACE_RETENTION_DAYS`). Captura e unealtă de diagnoză/test, nu arhivă:
conținutul de conversație nu rămâne pe disc la nesfârșit.

BOUNDED: `cleanup_conversation_traces` șterge în batch-uri; jobul repetă până la 0, ca o
rulare zilnică pe un backlog mare să nu țină un DELETE gigant. Mentenanță cross-tenant →
conexiune admin (modelul `cleanup_web_turns`; bot_runtime/RLS ar purja doar tenantul curent
și oricum nu are DELETE pe tabel). NU e gated pe `conversation_trace_enabled`: dacă flagul
se stinge după ce s-au acumulat rânduri, ele tot trebuie purjate; pe o DB fără migrarea
045 e no-op tăcut (guard `to_regclass` în query).

Rulează standalone: python -m src.jobs.cleanup_conversation_traces
"""

import asyncio
import logging

from src.config import get_settings
from src.db.connection import admin_conn, close_pool, get_pool
from src.db.queries.traces import cleanup_conversation_traces

log = logging.getLogger(__name__)


async def run(older_than_days: int | None = None) -> int:
    """Purjează captura în batch-uri. Întoarce câte rânduri a șters în total."""
    days = older_than_days or get_settings().conversation_trace_retention_days
    pool = await get_pool()
    total = 0
    while True:
        async with admin_conn(pool) as conn:
            deleted = await cleanup_conversation_traces(conn, older_than_days=days)
        total += deleted
        if deleted == 0:
            break
    log.info("cleanup conversation_traces: %d rânduri șterse (>%dz)", total, days)
    return total


async def _main() -> None:
    logging.basicConfig(level=logging.INFO)
    try:
        await run()
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(_main())
