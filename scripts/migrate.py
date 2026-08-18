"""Runner ordonat de migrări + poartă de boot (NX-123), one-shot și blocat (NX-248).

Înlocuiește scripturile one-off `apply_0NN.py` (fire-and-forget, fără stare) cu:
  • un singur entrypoint care descoperă `docs/0NN_*.sql`, le aplică ORDONAT NUMERIC
    (prefix, nu lexicografic — 010 > 009) și înregistrează fiecare în
    `schema_migrations` (o tranzacție per fișier, fail-fast);
  • idempotență: o migrare deja înregistrată (checksum potrivit / 'legacy') se sare;
  • `--check`: cod ≠0 dacă există migrări pending sau drift de checksum — folosit de
    poarta de boot a workerului ȘI ca pas de CI;
  • `--dry-run`: listează pending fără a aplica;
  • `--baseline`: ADOPTARE pe o DB de PROD existentă (003–013 deja aplicate manual) —
    marchează tot ce e pe disc ca aplicat ('legacy') FĂRĂ a rula SQL-ul;
  • `--mark-applied 005[,NNN]`: marchează PUNCTUAL, cu checksumul real, migrările care nu pot
    trece prin `conn.execute` (variabile psql) — aplicate în afara runnerului, dar înregistrate
    de el, deci `--check` continuă să vadă drift;
  • `--json`: starea ca artefact (preflight-ul de release o consumă, vezi scripts/release/).

## NX-248 — două credentiale, un singur migrator

**Credentialul de DDL nu trăiește în runtime.** `DATABASE_URL_MIGRATION` există doar în jobul de
migrare (un serviciu compose sub profil, pornit înainte de deploy); serviciile de runtime n-au
nevoie de el, fiindcă `--check` cere doar SELECT pe `schema_migrations`. Un proces care nu are
credentialul nu poate face DDL nici din greșeală, nici dacă e compromis — asta e diferența dintre
„nu aplicăm migrări în runtime, prin convenție" și „nu putem".

**Un singur migrator la un moment dat.** Aplicarea ia un advisory lock de SESIUNE; al doilea job
concurent primește `False` și iese cu cod dedicat (`EXIT_LOCKED`), fără să scrie nimic. Lock-ul
se și VERIFICĂ după acquire: dacă nu se vede în `pg_locks` pe backend-ul curent, înseamnă că
sesiunea trece printr-un pooler în mod tranzacție (unde un lock de sesiune e o iluzie), iar
scriptul refuză să continue. Migrările cer conexiune DIRECTĂ; a descoperi asta în timpul unui DDL
parțial aplicat e mult mai scump decât a o refuza la început.

Reutilizează handling-ul IPv4 + SSL pentru pooler-ul Supabase (cf. memory „DB URL password
encoding").

Importabil: `assert_migrations_current(pool)` e poarta de boot apelată din
`src/worker/consumer.py` înainte de XREADGROUP (P6 — nu boot peste schemă incompletă).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import socket
import ssl
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import asyncpg

DOCS_DIR = Path(__file__).resolve().parent.parent / "docs"
# numele fișierelor de migrare: <prefix numeric>_<slug>.sql, ex. 014_schema_migrations.sql
_MIGRATION_RE = re.compile(r"^(\d+)_.*\.sql$")

# Coduri de ieșire — citite de `scripts/release/preflight.py` și de jobul de migrare din compose.
# Distincte fiindcă cer acțiuni distincte: „mai încearcă" (blocat) ≠ „repară" (eroare).
EXIT_OK = 0
EXIT_PENDING = 1  # --check: există migrări neaplicate / drift
EXIT_LOCKED = 3  # alt migrator ține lock-ul: NU e eroare, e concurență rezolvată corect
EXIT_UNSAFE = 4  # sesiune prin pooler tranzacțional, sau credential greșit pentru operație

# Cheia advisory (constantă, arbitrară dar STABILĂ: dacă se schimbă, două versiuni de script nu
# se mai exclud reciproc — exact bugul pe care lock-ul trebuie să-l prevină).
_LOCK_KEY = 0x4E58323438  # "NX248" în hex, pe 40 de biți

# DDL canonic = docs/014_schema_migrations.sql. Bootstrap-ul de aici (IF NOT EXISTS) doar
# garantează că putem INTEROGA starea pe prima rulare, înainte ca 014 să fie aplicat.
_BOOTSTRAP_DDL = """
create table if not exists schema_migrations (
  version    text primary key,
  filename   text not null,
  checksum   text not null,
  applied_at timestamptz not null default now()
)
"""


@dataclass(frozen=True)
class Migration:
    version: str  # prefix numeric din numele fișierului (ex. "014")
    filename: str  # numele complet
    path: Path
    checksum: str  # sha256 al conținutului


def discover_migrations(docs_dir: Path = DOCS_DIR) -> list[Migration]:
    """docs/0NN_*.sql sortate NUMERIC pe prefix (010 > 009, nu lexicografic)."""
    out: list[Migration] = []
    for p in sorted(docs_dir.glob("*.sql")):
        m = _MIGRATION_RE.match(p.name)
        if not m:
            continue
        # Normalizăm CRLF→LF ÎNAINTE de hash: altfel același fișier dă checksum diferit pe
        # Windows (dev, autocrlf) vs Linux (CI) → drift fals. Conținutul aplicat (read_text)
        # rămâne neatins; doar amprenta e platform-independentă.
        normalized = p.read_text(encoding="utf-8").replace("\r\n", "\n").encode("utf-8")
        out.append(
            Migration(
                version=m.group(1),
                filename=p.name,
                path=p,
                checksum=hashlib.sha256(normalized).hexdigest(),
            )
        )
    out.sort(key=lambda mig: int(mig.version))
    return out


# --------------------------------------------------------------------------- #
# Operații pe o conexiune deja deschisă (importabile — reutilizate de boot gate)
# --------------------------------------------------------------------------- #


async def _applied(conn: asyncpg.Connection) -> dict[str, str]:
    """version -> checksum din schema_migrations; {} dacă tabelul nu există încă."""
    try:
        rows = await conn.fetch("select version, checksum from schema_migrations")
    except asyncpg.UndefinedTableError:
        return {}
    return {r["version"]: r["checksum"] for r in rows}


async def pending_migrations(
    conn: asyncpg.Connection, docs_dir: Path = DOCS_DIR
) -> list[Migration]:
    """Migrările de pe disc care NU apar încă în schema_migrations (tabel lipsă → toate)."""
    applied = await _applied(conn)
    return [m for m in discover_migrations(docs_dir) if m.version not in applied]


async def checksum_drift(conn: asyncpg.Connection, docs_dir: Path = DOCS_DIR) -> list[Migration]:
    """Migrări înregistrate al căror FIȘIER s-a schimbat după aplicare (checksum ≠),
    excluzând rândurile 'legacy' (backfill istoric, fără checksum real)."""
    applied = await _applied(conn)
    drift: list[Migration] = []
    for m in discover_migrations(docs_dir):
        rec = applied.get(m.version)
        if rec is not None and rec != "legacy" and rec != m.checksum:
            drift.append(m)
    return drift


async def apply_pending(
    conn: asyncpg.Connection, docs_dir: Path = DOCS_DIR, *, dry_run: bool = False
) -> list[str]:
    """Aplică migrările pending în ordine numerică. O tranzacție per fișier (fail-fast):
    `BEGIN; <sql>; INSERT INTO schema_migrations; COMMIT`. Întoarce versiunile aplicate."""
    if not dry_run:
        # bootstrap tabelul ca să putem interoga starea (014 îl creează oricum; idempotent).
        await conn.execute(_BOOTSTRAP_DDL)
    done: list[str] = []
    for m in await pending_migrations(conn, docs_dir):
        if dry_run:
            done.append(m.version)
            continue
        async with conn.transaction():
            await conn.execute(m.path.read_text(encoding="utf-8"))
            await conn.execute(
                "insert into schema_migrations(version, filename, checksum) "
                "values ($1, $2, $3) on conflict (version) do nothing",
                m.version,
                m.filename,
                m.checksum,
            )
        done.append(m.version)
    return done


async def baseline(conn: asyncpg.Connection, docs_dir: Path = DOCS_DIR) -> list[str]:
    """Adoptare pe o DB EXISTENTĂ: marchează tot ce e pe disc ca aplicat ('legacy')
    FĂRĂ a rula SQL-ul. De rulat O SINGURĂ DATĂ pe o DB unde 003+ sunt deja aplicate
    manual (altfel folosește `apply_pending`)."""
    await conn.execute(_BOOTSTRAP_DDL)
    marked: list[str] = []
    for m in discover_migrations(docs_dir):
        res = await conn.execute(
            "insert into schema_migrations(version, filename, checksum) "
            "values ($1, $2, 'legacy') on conflict (version) do nothing",
            m.version,
            m.filename,
        )
        if res.split()[-1] == "1":  # "INSERT 0 1" → chiar a inserat
            marked.append(m.version)
    return marked


async def mark_applied(
    conn: asyncpg.Connection, versions: list[str], docs_dir: Path = DOCS_DIR
) -> list[str]:
    """Marchează migrări PUNCTUALE ca aplicate, cu checksumul REAL, fără a le rula.

    Există pentru o singură clasă de fișiere: cele care nu pot trece prin `conn.execute` fiindcă
    folosesc variabile psql. `005_bot_runtime_login.sql` conține `:'bot_password'` — parola nu se
    comite, deci pe DB-ul real a fost aplicată cu `apply_005.py`. Pe un Postgres EFEMER (NX-247)
    o aplică `psql -v bot_password=…`, iar apoi runnerul trebuie să afle că e făcută.

    Diferența față de `--baseline`: acolo se marchează TOT ca `'legacy'`, ceea ce pe o DB goală ar
    sări peste toate migrările și ar lăsa o schemă incompletă declarată completă. Aici se
    marchează exact ce s-a cerut, cu checksumul de pe disc — deci `--check` continuă să detecteze
    dacă fișierul e editat ulterior.
    """
    await conn.execute(_BOOTSTRAP_DDL)
    by_version = {m.version: m for m in discover_migrations(docs_dir)}
    unknown = sorted(set(versions) - set(by_version))
    if unknown:
        raise RuntimeError(f"versiuni inexistente în docs/: {', '.join(unknown)}")
    marked: list[str] = []
    for version in versions:
        m = by_version[version]
        res = await conn.execute(
            "insert into schema_migrations(version, filename, checksum) "
            "values ($1, $2, $3) on conflict (version) do nothing",
            m.version,
            m.filename,
            m.checksum,
        )
        if res.split()[-1] == "1":
            marked.append(m.version)
    return marked


class MigrationLockUnsafe(RuntimeError):
    """Lock-ul a fost „luat" dar nu se vede pe backend-ul curent ⇒ sesiune multiplexată."""


