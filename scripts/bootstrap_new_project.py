"""Ridică schema completă pe un proiect Supabase NOU și verifică fidelitatea față de sursă.

Rulează O SINGURĂ DATĂ, pe o bază goală. Pașii, în ordine:

  1. preflight    — versiune, extensii disponibile, roluri, `public` chiar gol
  2. extensii     — `vector`, `pg_trgm` (restul le pune Supabase)
  3. roluri       — `bot_runtime` (LOGIN, FĂRĂ bypassrls) + `gdpr_svc`
  4. schemă       — `docs/schema_v3_generated.sql` apoi `docs/schema_v3_delta.sql`
  5. migrări      — marchează 003-045 ca aplicate, cu checksumurile REALE
                    (refolosește `scripts/migrate.py`, nu reimplementează)
  6. partiții     — luna curentă + următoarea pentru `messages` și `analytics_events`
  7. self-check   — compară inventarul de obiecte cu baza SURSĂ, obiect cu obiect

Pasul 7 e motivul pentru care scriptul există. „S-a aplicat fără eroare" nu înseamnă „e la fel":
o politică RLS lipsă sau un CHECK pierdut nu dau eroare la aplicare, dau o gaură de izolare sau
un fapt fals peste șase luni. Comparația cu sursa e singura dovadă.

Rulare:
    TARGET_DB_URL=postgresql://...  \
    SOURCE_DB_URL=postgresql://...  \
    BOT_RUNTIME_PASSWORD=...        \
    python scripts/bootstrap_new_project.py --apply

Fără `--apply` face DOAR preflight și self-check (util ca să verifici o bază deja ridicată).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import date
from pathlib import Path

import asyncpg

ROOT = Path(__file__).resolve().parent.parent
GENERATED = ROOT / "docs" / "schema_v3_generated.sql"
DELTA = ROOT / "docs" / "schema_v3_delta.sql"

# `extensions` intră în search_path fiindcă `pgcrypto` trăiește acolo pe Supabase, iar coloana
# generată `channel_identities.external_id_hash` cheamă `digest()` NEcalificat. Fără asta,
# `create table` pică pe o funcție „inexistentă" care e de fapt doar în alt schema.
SEARCH_PATH = "set search_path = public, extensions"

REQUIRED_EXTENSIONS = ("vector", "pg_trgm")

OK, WARN, BAD = "  ok  ", " ATENTIE ", " ESUAT "


def say(status: str, msg: str) -> None:
    print(f"[{status}] {msg}")


# ============================================================================
# 1. Preflight
# ============================================================================


async def preflight(conn: asyncpg.Connection, *, applying: bool) -> bool:
    ok = True
    version = await conn.fetchval("select current_setting('server_version_num')::int")
    say(OK, f"Postgres {await conn.fetchval('show server_version')}")
    if version < 160000:
        say(BAD, "schema cere Postgres 16+")
        ok = False

    avail = {r["name"] for r in await conn.fetch("select name from pg_available_extensions")}
    for ext in REQUIRED_EXTENSIONS:
        if ext in avail:
            say(OK, f"extensia `{ext}` e disponibila")
        else:
            say(BAD, f"extensia `{ext}` NU e disponibila pe acest proiect")
            ok = False

    tables = await conn.fetchval(
        "select count(*) from information_schema.tables where table_schema = 'public'"
    )
    if applying and tables:
        say(BAD, f"`public` are deja {tables} tabele — bootstrapul ruleaza pe o baza GOALA")
        ok = False
    else:
        say(OK, f"`public` are {tables} tabele")
    return ok


# ============================================================================
# 2-3. Extensii și roluri
# ============================================================================


async def ensure_extensions(conn: asyncpg.Connection) -> None:
    for ext in REQUIRED_EXTENSIONS:
        await conn.execute(f'create extension if not exists "{ext}" with schema public')
        say(OK, f"extensia `{ext}` instalata")


async def ensure_roles(conn: asyncpg.Connection, bot_password: str) -> None:
    """`bot_runtime` e rol de LOGIN cu parolă proprie, nu `SET ROLE`.

    `SET ROLE` se scurge sub multiplexarea poolerului: o conexiune returnată în pool poate
    păstra rolul turului precedent. De asta NX-50 a făcut din `bot_runtime` un rol de login cu
    pool separat. Pe un proiect nou e momentul să pornim direct cu postura corectă.

    Fără `bypassrls`, deliberat: politicile RLS transformă un query scris greșit în „zero
    rezultate" în loc de „datele altui client".
    """
    await conn.execute(
        """
        do $$
        begin
          if not exists (select 1 from pg_roles where rolname = 'bot_runtime') then
            create role bot_runtime nologin nobypassrls;
          end if;
          if not exists (select 1 from pg_roles where rolname = 'gdpr_svc') then
            create role gdpr_svc nologin nobypassrls;
          end if;
        end $$;
        """
    )
    # asyncpg nu parametrizează DDL, deci parola se scapă ca literal SQL.
    escaped = bot_password.replace("'", "''")
    await conn.execute(f"alter role bot_runtime login password '{escaped}'")
    say(OK, "rolurile `bot_runtime` (LOGIN, fara bypassrls) si `gdpr_svc` exista")


# ============================================================================
# 4-5. Schema și migrările
# ============================================================================


async def apply_schema(conn: asyncpg.Connection) -> None:
    for path in (GENERATED, DELTA):
        if not path.exists():
            raise SystemExit(f"lipseste {path}")
        sql = path.read_text(encoding="utf-8")
        # Un singur `execute` pe tot fișierul → protocol simplu → o tranzacție implicită,
        # deci totul sau nimic. O aplicare pe jumătate e mai rea decât una esuata.
        await conn.execute(f"{SEARCH_PATH};\n{sql}")
        say(OK, f"aplicat {path.name}")


async def mark_migrations(conn: asyncpg.Connection) -> None:
    sys.path.insert(0, str(ROOT))
    from scripts.migrate import discover_migrations, mark_applied  # noqa: PLC0415

    versions = [m.version for m in discover_migrations()]
    marked = await mark_applied(conn, versions)
    say(OK, f"marcate ca aplicate {len(marked)} migrari (003-{versions[-1]}), checksumuri reale")


# ============================================================================
# 6. Partiții
# ============================================================================


async def ensure_partitions(conn: asyncpg.Connection) -> None:
    """Luna curentă + următoarea. Fără ele, scrierile cad în partiția DEFAULT."""
    today = date.today()
    months = []
    for offset in (0, 1):
        year, month = divmod(today.year * 12 + today.month - 1 + offset, 12)
        months.append((year, month + 1))

    for parent in ("messages", "analytics_events"):
        for year, month in months:
            start = date(year, month, 1)
            end = date(year + (month == 12), (month % 12) + 1, 1)
            name = f"{parent}_{year}_{month:02d}"
            await conn.execute(
                f"create table if not exists {name} partition of {parent} "
                f"for values from ('{start}') to ('{end}')"
            )
    say(OK, f"partitii pentru {[f'{y}-{m:02d}' for y, m in months]}")


# ============================================================================
# 7. Self-check: comparație obiect cu obiect față de sursă
# ============================================================================

# Fiecare query întoarce (cheie, DEFINIȚIE). Comparația pe NUME e insuficientă și înșelătoare:
# o coloană `price` există în ambele baze și în ambele trece testul de nume, dar poate fi
# `numeric(12,2)` într-una și `numeric` în cealaltă; un CHECK cu același nume poate avea altă
# expresie; o politică RLS poate avea alt `using`. Un „schema completă" bazat pe nume ar fi
# exact felul de verificare care trece în ziua migrării și explodează peste șase luni.
#
# Partițiile se exclud peste tot: sunt date de calendar, nu schemă.
INVENTORY = {
    "tabele": """
        select c.relname,
               c.relkind::text || coalesce(' ' || pg_get_partkeydef(c.oid), '')
        from pg_class c join pg_namespace n on n.oid = c.relnamespace
        where n.nspname='public' and c.relkind in ('r','p') and not c.relispartition
          and not exists (select 1 from pg_depend d where d.objid=c.oid and d.deptype='e')""",
    "coloane": """
        select c.relname || '.' || a.attname,
               format_type(a.atttypid, a.atttypmod)
                 || case when a.attnotnull then ' NOT NULL' else '' end
                 || coalesce(' DEFAULT ' || pg_get_expr(ad.adbin, ad.adrelid), '')
                 || case a.attgenerated when 's' then ' GENERATED' else '' end
                 || case when a.attidentity <> '' then ' IDENTITY' else '' end
        from pg_attribute a
        join pg_class c on c.oid = a.attrelid
        join pg_namespace n on n.oid = c.relnamespace
        left join pg_attrdef ad on ad.adrelid = a.attrelid and ad.adnum = a.attnum
        where n.nspname='public' and c.relkind in ('r','p')
          and not c.relispartition and a.attnum > 0 and not a.attisdropped""",
    "constrangeri": """
        select c.relname || ':' || con.conname, pg_get_constraintdef(con.oid)
        from pg_constraint con join pg_class c on c.oid = con.conrelid
        join pg_namespace n on n.oid = c.relnamespace
        where n.nspname='public' and not c.relispartition""",
    "indexuri": """
        select ic.relname, pg_get_indexdef(i.indexrelid)
        from pg_index i
        join pg_class ic on ic.oid = i.indexrelid
        join pg_class tc on tc.oid = i.indrelid
        join pg_namespace n on n.oid = tc.relnamespace
        where n.nspname='public' and not tc.relispartition""",
    "politici RLS": """
        select tablename || ':' || policyname,
               cmd || ' TO ' || array_to_string(roles, ',')
                 || ' USING ' || coalesce(qual, '-')
                 || ' CHECK ' || coalesce(with_check, '-')
        from pg_policies where schemaname='public'""",
    # Definiția INTEGRALĂ, nu un md5 calculat în SQL: hashul s-ar face peste bytes
    # nenormalizați, deci `_canon` n-ar mai avea ce normaliza și diferențele de CRLF ar
    # rămâne raportate ca divergențe reale.
    "functii": """
        select p.proname || '(' || pg_get_function_identity_arguments(p.oid) || ')',
               pg_get_functiondef(p.oid)
        from pg_proc p join pg_namespace n on n.oid = p.pronamespace
        where n.nspname='public'
          and not exists (select 1 from pg_depend d where d.objid=p.oid and d.deptype='e')""",
    "triggere": """
        select t.tgname, pg_get_triggerdef(t.oid)
        from pg_trigger t join pg_class c on c.oid = t.tgrelid
        join pg_namespace n on n.oid = c.relnamespace
        where n.nspname='public' and not t.tgisinternal and not c.relispartition""",
    "granturi bot_runtime": """
        select table_name || ':' || privilege_type, 'grant'
        from information_schema.role_table_grants
        where table_schema='public' and grantee='bot_runtime'""",
}

# Diferențe pe care delta v3 le produce DELIBERAT. Fără lista asta, raportul ar arăta
# „2 definiții diverg" și ar obliga pe cineva să decidă de fiecare dată dacă e intenționat —
# adică exact obiceiul care face ca a treia divergență, cea reală, să fie bifată din reflex.
EXPECTED_DIFFS: dict[str, str] = {
    "coloane:reviews.rating": "delta A6: devine nullable (o recenzie cu text bun si rating 0)",
    "constrangeri:reviews:reviews_rating_check": "delta A6: accepta NULL",
}


def _canon(value: str | None) -> str:
    """Normalizează terminațiile de linie înainte de comparație.

    Corpurile funcțiilor din baza sursă conțin `\\r\\n`, moștenit din fișiere `.sql` scrise pe
    Windows. Cele din ținta nouă sunt LF curat. Diferența e reală, dar e despre cum a fost
    tastat fișierul acum un an, nu despre ce face funcția — iar dacă o raportăm, raportul
    conține patru zgomote permanente și nimeni nu se mai uită la al cincilea rând.
    `scripts/migrate.py` normalizează identic, la calculul checksumurilor.
    """
    return (value or "").replace("\r\n", "\n")


async def inventory(conn: asyncpg.Connection) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for label, sql in INVENTORY.items():
        out[label] = {r[0]: _canon(r[1]) for r in await conn.fetch(sql)}
    return out


async def self_check(target: asyncpg.Connection, source_dsn: str | None) -> bool:
    tgt = await inventory(target)
    if not source_dsn:
        say(WARN, "fara SOURCE_DB_URL: raportez doar numaratoarea, fara comparatie")
        for label, items in tgt.items():
            say(OK, f"{label}: {len(items)}")
        return True

    src_conn = await asyncpg.connect(source_dsn, statement_cache_size=0)
    try:
        src = await inventory(src_conn)
    finally:
        await src_conn.close()

    ok = True
    for label in INVENTORY:
        missing = sorted(set(src[label]) - set(tgt[label]))
        extra = sorted(set(tgt[label]) - set(src[label]))  # asteptat: delta v3
        diverged = [
            (k, src[label][k], tgt[label][k])
            for k in sorted(set(src[label]) & set(tgt[label]))
            if src[label][k] != tgt[label][k] and f"{label}:{k}" not in EXPECTED_DIFFS
        ]
        intended = [
            k
            for k in set(src[label]) & set(tgt[label])
            if src[label][k] != tgt[label][k] and f"{label}:{k}" in EXPECTED_DIFFS
        ]

        if missing or diverged:
            ok = False
            if missing:
                say(BAD, f"{label}: {len(missing)} LIPSESC")
                for item in missing[:10]:
                    print(f"          - {item}")
                if len(missing) > 10:
                    print(f"          ... inca {len(missing) - 10}")
            if diverged:
                say(BAD, f"{label}: {len(diverged)} DEFINITII DIVERG")
                for key, s, t in diverged[:8]:
                    print(f"          ~ {key}")
                    print(f"              sursa: {s[:150]}")
                    print(f"              tinta: {t[:150]}")
                if len(diverged) > 8:
                    print(f"          ... inca {len(diverged) - 8}")
        else:
            note = f", +{len(extra)} noi in v3" if extra else ""
            note += f", {len(intended)} modificate intentionat" if intended else ""
            say(OK, f"{label}: {len(tgt[label])} identice cu sursa{note}")
    return ok


# ============================================================================


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="chiar scrie (fara el: doar verifica)")
    args = ap.parse_args()

    target_dsn = os.environ.get("TARGET_DB_URL")
    if not target_dsn:
        return int(bool(sys.stderr.write("TARGET_DB_URL lipseste\n"))) or 2
    source_dsn = os.environ.get("SOURCE_DB_URL") or os.environ.get("SUPABASE_DB_URL")

    conn = await asyncpg.connect(target_dsn, statement_cache_size=0, timeout=30)
    try:
        print("=== PREFLIGHT ===")
        if not await preflight(conn, applying=args.apply):
            say(BAD, "preflight esuat, nu continui")
            return 1

        if args.apply:
            bot_password = os.environ.get("BOT_RUNTIME_PASSWORD")
            if not bot_password or len(bot_password) < 16:
                say(BAD, "BOT_RUNTIME_PASSWORD lipseste sau e sub 16 caractere")
                return 2
            print("\n=== APLICARE ===")
            await ensure_extensions(conn)
            await ensure_roles(conn, bot_password)
            await apply_schema(conn)
            await mark_migrations(conn)
            await ensure_partitions(conn)

        print("\n=== SELF-CHECK fata de sursa ===")
        ok = await self_check(conn, source_dsn)
        size = await conn.fetchval("select pg_size_pretty(pg_database_size(current_database()))")
        print(f"\nmarime baza: {size}")
        if ok:
            print("\nSchema e completa fata de sursa.")
            return 0
        print("\nSchema NU e completa. Vezi lipsurile de mai sus.")
        return 1
    finally:
        await conn.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
