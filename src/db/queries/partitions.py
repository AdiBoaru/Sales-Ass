"""NX-218 — întreținerea partițiilor lunare (`analytics_events`, `messages`).

Schema declară partiționare pe lună, dar creează partițiile MANUAL (schema_v2 avea doar
2026_06/2026_07 + DEFAULT) — deci fără un job care le creează din timp, totul aterizează în
partiția DEFAULT: scanări care cresc monoton și retenție prin `drop partition` imposibilă.

Aici stă DOAR SQL-ul (convenția `src/db/queries/`); orchestrarea + logurile sunt în
`src/jobs/partition_maintenance.py`. DDL cross-tenant → conexiune ADMIN (ca `cleanup_dedupe`).

Identificatorii se construiesc din nume de tabel + dată, deci NU pot veni din input de user;
`_ident` rămâne o plasă explicită (fail-closed) pentru cazul în care cineva pasează un nume
dinamic într-un apel viitor.
"""

from __future__ import annotations

import re
from datetime import date

import asyncpg

# Tabelele partiționate pe lună din schema_v2 (`partition by range (created_at)`).
PARTITIONED_TABLES: tuple[str, ...] = ("analytics_events", "messages")

_IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def _ident(name: str) -> str:
    """Validează un identificator simplu (fail-closed). Numele partițiilor sunt derivate din
    nume de tabel + `YYYY_MM`, deci trec mereu; orice altceva e o eroare de programare."""
    if not _IDENT_RE.match(name):
        raise ValueError(f"identificator invalid: {name!r}")
    return name


def month_start(day: date) -> date:
    """Prima zi a lunii lui `day` (ancora intervalului de partiție)."""
    return day.replace(day=1)


def next_month(month: date) -> date:
    """Prima zi a lunii următoare (limita superioară, exclusivă)."""
    return date(month.year + (month.month == 12), month.month % 12 + 1, 1)


def partition_name(table: str, month: date) -> str:
    """Numele partiției lunii: `analytics_events_2026_08` (convenția din schema_v2)."""
    return f"{_ident(table)}_{month:%Y_%m}"


def default_partition_name(table: str) -> str:
    """Numele partiției DEFAULT: `analytics_events_default` (convenția din schema_v2)."""
    return f"{_ident(table)}_default"


async def partition_exists(conn: asyncpg.Connection, name: str) -> bool:
    """True dacă relația există (`to_regclass` respectă search_path, ca restul schemei)."""
    return await conn.fetchval("select to_regclass($1) is not null", _ident(name))


async def create_month_partition(conn: asyncpg.Connection, table: str, month: date) -> bool:
    """Creează partiția lunii dacă lipsește. Întoarce True dacă a creat-o acum.

    `if not exists` acoperă cursa dintre două instanțe de scheduler (verificarea și crearea nu
    sunt atomice). Dacă partiția DEFAULT conține deja rânduri din interval, Postgres REFUZĂ
    crearea — e cazul reparat o singură dată de migrarea 037; aici lăsăm eroarea să urce, jobul
    o loghează ca warning acționabil (nu o înghițim: ar ascunde exact problema pe care o vânăm).
    """
    name = partition_name(table, month)
    if await partition_exists(conn, name):
        return False
    lo, hi = month, next_month(month)
    await conn.execute(
        f'create table if not exists "{name}" partition of "{_ident(table)}" '
        f"for values from ('{lo:%Y-%m-%d}') to ('{hi:%Y-%m-%d}')"
    )
    return True


async def default_row_count(conn: asyncpg.Connection, table: str) -> int:
    """Câte rânduri stau în partiția DEFAULT. `> 0` = o perioadă n-a avut partiție la timp →
    semnal, nu dezastru (datele sunt acolo, dar nu se pot arhiva/dropa pe interval)."""
    name = default_partition_name(table)
    if not await partition_exists(conn, name):
        return 0
    return int(await conn.fetchval(f'select count(*) from "{name}"'))