async def acquire_migration_lock(conn: asyncpg.Connection) -> bool:
    """Advisory lock de SESIUNE pentru aplicarea migrărilor. `False` = altcineva migrează acum.

    După acquire, VERIFICĂM lock-ul în `pg_locks` pe `pg_backend_pid()`. Verificarea nu e
    paranoia: prin pgbouncer în mod tranzacție, fiecare statement poate ajunge pe alt backend,
    deci `pg_try_advisory_lock` întoarce `true` pe o sesiune pe care n-o mai vedem niciodată —
    iar două joburi ar crede amândouă că au lock-ul. Preferăm să refuzăm decât să aplicăm DDL
    într-o cursă pe care nu o putem observa.
    """
    got = await conn.fetchval("select pg_try_advisory_lock($1)", _LOCK_KEY)
    if not got:
        return False
    visible = await conn.fetchval(
        "select exists(select 1 from pg_locks where locktype = 'advisory' "
        "and objid = $1::bigint % 4294967296 and pid = pg_backend_pid())",
        _LOCK_KEY,
    )
    if not visible:
        raise MigrationLockUnsafe(
            "advisory lock invizibil pe backend-ul curent — conexiunea trece printr-un pooler "
            "tranzacțional. Migrările cer conexiune DIRECTĂ (port 5432), nu pooler."
        )
    return True


