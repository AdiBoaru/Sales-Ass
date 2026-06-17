"""Job cleanup (NX-84) — drop partiții vechi + expire semantic_cache.

Două bucle de mentenanță care lipseau: (1) partițiile lunare vechi din `messages`/
`analytics_events` nu se ștergeau NICIODATĂ (tabele la infinit); (2) entry-urile expirate
din `semantic_cache` erau filtrate la citire dar niciodată purjate (index HNSW umflat).

Rulează pe `admin_conn` (DDL + mentenanță cross-tenant). Standalone (`python -m src.jobs.cleanup`)
SAU chemat de scheduler-ul NX-83. Schema îl prevede explicit (secțiunea „RETENȚIE (pg_cron)").
Cod pur determinist — zero LLM.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime

from src.config import get_settings
from src.db.connection import admin_conn, get_pool
from src.db.queries.maintenance import (
    drop_partition,
    expire_semantic_cache,
    list_time_partitions,
)

log = logging.getLogger(__name__)


def _cutoff_month(today: date, retention_months: int) -> date:
    """Prima zi a lunii de la care în jos (strict) partițiile se șterg. Calcul pur,
    fără dependență de dateutil."""
    total = (today.year * 12 + (today.month - 1)) - retention_months
    year, month0 = divmod(total, 12)
    return date(year, month0 + 1, 1)


async def drop_old_partitions(
    conn, *, retention_months: int, today: date | None = None
) -> list[str]:
    """Șterge partițiile lunare mai vechi decât fereastra de retenție. Întoarce numele șterse.
    O partiție care crapă la drop e logată și sărită (restul continuă; idempotent: DROP IF EXISTS).
    NU atinge `*_default` (filtrat de `_PART_RE` în `list_time_partitions`)."""
    today = today or datetime.now(UTC).date()
    cutoff = _cutoff_month(today, retention_months)
    dropped: list[str] = []
    for name, part_month in await list_time_partitions(conn):
        if part_month < cutoff:  # strict: luna == cutoff NU se șterge
            try:
                await drop_partition(conn, name)
                dropped.append(name)
            except Exception:  # noqa: BLE001 — o partiție nu trebuie să blocheze restul
                log.exception("drop partiție eșuat: %s", name)
    log.info(
        "cleanup partiții: %d șterse (retenție %d luni, cutoff %s)",
        len(dropped),
        retention_months,
        cutoff,
    )
    return dropped


async def _run_on_conn(conn, *, retention_months: int) -> dict[str, int]:
    dropped = await drop_old_partitions(conn, retention_months=retention_months)
    expired = await expire_semantic_cache(conn)
    log.info("cleanup: %d partiții, %d entry-uri cache expirate", len(dropped), expired)
    return {"partitions_dropped": len(dropped), "cache_expired": expired}


async def run() -> dict[str, int]:
    """Entrypoint (standalone + chemat de scheduler-ul NX-83). Pe `admin_conn`."""
    s = get_settings()
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        return await _run_on_conn(conn, retention_months=s.retention_months)


async def _main() -> None:
    from src.db.connection import close_pool

    logging.basicConfig(level=logging.INFO)
    try:
        result = await run()
        log.info("cleanup done: %s", result)
    finally:
        await close_pool()


if __name__ == "__main__":
    import asyncio

    asyncio.run(_main())
