"""Job nocturn — rollup `demand_daily` (NX-217 felia 2).

Agregă faptele de cerere ale unei zile (evenimentele NX-163/163b) într-un tabel durabil, ca
istoria cererii să supraviețuiască retenției pe `analytics_events` și ca ferestrele de 30/90
zile să nu mai scaneze tabelul cel mai gras.

Spre deosebire de `rollup_usage` (buclă per business), aici o SINGURĂ trecere acoperă toți
tenanții — `business_id` e în GROUP BY și în cheia primară. Nu există izolare per-tenant de
raportat: e un singur statement, reușește sau eșuează în bloc (și e idempotent, deci re-rularea
repară complet).

Standalone:
    python -m src.jobs.rollup_demand              # ziua de ieri (UTC)
    python -m src.jobs.rollup_demand 2026-08-04   # o zi anume
    python -m src.jobs.rollup_demand 2026-07-11 2026-08-04   # backfill pe interval [from, to]
"""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import UTC, date, datetime, timedelta

from src.db.connection import admin_conn, close_pool, get_pool
from src.db.queries.demand_rollup import rollup_demand_day

log = logging.getLogger(__name__)


def yesterday_utc() -> date:
    """Ziua țintă implicită: ieri (UTC) — rulează după miezul nopții, pe ziua încheiată."""
    return (datetime.now(UTC) - timedelta(days=1)).date()


def parse_days(argv: list[str]) -> list[date]:
    """Zilele din argv: niciun argument → ieri; o zi → doar ea; două → intervalul INCLUSIV
    [from, to] (backfill peste evenimentele deja acumulate). ValueError la format/ordine greșită."""
    if not argv:
        return [yesterday_utc()]
    if len(argv) == 1:
        return [date.fromisoformat(argv[0])]
    start, end = date.fromisoformat(argv[0]), date.fromisoformat(argv[1])
    if end < start:
        raise ValueError("intervalul de backfill are `to` înaintea lui `from`")
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


async def run_rollup(conn, *, day: date) -> int:
    """Rulează rollup-ul pe o zi. Întoarce numărul de rânduri scrise."""
    rows = await rollup_demand_day(conn, day)
    log.info("rollup demand_daily %s: %d rânduri", day, rows)
    return rows


async def run_backfill(conn, *, days: list[date]) -> dict[str, int]:
    """Rulează rollup-ul pe mai multe zile. O zi care crapă e logată și sărită — restul
    intervalului se scrie oricum (P6); fiecare zi e o tranzacție separată, deci idempotentă."""
    written = 0
    failed = 0
    for day in days:
        try:
            written += await run_rollup(conn, day=day)
        except Exception:  # noqa: BLE001 — o zi nu blochează backfill-ul
            log.exception("rollup demand_daily eșuat pe %s", day)
            failed += 1
    return {"days": len(days), "rows": written, "failed": failed}


async def run(days: list[date] | None = None) -> dict[str, int]:
    """Entrypoint pentru scheduler: pool + conexiune admin (citește analytics_events)."""
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        return await run_backfill(conn, days=days or [yesterday_utc()])


async def _main() -> None:
    logging.basicConfig(level=logging.INFO)
    days = parse_days(sys.argv[1:])
    try:
        await run(days)
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(_main())