async def release_migration_lock(conn: asyncpg.Connection) -> None:
    await conn.fetchval("select pg_advisory_unlock($1)", _LOCK_KEY)


async def applied_version(conn: asyncpg.Connection) -> int:
    """Cea mai mare migrare ÎNREGISTRATĂ (0 dacă tabelul nu există). Numărul pe care readiness-ul
    îl compară cu intervalul tolerat de imagine (`src/ops/build_info.py`)."""
    try:
        value = await conn.fetchval("select coalesce(max(version::int), 0) from schema_migrations")
    except asyncpg.UndefinedTableError:
        return 0
    return int(value or 0)


async def migration_state(conn: asyncpg.Connection, docs_dir: Path = DOCS_DIR) -> dict:
    """Starea schemei ca DATE, nu ca text de consolă — preflight-ul de release o consumă.

    `bundled` = ce conține artefactul, `applied` = ce e în DB. Diferența dintre ele e chiar
    întrebarea „pot promova imaginea asta?", deci merită să fie un număr, nu o impresie.
    """
    pend = await pending_migrations(conn, docs_dir)
    drift = await checksum_drift(conn, docs_dir)
    discovered = discover_migrations(docs_dir)
    return {
        "applied": await applied_version(conn),
        "bundled": max((int(m.version) for m in discovered), default=0),
        "pending": [m.version for m in pend],
        "drift": [m.filename for m in drift],
        "ok": not pend and not drift,
    }


