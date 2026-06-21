"""Runner ordonat de migrări + poartă de boot (NX-123).

Înlocuiește scripturile one-off `apply_0NN.py` (fire-and-forget, fără stare) cu:
  • un singur entrypoint care descoperă `docs/0NN_*.sql`, le aplică ORDONAT NUMERIC
    (prefix, nu lexicografic — 010 > 009) și înregistrează fiecare în
    `schema_migrations` (o tranzacție per fișier, fail-fast);
  • idempotență: o migrare deja înregistrată (checksum potrivit / 'legacy') se sare;
  • `--check`: cod ≠0 dacă există migrări pending sau drift de checksum — folosit de
    poarta de boot a workerului ȘI ca pas de CI;
  • `--dry-run`: listează pending fără a aplica;
  • `--baseline`: ADOPTARE pe o DB de PROD existentă (003–013 deja aplicate manual) —
    marchează tot ce e pe disc ca aplicat ('legacy') FĂRĂ a rula SQL-ul.

Folosește DSN privilegiat (control plane, ca `apply_*`), NU `bot_runtime` — migrările
fac DDL/GRANT. Reutilizează handling-ul IPv4 + SSL pentru pooler-ul Supabase
(cf. memory „DB URL password encoding").

Importabil: `assert_migrations_current(pool)` e poarta de boot apelată din
`src/worker/consumer.py` înainte de XREADGROUP (P6 — nu boot peste schemă incompletă).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import re
import socket
import ssl
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

import asyncpg

DOCS_DIR = Path(__file__).resolve().parent.parent / "docs"
# numele fișierelor de migrare: <prefix numeric>_<slug>.sql, ex. 014_schema_migrations.sql
_MIGRATION_RE = re.compile(r"^(\d+)_.*\.sql$")

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


def _dsn() -> str:
    dsn = os.environ.get("SUPABASE_DB_URL") or os.environ.get("DATABASE_URL")
    if not dsn:
        raise RuntimeError("SUPABASE_DB_URL (sau DATABASE_URL) lipsește — nu pot rula migrările.")
    return dsn


def _connect_kwargs(dsn: str) -> dict:
    """IPv4 + SSL fără verificare de hostname pentru pooler-ul Supabase (cf. apply_012)."""
    p = urlparse(dsn)
    ip = socket.getaddrinfo(p.hostname, p.port or 5432, socket.AF_INET, socket.SOCK_STREAM)[0][4][0]
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return {
        "host": ip,
        "port": p.port or 5432,
        "user": unquote(p.username),
        "password": unquote(p.password),
        "database": (p.path or "/postgres").lstrip("/"),
        "ssl": ctx,
    }


async def _connect() -> asyncpg.Connection:
    return await asyncpg.connect(**_connect_kwargs(_dsn()), statement_cache_size=0)


async def _amain(args: argparse.Namespace) -> int:
    conn = await _connect()
    try:
        if args.baseline:
            marked = await baseline(conn)
            print(f"baseline: {len(marked)} migrări marcate aplicate: {', '.join(marked) or '—'}")
            return 0
        if args.check:
            pend = await pending_migrations(conn)
            drift = await checksum_drift(conn)
            for m in drift:
                print(f"DRIFT checksum: {m.filename} editat după aplicare", file=sys.stderr)
            if pend:
                print("PENDING: " + ", ".join(m.version for m in pend), file=sys.stderr)
                return 1
            if drift:
                return 1  # drift = eroare în poarta CI
            print("migrări la zi (zero pending)")
            return 0
        done = await apply_pending(conn, dry_run=args.dry_run)
        verb = "ar aplica" if args.dry_run else "aplicat"
        print(f"{verb}: {len(done)} migrări: {', '.join(done) or '—'}")
        return 0
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
    args = ap.parse_args()
    raise SystemExit(asyncio.run(_amain(args)))


if __name__ == "__main__":
    main()
