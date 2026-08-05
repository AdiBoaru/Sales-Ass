"""NX-218 — teste INTEGRATION (DB reală, tabel partiționat throwaway, drop la teardown).

Exclus din CI fast (`-m "not integration"`). Acoperă exact ce nu se poate dovedi fără Postgres:
  - o partiție creată din timp preia scrierile (rândul NU mai cade în DEFAULT);
  - jobul e idempotent (a doua rulare nu creează nimic, nu crapă);
  - dacă DEFAULT-ul conține deja rânduri din interval, Postgres REFUZĂ crearea partiției —
    exact situația reparată de docs/037_partition_maintenance.sql; jobul o raportează ca
    `failed`, nu o înghite.

Lucrăm pe un tabel propriu (`nx218_probe`), NU pe analytics_events/messages: testul nu are voie
să facă DDL pe tabelele hot de producție.
"""

from datetime import date, datetime, timezone
from uuid import uuid4

import pytest

from src.db.connection import admin_conn, close_pool, get_pool
from src.db.queries.partitions import default_row_count, partition_exists
from src.jobs.partition_maintenance import ensure_partitions

pytestmark = [pytest.mark.integration]

UTC = timezone.utc


@pytest.fixture
async def probe():
    """Tabel partiționat pe lună, cu DEFAULT — copia structurii din schema_v2 (identity + PK
    compus pe (id, created_at)), ca testul să prindă și capcanele de identity."""
    pool = await get_pool()
    name = f"nx218_probe_{uuid4().hex[:8]}"
    async with admin_conn(pool) as conn:
        await conn.execute(
            f"""
            create table {name} (
              id         bigint generated always as identity,
              created_at timestamptz not null default now(),
              primary key (id, created_at)
            ) partition by range (created_at);
            create table {name}_default partition of {name} default;
            """
        )
    try:
        yield name
    finally:
        async with admin_conn(pool) as conn:
            await conn.execute(f"drop table if exists {name} cascade")
        await close_pool()


async def _insert_at(conn, table: str, when: datetime) -> None:
    await conn.execute(f"insert into {table} (created_at) values ($1)", when)


async def _partition_of(conn, table: str, when: datetime) -> str:
    """În ce partiție a aterizat rândul (tableoid = adevărul, nu presupunerea)."""
    return await conn.fetchval(
        f"select tableoid::regclass::text from {table} where created_at = $1", when
    )


async def test_partition_created_ahead_takes_the_writes(probe):
    """Rândul din luna viitoare aterizează în partiția nouă, NU în DEFAULT."""
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        out = await ensure_partitions(conn, today=date(2026, 8, 5), tables=(probe,))
        assert f"{probe}_2026_08" in out["created"]
        assert f"{probe}_2026_09" in out["created"]

        when = datetime(2026, 9, 14, 10, 0, tzinfo=UTC)
        await _insert_at(conn, probe, when)
        assert await _partition_of(conn, probe, when) == f"{probe}_2026_09"
        assert await default_row_count(conn, probe) == 0


async def test_second_run_is_idempotent(probe):
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        await ensure_partitions(conn, today=date(2026, 8, 5), tables=(probe,))
        again = await ensure_partitions(conn, today=date(2026, 8, 5), tables=(probe,))
        assert again["created"] == []
        assert again["failed"] == []


async def test_rows_already_in_default_block_creation_and_are_reported(probe):
    """Situația reală de la 1 aug 2026: s-a scris fără partiție → rândurile stau în DEFAULT și
    Postgres refuză crearea partiției peste intervalul lor. Jobul NU înghite: raportează
    `failed` + numărul de rânduri rămase în DEFAULT (semnalul care cere migrarea 037)."""
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        when = datetime(2026, 10, 3, 8, 0, tzinfo=UTC)
        await _insert_at(conn, probe, when)  # fără partiție → cade în DEFAULT
        assert await _partition_of(conn, probe, when) == f"{probe}_default"

        out = await ensure_partitions(conn, today=date(2026, 10, 1), tables=(probe,))
        assert f"{probe}_2026_10" in out["failed"]
        assert not await partition_exists(conn, f"{probe}_2026_10")
        assert out["default_rows"][probe] == 1
        # luna următoare (goală în DEFAULT) se creează oricum — un eșec nu blochează restul
        assert f"{probe}_2026_11" in out["created"]


async def test_migration_037_move_technique(probe):
    """Validează MECANISMUL migrării 037 (partea riscantă), pe un tabel throwaway: mută rândurile
    din DEFAULT → creează partiția → reinserează prin părinte. Verifică inclusiv `overriding
    system value` (fără el, reinserarea unui `generated always as identity` crapă) și că ID-urile
    se PĂSTREAZĂ (nu se regenerează — altfel s-ar rupe orice referință externă)."""
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        when = datetime(2026, 10, 3, 8, 0, tzinfo=UTC)
        await _insert_at(conn, probe, when)
        original_id = await conn.fetchval(f"select id from {probe} where created_at = $1", when)

        async with conn.transaction():
            await conn.execute(
                f"""create temp table _mv as
                    with moved as (
                      delete from {probe}_default
                      where created_at >= '2026-10-01' and created_at < '2026-11-01'
                      returning *
                    )
                    select * from moved"""
            )
            await conn.execute(
                f"create table {probe}_2026_10 partition of {probe} "
                f"for values from ('2026-10-01') to ('2026-11-01')"
            )
            await conn.execute(f"insert into {probe} overriding system value select * from _mv")
            await conn.execute("drop table _mv")

        assert await _partition_of(conn, probe, when) == f"{probe}_2026_10"
        assert await conn.fetchval(f"select id from {probe} where created_at = $1", when) == (
            original_id
        )
        assert await default_row_count(conn, probe) == 0