async def assert_migrations_current(pool: asyncpg.Pool, docs_dir: Path = DOCS_DIR) -> None:
    """Poarta de boot (P6): refuză pornirea workerului dacă există migrări neaplicate.
    Workerul NU pornește tăcut peste o schemă incompletă (regresia 010/012 care crăpa
    primul mesaj al fiecărui client nou)."""
    async with pool.acquire() as conn:
        pend = await pending_migrations(conn, docs_dir)
    if pend:
        versions = ", ".join(m.version for m in pend)
        raise RuntimeError(
            f"Migrări neaplicate: {versions}. Rulează `python scripts/migrate.py` "
            "(sau `--baseline` pe o DB existentă) înainte de boot. (NX-123, P6)"
        )


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _env_or_file(name: str) -> str | None:
    """`NAME` sau `NAME_FILE` (fișier montat read-only), ca în `src/config.py`.

    Scriptul ăsta nu trece prin `Settings` (rulează și fără app-ul întreg, în jobul de migrare),
    deci suportul de fișier se repetă aici — dar cu ACELAȘI contract, inclusiv refuzul când sunt
    setate ambele: un credential de DDL livrat pe două căi e exact locul unde nu vrei să ghicești
    care câștigă.
    """
    direct = os.environ.get(name)
    path = os.environ.get(f"{name}_FILE")
    if direct and path:
        raise RuntimeError(f"{name} și {name}_FILE sunt AMBELE setate — livrare ambiguă (NX-248)")
    if path:
        return Path(path).read_text(encoding="utf-8").strip()
    return direct


