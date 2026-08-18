"""NX-248 — drill-uri de migrare pe un Postgres EFEMER (CI), nu pe DB-ul real.

Trei întrebări la care „am rulat migrarea și a mers" nu răspunde:

  **fresh** — schema completă se construiește de la ZERO? Migrările se aplică mereu incremental
  pe DB-ul de dev, deci un `alter table` care presupune o coloană creată manual acum șase luni
  trece neobservat până la primul tenant nou / primul restore.

  **concurrent** — două joburi pornite simultan (retry de deploy, două replici) produc UN singur
  migrator? Fără advisory lock, ambele ar aplica același DDL: al doilea ar pica la mijloc, lăsând
  schema între două stări. Aici verificăm exact contractul: unul câștigă (cod 0), celălalt iese
  cu `EXIT_LOCKED` (3) FĂRĂ să scrie nimic.

  **idempotent** — a doua rulare e no-op? Dacă nu, orice reîncercare de deploy devine o migrare
  nouă.

Rulează DOAR pe DSN-uri efemere. Refuză explicit orice DSN care nu arată a localhost/CI: un drill
care poate rula din greșeală pe producție e un incident care așteaptă o variabilă de mediu greșită.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import asyncpg  # noqa: E402

from scripts.migrate import (  # noqa: E402
    EXIT_LOCKED,
    EXIT_OK,
    apply_pending,
    discover_migrations,
    migration_state,
)

#: Hosturi pe care ACCEPTĂM să rulăm drill-uri distructive. Orice altceva = refuz.
_EPHEMERAL_HOSTS = frozenset({"localhost", "127.0.0.1", "postgres", "db", "::1"})


def _dsn() -> str:
    dsn = os.environ.get("DATABASE_URL_MIGRATION") or os.environ.get("SUPABASE_DB_URL") or ""
    host = (urlparse(dsn).hostname or "").lower()
    if host not in _EPHEMERAL_HOSTS:
        raise SystemExit(
            f"REFUZ: drill-urile de migrare rulează doar pe DB efemer (host={host!r}). "
            "Nu există flag care să treacă peste asta — vezi docs/DISASTER-RECOVERY.md pentru "
            "verificarea pe un restore izolat."
        )
    return dsn


async def _connect() -> asyncpg.Connection:
    return await asyncpg.connect(_dsn(), statement_cache_size=0)


async def _reset(conn: asyncpg.Connection) -> None:
    """DB gol: schema publică recreată. Sigur DOAR pentru că `_dsn()` a verificat hostul."""
    await conn.execute(
        "drop schema if exists public cascade; drop schema if exists auth cascade; "
        "create schema public"
    )


#: Schema de BAZĂ. Migrările numerotate (003→042) sunt DELTE peste ea, nu o construiesc: prima
#: rulare a acestui drill a picat cu `relation "products" does not exist`, ceea ce e chiar
#: informația utilă — „fresh → latest" înseamnă `schema_v2_production.sql` + delte, în ordinea
#: asta. Un restore din backup e alt scenariu (are deja totul) și se verifică separat, în
#: `scripts/dr/restore_verify.py`.
BASE_SCHEMA = ROOT / "docs" / "schema_v2_production.sql"

#: Ce ia schema noastră de la PLATFORMĂ, nu de la ea însăși.
#:
#: A doua rulare a drill-ului a picat cu `schema "auth" does not exist`: politicile RLS de
#: dashboard folosesc `auth.uid()` și `auth.users`, care în producție vin de la Supabase. Pe un
#: Postgres gol nu există. Shimul de mai jos e minimul care le înlocuiește ÎN CI — și, mai
#: important, e inventarul EXPLICIT al dependenței de platformă: dacă vreodată restaurăm în afara
#: Supabase (incident de furnizor), asta e lista de care avem nevoie. Vezi
#: docs/DISASTER-RECOVERY.md §Dependențe de platformă.
#:
#: NU e o migrare și nu ajunge niciodată în `docs/0NN_*.sql`: în producție, `auth` e al
#: platformei, iar o migrare care l-ar crea ar intra în conflict cu el.
_PLATFORM_SHIM = """
create schema if not exists auth;
create table if not exists auth.users (id uuid primary key);
create or replace function auth.uid() returns uuid language sql stable as $$ select null::uuid $$;
create extension if not exists vector;
create extension if not exists pg_trgm;
do $$
declare
  r text;
begin
  -- Rolurile pe care Supabase le creează pentru noi (dashboard + API). Schema le dă GRANT-uri,
  -- deci fără ele `create policy`/`grant` pică. Aici sunt `nologin`: shimul reproduce STRUCTURA
  -- de permisiuni, nu identitățile — un rol care se poate loga într-un drill e o ușă în plus.
  foreach r in array array['anon', 'authenticated', 'service_role', 'gdpr_svc']
  loop
    if not exists (select 1 from pg_roles where rolname = r) then
      execute format('create role %I nologin', r);
    end if;
  end loop;
