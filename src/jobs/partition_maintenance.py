"""Job de întreținere a partițiilor lunare (NX-218).

Creează din timp partițiile `analytics_events` / `messages` pentru luna curentă + lunile
următoare, ca scrierile să NU mai aterizeze în partiția DEFAULT (unde au stat toate scrierile
de la 1 august 2026 — vezi docs/037_partition_maintenance.sql pentru reparația istoriei).

Rulează zilnic din scheduler. Crearea „cu o lună înainte" e esențială: o partiție creată
ÎNAINTE să existe rânduri în intervalul ei nu cere nicio mutare de date. Dacă totuși găsim
rânduri în DEFAULT, logăm warning cu numărul lor — semnal că o perioadă a scăpat.

Un tabel care crapă e logat și sărit (P6: degradare, nu cădere tăcută).

Rulează standalone: python -m src.jobs.partition_maintenance
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, date, datetime

from src.db.connection import admin_conn, close_pool, get_pool
from src.db.queries.partitions import (
    PARTITIONED_TABLES,
    create_month_partition,
    default_row_count,
    month_start,
    next_month,
    partition_name,
)

log = logging.getLogger(__name__)

# Câte luni ÎNAINTE de cea curentă asigurăm. 1 = luna viitoare e gata cu ~30 zile în avans;
# suficient pentru un job zilnic, chiar dacă e oprit câteva zile.
MONTHS_AHEAD = 1


def target_months(today: date, months_ahead: int = MONTHS_AHEAD) -> list[date]:
    """Lunile de asigurat: cea curentă + `months_ahead` următoare (prima zi a fiecăreia)."""
    months = [month_start(today)]
    for _ in range(months_ahead):
        months.append(next_month(months[-1]))
    return months


async def ensure_partitions(
    conn,
    *,
    today: date | None = None,
    months_ahead: int = MONTHS_AHEAD,
    tables: tuple[str, ...] = PARTITIONED_TABLES,
) -> dict[str, object]:
    """Asigură partițiile lunare + raportează starea DEFAULT-ului.

    Întoarce {created: [nume], failed: [nume], default_rows: {tabel: n}}. Nu ridică: un tabel
    care crapă (ex. DEFAULT cu rânduri în intervalul cerut) e logat ca warning acționabil și
    sărit — restul tabelelor se asigură oricum."""
    today = today or datetime.now(UTC).date()
    created: list[str] = []
    failed: list[str] = []
    default_rows: dict[str, int] = {}

    for table in tables:
        for month in target_months(today, months_ahead):
            name = partition_name(table, month)
            try:
                if await create_month_partition(conn, table, month):
                    created.append(name)
                    log.info("partiție creată: %s", name)
            except Exception:  # noqa: BLE001 — un tabel nu oprește restul (P6)
                failed.append(name)
                log.exception(
                    "nu am putut crea partiția %s — probabil DEFAULT-ul conține deja rânduri "
                    "din interval (vezi docs/037_partition_maintenance.sql pentru mutare)",
                    name,
                )
        try:
            n = await default_row_count(conn, table)
        except Exception:  # noqa: BLE001
            log.exception("nu am putut număra %s_default", table)
            continue
        default_rows[table] = n
        if n:
            log.warning(
                "%s_default conține %d rânduri — o perioadă a fost scrisă fără partiție; "
                "retenția pe interval nu funcționează până nu sunt mutate",
                table,
                n,
            )

    log.info(
        "partition_maintenance: created=%d failed=%d default_rows=%s",
        len(created),
        len(failed),
        default_rows,
    )
    return {"created": created, "failed": failed, "default_rows": default_rows}


async def run(months_ahead: int = MONTHS_AHEAD) -> dict[str, object]:
    """Entrypoint pentru scheduler: pool + conexiune admin (DDL cross-tenant)."""
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        return await ensure_partitions(conn, months_ahead=months_ahead)


async def _main() -> None:
    logging.basicConfig(level=logging.INFO)
    try:
        await run()
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(_main())