def _dsn(*, write: bool) -> str:
    """DSN-ul potrivit OPERAȚIEI (NX-248).

    `write=True` (apply/baseline) cere `DATABASE_URL_MIGRATION` — credentialul de DDL, care există
    doar în jobul de migrare. `write=False` (`--check`) se mulțumește cu DSN-ul de control plane:
    poarta de boot a workerului are nevoie doar să CITEASCĂ `schema_migrations`, deci n-are de ce
    să ceară un credential care poate face DROP.

    În dev (`ENV` ≠ `prod`) căderea pe DSN-ul de control plane e permisă cu avertisment: altfel
    fiecare mașină de dezvoltare ar avea nevoie de două DSN-uri ca să ruleze o migrare locală. În
    prod nu: acolo distincția e chiar controlul.
    """
    if write:
        migration = _env_or_file("DATABASE_URL_MIGRATION")
        if migration:
            return migration
        if (os.environ.get("ENV") or "").lower() in ("prod", "production"):
            raise RuntimeError(
                "DATABASE_URL_MIGRATION lipsește. În prod, aplicarea migrărilor rulează EXCLUSIV "
                "în jobul dedicat, cu credentialul de DDL — serviciile de runtime nu îl au "
                "(NX-248)."
            )
        print(
            "ATENȚIE: DATABASE_URL_MIGRATION lipsește; folosesc DSN-ul de control plane "
            "(permis doar în afara prod).",
            file=sys.stderr,
        )
    dsn = _env_or_file("SUPABASE_DB_URL") or _env_or_file("DATABASE_URL")
    if not dsn:
        raise RuntimeError("SUPABASE_DB_URL (sau DATABASE_URL) lipsește — nu pot rula migrările.")
    return dsn


#: Hosturi pentru care TLS nu are ce proteja: traficul nu părăsește mașina. NX-247 are nevoie de
#: asta ca migrările să poată rula pe Postgres-ul EFEMER din `docker-compose.stage1-e2e.yml`
#: (fără certificat) — altfel runnerul canonic, singurul permis, n-ar putea aplica schema local, iar
#: runbookul ar fi copy/paste doar pe hârtie. Poarta e pe HOST, deci se aplică singură: un DSN
#: remote nu pierde SSL-ul din greșeală. Cealaltă cale (`sslmode=disable`, NX-248) e pentru
#: restore-ul izolat, care nu e pe loopback — dar acolo dezactivarea e SCRISĂ, nu dedusă.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def _connect_kwargs(dsn: str) -> dict:
    """IPv4 + SSL fără verificare de hostname pentru pooler-ul Supabase (cf. apply_012).

    Două excepții de la SSL, amândouă pentru medii unde TLS n-are ce proteja:
      • host de LOOPBACK — traficul nu părăsește mașina, iar Postgres-ul efemer din
        `docker-compose.stage1-e2e.yml` (NX-247) n-are certificat;
      • `sslmode=disable` EXPLICIT în DSN (NX-248) — restore-ul IZOLAT din DR, care rulează pe alt
        host decât producția și tot n-are TLS.

    Înainte, SSL-ul era necondiționat, ceea ce făcea runnerul inutilizabil exact acolo unde avem cel
    mai mult nevoie de el: un instrument de migrare care merge doar pe producție nu poate fi
    exersat înainte de producție. Default-ul rămâne SSL: absența ambelor semnale înseamnă „mediu
    real", nu „fără criptare" — un DSN remote nu pierde SSL-ul din greșeală, ci doar dacă cineva
    scrie `sslmode=disable` cu mâna lui.
    """
    p = urlparse(dsn)
    base = {
        "port": p.port or 5432,
        "user": unquote(p.username),
        "password": unquote(p.password),
        "database": (p.path or "/postgres").lstrip("/"),
    }
    # Loopback ÎNAINTE de rezolvare: `::1` n-are răspuns în AF_INET, deci `getaddrinfo` ar crăpa.
    if p.hostname in _LOOPBACK_HOSTS:
        return {**base, "host": p.hostname, "ssl": False}
    ip = socket.getaddrinfo(p.hostname, p.port or 5432, socket.AF_INET, socket.SOCK_STREAM)[0][4][0]
    if parse_qs(p.query).get("sslmode", [""])[0].lower() == "disable":
        return {**base, "host": ip, "ssl": False}
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return {**base, "host": ip, "ssl": ctx}