end $$;
"""


#: Migrări care NU se pot aplica de runner-ul standard, cu motivul exact.
#:
#: `005` conține un parametru în stil psql (`:'bot_password'`), gândit pentru `apply_005.py`.
#: Runner-ul îl trimite ca SQL brut și Postgres răspunde `syntax error at or near ":"`. Pe DB-ul
#: de producție nu se vede: 005 e marcat aplicat prin `--baseline` (istoric). Se vede DOAR pe o
#: instalare de la zero — adică exact la provisioningul unui client nou sau la un restore în alt
#: proiect. Drill-ul substituie o parolă de unică folosință ca să poată continua; remedierea
#: reală (un `0NN` care ia parola din `current_setting`, sau scoaterea lui 005 din calea
#: runner-ului) e notată în docs/PRODUCTION-READINESS.md §Datorii cunoscute.
_PARAMETERIZED = {
    "005": (":'bot_password'", "'drill-only-throwaway'"),
}


async def _apply_all(conn: asyncpg.Connection) -> list[str]:
    """Aplică migrările în ordine, substituind parametrii psql cunoscuți (doar în drill)."""
    applied: list[str] = []
    for migration in discover_migrations():
        sql = migration.path.read_text(encoding="utf-8")
        substitution = _PARAMETERIZED.get(migration.version)
        if substitution:
            sql = sql.replace(*substitution)
        async with conn.transaction():
            await conn.execute(sql)
            await conn.execute(
                "insert into schema_migrations(version, filename, checksum) "
                "values ($1, $2, $3) on conflict (version) do nothing",
                migration.version,
                migration.filename,
                migration.checksum,
            )
        applied.append(migration.version)
    return applied


async def drill_fresh() -> int:
    conn = await _connect()
    try:
        await _reset(conn)
        await conn.execute(_PLATFORM_SHIM)
        await conn.execute(BASE_SCHEMA.read_text(encoding="utf-8"))
        await conn.execute(
            "create table if not exists schema_migrations ("
            "version text primary key, filename text not null, checksum text not null, "
            "applied_at timestamptz not null default now())"
        )
        applied = await _apply_all(conn)
        state = await migration_state(conn)
        expected = len(discover_migrations())
        print(f"fresh: aplicat {len(applied)}/{expected}, stare={state}")
        if len(applied) != expected or not state["ok"]:
            print("::error::schema completă NU se construiește de la zero", file=sys.stderr)
            return 1
        return 0
    finally:
        await conn.close()


async def drill_idempotent() -> int:
    conn = await _connect()
    try:
        again = await apply_pending(conn)
        print(f"idempotent: a doua rulare a aplicat {len(again)} migrări (aștept 0)")
        return 0 if not again else 1
    finally:
        await conn.close()


async def _stage_one_pending() -> str:
    """Aduce DB-ul în starea „exact o migrare de aplicat", ca să existe pe ce se contesta.

    Fără asta, testul de concurență e o iluzie: pe o schemă deja la zi, ambele procese n-au nimic
    de făcut, ies amândouă cu 0 și lock-ul nu e exersat niciodată. (Prima rulare a dat chiar
    `[0, 0]` — un verde care nu demonstra nimic.)
    """
    conn = await _connect()
    try:
        await _reset(conn)
        await conn.execute(_PLATFORM_SHIM)
        await conn.execute(BASE_SCHEMA.read_text(encoding="utf-8"))
        await conn.execute(
            "create table if not exists schema_migrations ("
            "version text primary key, filename text not null, checksum text not null, "
            "applied_at timestamptz not null default now())"
        )
        migrations = discover_migrations()
        last = migrations[-1]
        for migration in migrations[:-1]:
            sql = migration.path.read_text(encoding="utf-8")
            substitution = _PARAMETERIZED.get(migration.version)
            if substitution:
                sql = sql.replace(*substitution)
            async with conn.transaction():
                await conn.execute(sql)
                await conn.execute(
                    "insert into schema_migrations(version, filename, checksum) "
                    "values ($1, $2, $3) on conflict (version) do nothing",
                    migration.version,
                    migration.filename,
                    migration.checksum,
                )
        return last.version
    finally:
        await conn.close()


def drill_concurrent() -> int:
    """Două procese REALE de `migrate.py`, pornite împreună peste EXACT o migrare pending.

    Procese, nu task-uri asyncio: lock-ul e de SESIUNE Postgres, iar două task-uri pe aceeași
    conexiune l-ar obține amândouă — testul ar trece degeaba.
    """
    pending = asyncio.run(_stage_one_pending())
    print(f"concurrent: o singură migrare pending ({pending}); pornesc două procese")
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    cmd = [sys.executable, str(ROOT / "scripts" / "migrate.py")]
    first = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    second = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    codes = sorted((first.wait(timeout=300), second.wait(timeout=300)))
    print(f"concurrent: coduri de ieșire {codes} (aștept [{EXIT_OK}, {EXIT_LOCKED}])")
    # Exact un câștigător. `[0, 0]` NU mai e acceptat aici: cu o migrare pending garantată, doi de
    # zero ar însemna că al doilea a intrat peste primul — adică lock-ul n-a făcut nimic.
    if codes == [EXIT_OK, EXIT_LOCKED]:
        return 0
    print("::error::concurența pe migrare NU e rezolvată de lock", file=sys.stderr)
    for proc in (first, second):
        print(proc.stderr.read().decode("utf-8", "replace")[:2000], file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    which = args[0] if args else "fresh"
    if which == "fresh":
        return asyncio.run(drill_fresh())
    if which == "idempotent":
        return asyncio.run(drill_idempotent())
    if which == "concurrent":
        return drill_concurrent()
    print(f"drill necunoscut: {which!r} (fresh | concurrent | idempotent)", file=sys.stderr)
    return 2


if __name__ == "__main__":
    from src.ops.cli import enable_utf8_stdout

    enable_utf8_stdout()

    raise SystemExit(main())
