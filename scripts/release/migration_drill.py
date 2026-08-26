"""NX-248 — drill-uri de migrare pe un Postgres EFEMER (CI), nu pe DB-ul real.

Trei întrebări la care „am rulat migrarea și a mers" nu răspunde:

  **fresh** — schema completă se construiește de la ZERO? Migrările se aplică mereu incremental
  pe DB-ul de dev, deci un `alter table` care presupune o coloană creată manual acum șase luni
  trece neobservat până la primul tenant nou / primul restore.

  **concurrent** — două joburi pornite simultan (retry de deploy, două replici) produc UN singur
  migrator? Fără advisory lock, ambele ar aplica același DDL: al doilea ar pica la mijloc, lăsând
  schema între două stări. Verificat în două probe DETERMINISTE: (A) ținem noi lock-ul dintr-o a
  treia sesiune, deci contenția e garantată, iar `migrate.py` trebuie să iasă `EXIT_LOCKED` fără
  să scrie; (B) doi migratori reali — exact UNUL raportează că a aplicat, un rând în ledger,
  schema la zi. Vezi `_drill_concurrent` pentru de ce aserțiunea veche era dependentă de ceas.

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
    acquire_migration_lock,
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


def _run_migrate() -> subprocess.Popen:
    """Un proces REAL de `migrate.py`. Procese, nu task-uri asyncio: lock-ul e de SESIUNE
    Postgres, iar două task-uri pe aceeași conexiune l-ar obține amândouă."""
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    return subprocess.Popen(
        [sys.executable, str(ROOT / "scripts" / "migrate.py")],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _out(proc: subprocess.Popen) -> tuple[str, str]:
    return (
        proc.stdout.read().decode("utf-8", "replace"),
        proc.stderr.read().decode("utf-8", "replace"),
    )


async def _probe_lock_is_exclusive(pending: str) -> bool:
    """A) Lock-ul ține — DETERMINIST, fără cursă.

    Ținem NOI lock-ul dintr-o a treia sesiune, apoi pornim `migrate.py` peste o migrare pending
    reală. Contenția e garantată de construcție, nu de noroc: procesul TREBUIE să iasă
    `EXIT_LOCKED` și să nu aplice nimic. Dacă lock-ul n-ar funcționa, ar aplica și ar ieși 0 —
    exact ce vrem să prindem.
    """
    holder = await _connect()
    try:
        if not await acquire_migration_lock(holder):
            print("::error::nu am putut lua lock-ul pt probă (altcineva îl ține?)", file=sys.stderr)
            return False
        proc = _run_migrate()
        code = proc.wait(timeout=300)
        stdout, stderr = _out(proc)
    finally:
        await holder.close()  # închiderea sesiunii eliberează advisory lock-ul

    if code != EXIT_LOCKED:
        print(
            f"::error::lock ținut de altă sesiune, dar migrate a ieșit {code} "
            f"(aștept {EXIT_LOCKED})",
            file=sys.stderr,
        )
        print(stdout[:2000] + stderr[:2000], file=sys.stderr)
        return False

    conn = await _connect()
    try:
        state = await migration_state(conn)
    finally:
        await conn.close()
    if pending not in state["pending"]:
        print(
            f"::error::procesul blocat a scris totuși: {pending} nu mai e pending",
            file=sys.stderr,
        )
        return False
    print(f"lock: migrate a ieșit {EXIT_LOCKED} și n-a scris nimic ({pending} încă pending)")
    return True


async def _probe_no_double_apply(pending: str) -> bool:
    """B) Doi migratori nu aplică de două ori — invariantul REAL.

    Codurile acceptate sunt AMBELE rezultate corecte ale unui lock funcțional:
      • `[OK, LOCKED]` — al doilea a găsit lock-ul ținut;
      • `[OK, OK]`     — al doilea a ajuns la lock DUPĂ ce primul terminase, l-a luat curat și
        n-a găsit nimic de aplicat.
    A doua variantă nu e o scăpare de lock, e ordonare — și e exact ce a făcut CI-ul roșu pe 045:
    migrarea era destul de mică încât câștigătorul să termine în fereastra de pornire a
    celuilalt Python. A cere `[OK, LOCKED]` înseamnă a cere ca DDL-ul să fie lent, nu ca lock-ul
    să fie corect.

    Ce verificăm în schimb e ce contează: **exact UN proces raportează că a aplicat** migrarea,
    rândul există o singură dată, iar schema iese la zi. Dacă lock-ul ar ceda, amândoi ar rula
    același DDL — al doilea ar pica la mijloc, sau ar raporta și el aplicarea.
    """
    first, second = _run_migrate(), _run_migrate()
    codes = sorted((first.wait(timeout=300), second.wait(timeout=300)))
    outs = [_out(first), _out(second)]
    print(f"concurent: coduri de ieșire {codes}")

    bad = [c for c in codes if c not in (EXIT_OK, EXIT_LOCKED)]
    if bad or EXIT_OK not in codes:
        print(
            f"::error::coduri neașteptate {codes} (permise: {EXIT_OK}/{EXIT_LOCKED})",
            file=sys.stderr,
        )
        for stdout, stderr in outs:
            print(stdout[:1500] + stderr[:1500], file=sys.stderr)
        return False

    # „A aplicat" se citește din raportul procesului: `aplicat: N migrări în Xms: 045, …`.
    appliers = [i for i, (stdout, _) in enumerate(outs) if pending in stdout.split("aplicat:")[-1]]
    if len(appliers) != 1:
        print(
            f"::error::{len(appliers)} procese raportează că au aplicat {pending} (aștept exact 1)",
            file=sys.stderr,
        )
        for stdout, stderr in outs:
            print(stdout[:1500] + stderr[:1500], file=sys.stderr)
        return False

    conn = await _connect()
    try:
        rows = await conn.fetchval(
            "select count(*) from schema_migrations where version = $1", pending
        )
        state = await migration_state(conn)
    finally:
        await conn.close()
    if rows != 1 or not state["ok"]:
        print(
            f"::error::după cursă: {rows} rânduri pt {pending}, pending={state['pending']}, "
            f"drift={state['drift']}",
            file=sys.stderr,
        )
        return False
    print(f"concurent: exact un proces a aplicat {pending}; un rând în ledger; schema la zi")
    return True


async def _drill_concurrent() -> int:
    """Lock-ul de migrare, în două probe DETERMINISTE.

    Versiunea veche cerea codurile `[OK, LOCKED]` de la două procese pornite simultan. Aserțiunea
    depindea de CRONOMETRAJ: al doilea proces primește `LOCKED` doar dacă ajunge la
    `pg_try_advisory_lock` cât timp primul încă îl ține — deci testul trecea fiindcă migrările de
    până acum erau destul de lente, nu fiindcă lock-ul e corect. Migrarea 045 (un tabel mic) a
    făcut main roșu fără ca nimic din lock să se fi stricat.

    Acum contenția e garantată în proba A (o ținem noi), iar proba B verifică invariantul care
    contează cu adevărat: nimeni nu aplică de două ori.
    """
    pending = await _stage_one_pending()
    print(f"o singură migrare pending ({pending})")
    if not await _probe_lock_is_exclusive(pending):
        return 1
    if not await _probe_no_double_apply(pending):
        return 1
    return 0


def drill_concurrent() -> int:
    return asyncio.run(_drill_concurrent())


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