async def _connect(*, write: bool) -> asyncpg.Connection:
    return await asyncpg.connect(**_connect_kwargs(_dsn(write=write)), statement_cache_size=0)


async def _amain(args: argparse.Namespace) -> int:
    # `--check` și `--dry-run` NU scriu → citesc cu DSN-ul de control plane. Restul cere DDL.
    write = not (args.check or args.dry_run)
    conn = await _connect(write=write)
    try:
        if args.check:
            state = await migration_state(conn)
            if args.json:
                print(json.dumps(state, separators=(",", ":"), sort_keys=True))
            for name in state["drift"]:
                print(f"DRIFT checksum: {name} editat după aplicare", file=sys.stderr)
            if state["pending"]:
                print("PENDING: " + ", ".join(state["pending"]), file=sys.stderr)
            if not state["ok"]:
                return EXIT_PENDING
            if not args.json:
                print(f"migrări la zi (aplicat={state['applied']:03d}, zero pending)")
            return EXIT_OK

        if args.dry_run:
            done = await apply_pending(conn, dry_run=True)
            print(f"ar aplica: {len(done)} migrări: {', '.join(done) or '—'}")
            return EXIT_OK

        # De aici în jos SCRIEM → un singur migrator, verificat (vezi `acquire_migration_lock`).
        try:
            got = await acquire_migration_lock(conn)
        except MigrationLockUnsafe as e:
            print(f"REFUZ: {e}", file=sys.stderr)
            return EXIT_UNSAFE
        if not got:
            print(
                "alt job de migrare ține lock-ul — ies fără să scriu nimic (NX-248)",
                file=sys.stderr,
            )
            return EXIT_LOCKED
        try:
            if args.baseline:
                marked = await baseline(conn)
                print(
                    f"baseline: {len(marked)} migrări marcate aplicate: {', '.join(marked) or '—'}"
                )
                return EXIT_OK
            # Scrie în `schema_migrations`, exact tabelul pe care îl scrie și `apply_pending` →
            # sub ACELAȘI lock: altfel un job care marchează 005 și unul care aplică 006+ ar putea
            # decide în paralel ce e „aplicat".
            if args.mark_applied:
                versions = [v.strip() for v in args.mark_applied.split(",") if v.strip()]
                marked = await mark_applied(conn, versions)
                print(f"marcate aplicate: {', '.join(marked) or '— (erau deja)'}")
                return EXIT_OK
            started = time.monotonic()
            done = await apply_pending(conn)
            elapsed_ms = int((time.monotonic() - started) * 1000)
            print(f"aplicat: {len(done)} migrări în {elapsed_ms}ms: {', '.join(done) or '—'}")
            if args.json:
                state = await migration_state(conn)
                print(
                    json.dumps(
                        {**state, "applied_now": done, "duration_ms": elapsed_ms},
                        separators=(",", ":"),
                        sort_keys=True,
                    )
                )
            return EXIT_OK
        finally:
            await release_migration_lock(conn)
    finally:
        await conn.close()


def main() -> None:
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass
    ap = argparse.ArgumentParser(description="Runner migrări Nativx (NX-123)")
    ap.add_argument("--check", action="store_true", help="cod ≠0 dacă există pending / drift")
    ap.add_argument("--dry-run", action="store_true", help="listează pending fără a aplica")
    ap.add_argument(
        "--baseline", action="store_true", help="marchează tot ca aplicat (adoptare DB existentă)"
    )
    ap.add_argument(
        "--mark-applied",
        metavar="VERSIUNI",
        help="marchează versiuni punctuale ca aplicate, cu checksum real (ex. 005) — pentru "
        "migrările psql-only, aplicate în afara runnerului",
    )
    ap.add_argument("--json", action="store_true", help="starea schemei ca JSON (preflight/CI)")
    args = ap.parse_args()
    raise SystemExit(asyncio.run(_amain(args)))


if __name__ == "__main__":
    main()
