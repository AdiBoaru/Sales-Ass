"""NX-248 — verifică un RESTORE. Read-only, pe un DB izolat, cu refuz explicit către producție.

Un backup nu e o copie de siguranță până când cineva l-a restaurat și a verificat că e util. Între
„jobul de backup a ieșit cu 0" și „pot servi clienți din datele astea" încap: o schemă incompletă,
grant-uri lipsă (rolul de runtime nu mai poate scrie), RLS dezactivat (izolarea între tenanți
dispare tăcut) și rânduri de ledger fără rezultatul lor.

Scriptul răspunde la cinci întrebări, în ordinea în care contează:

  1. **Migrări** — schema restaurată e la zi? (altfel imaginea curentă nici nu pornește)
  2. **Grant-uri** — `bot_runtime` poate citi/scrie ce trebuie și NU poate face DDL?
  3. **RLS** — politicile sunt ACTIVE pe tabelele tenant-scoped? Un restore care pierde RLS arată
     perfect până la primul query fără `business_id`.
  4. **Izolare cross-tenant** — cu `app.business_id` setat pe tenantul A, un `select` vede DOAR
     tenantul A? Ăsta e testul care contează, fiindcă e chiar promisiunea către clienți.
  5. **Recuperabilitate** — turele `accepted` pot fi reluate și cele terminale au rezultat
     persistat (adică un accept durabil chiar a supraviețuit).

RPO/RTO se CALCULEAZĂ, nu se declară: `--backup-timestamp` (când s-a făcut backupul) și momentul
în care verificarea trece dau fereastra reală. Fără ele, raportul spune `UNVERIFIED` — iar
`UNVERIFIED` blochează NX-249, exact ca verdictul `NOT-READY` din NX-238.

**Nu șterge nimic și nu restaurează nimic.** Crearea și distrugerea mediului de drill sunt operații
umane, separate și aprobate — un script care poate șterge un mediu poate șterge mediul greșit.

Uz:
    DR_VERIFY_DSN=postgresql://…/restore_2026_08_17 python scripts/dr/restore_verify.py \\
        --business-id <uuid> --backup-timestamp 2026-08-17T02:00:00Z --out reports/nx248/dr.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import asyncpg  # noqa: E402

from scripts.migrate import _connect_kwargs, migration_state  # noqa: E402

#: Tabele care TREBUIE să aibă RLS activ după restore. Lista e scurtă și aleasă: fiecare conține
#: date de client care, fără RLS, ar fi vizibile cross-tenant la primul query fără filtru.
RLS_REQUIRED = (
    "conversations",
    "messages",
    "contacts",
    "web_turns",
    "conversation_carts",
    "web_feedback",
)

#: Ce trebuie să poată face rolul de runtime pe ledger — și ce NU trebuie să poată face nicăieri.
RUNTIME_ROLE = "bot_runtime"


class RefusedError(RuntimeError):
    """Ținta nu e un mediu de drill. Nu există flag care să treacă peste asta."""


def _guard_target(dsn: str) -> None:
    """Refuză orice DSN care seamănă cu producția.

    Poarta e pe NUME, nu pe convingerea operatorului: numele bazei/hostului trebuie să conțină un
    marker de drill. Un script de verificare care poate rula pe producție ajunge, mai devreme sau
    mai târziu, să ruleze pe producție.
    """
    parsed = urlparse(dsn)
    haystack = f"{parsed.hostname or ''}/{(parsed.path or '').lstrip('/')}".lower()
    if not any(marker in haystack for marker in ("restore", "drill", "verify", "staging", "test")):
        raise RefusedError(
            f"REFUZ: {haystack!r} nu arată a mediu de drill. Ținta trebuie să conțină "
            "'restore'/'drill'/'verify'/'staging'/'test' în host sau nume de bază "
            "(docs/DISASTER-RECOVERY.md §Mediu de drill)."
        )


async def _check_migrations(conn: asyncpg.Connection) -> dict:
    state = await migration_state(conn)
    return {
        "ok": state["ok"],
        "applied": state["applied"],
        "pending": state["pending"],
        "drift": state["drift"],
    }


async def _check_grants(conn: asyncpg.Connection) -> dict:
    role_exists = await conn.fetchval(
        "select exists(select 1 from pg_roles where rolname = $1)", RUNTIME_ROLE
    )
    if not role_exists:
        return {"ok": False, "reason": "rolul de runtime nu există după restore"}
    can = {}
    for table, privilege in (
        ("web_turns", "select"),
        ("web_turns", "insert"),
        ("web_turns", "update"),
        ("conversations", "select"),
        ("messages", "insert"),
        ("products", "select"),
    ):
        can[f"{table}.{privilege}"] = bool(
            await conn.fetchval(
                "select has_table_privilege($1, $2, $3)", RUNTIME_ROLE, f"public.{table}", privilege
            )
        )
    # Rolul de runtime NU trebuie să poată face DDL. `bypassrls` ar fi și mai grav: ar anula
    # izolarea în tăcere, fără să pice niciun query.
    attrs = await conn.fetchrow(
        "select rolsuper, rolbypassrls, rolcreatedb from pg_roles where rolname = $1", RUNTIME_ROLE
    )
    privileged = bool(attrs["rolsuper"] or attrs["rolbypassrls"] or attrs["rolcreatedb"])
    return {
        "ok": all(can.values()) and not privileged,
        "privileges": can,
        "over_privileged": privileged,
    }


async def _check_rls(conn: asyncpg.Connection) -> dict:
    rows = await conn.fetch(
        "select relname, relrowsecurity, relforcerowsecurity from pg_class c "
        "join pg_namespace n on n.oid = c.relnamespace "
        "where n.nspname = 'public' and relname = any($1::text[])",
        list(RLS_REQUIRED),
    )
    found = {r["relname"]: bool(r["relrowsecurity"]) for r in rows}
    missing = [t for t in RLS_REQUIRED if t not in found]
    disabled = [t for t, on in found.items() if not on]
    return {"ok": not missing and not disabled, "missing": missing, "rls_disabled": disabled}


async def _check_isolation(conn: asyncpg.Connection, business_id: str | None) -> dict:
    """Cu `app.business_id` setat pe A, se văd DOAR rândurile lui A.

    Rulăm ca `bot_runtime` (SET ROLE): ca superuser, RLS e ocolit, deci testul ar trece mereu —
    ceea ce ar face din el cel mai periculos test verde din suită.
    """
    if not business_id:
        return {"ok": None, "reason": "fără --business-id: izolarea nu a fost testată"}
    async with conn.transaction():
        await conn.execute(f"set local role {RUNTIME_ROLE}")
        await conn.execute("select set_config('app.business_id', $1, true)", business_id)
        visible = await conn.fetchval("select count(*) from conversations")
        foreign = await conn.fetchval(
            "select count(*) from conversations where business_id <> $1::uuid", business_id
        )
    return {
        "ok": int(foreign or 0) == 0,
        "visible_rows": int(visible or 0),
        "foreign_rows_visible": int(foreign or 0),
    }


async def _check_ledger(conn: asyncpg.Connection) -> dict:
    """Ledgerul e recuperabil: turele terminale au rezultat, cele `accepted` pot fi reluate."""
    exists = await conn.fetchval("select to_regclass('public.web_turns') is not null")
    if not exists:
        return {"ok": False, "reason": "ledgerul web_turns lipsește după restore"}
    row = await conn.fetchrow(
        "select count(*) filter (where status = 'accepted') as reclaimable, "
        "count(*) filter (where status in ('completed','failed','cancelled')) as terminal, "
        "count(*) filter (where status = 'completed' and response_json is null) as mute_terminal "
        "from web_turns"
    )
    # Un `completed` fără rezultat persistat ar însemna că un client a primit un răspuns care nu
    # supraviețuiește restaurării — exact ce contrazice P6 și acceptul durabil NX-232.
    return {
        "ok": int(row["mute_terminal"] or 0) == 0,
        "reclaimable": int(row["reclaimable"] or 0),
        "terminal": int(row["terminal"] or 0),
        "terminal_fara_rezultat": int(row["mute_terminal"] or 0),
    }


async def verify(dsn: str, business_id: str | None, backup_ts: str | None) -> dict:
    started = datetime.now(UTC)
    conn = await asyncpg.connect(**_connect_kwargs(dsn), statement_cache_size=0)
    try:
        checks = {
            "migrations": await _check_migrations(conn),
            "grants": await _check_grants(conn),
            "rls": await _check_rls(conn),
            "isolation": await _check_isolation(conn, business_id),
            "ledger": await _check_ledger(conn),
        }
    finally:
        await conn.close()
    finished = datetime.now(UTC)

    failed = [name for name, result in checks.items() if result.get("ok") is False]
    skipped = [name for name, result in checks.items() if result.get("ok") is None]

    # RPO/RTO: calculate din timestampuri REALE sau `UNVERIFIED`. Nu există a treia variantă —
    # o țintă „probabil atinsă" e cel mai scump fel de necunoscut.
    rpo, rto, verdict = None, None, "UNVERIFIED"
    if backup_ts:
        try:
            backup_at = datetime.fromisoformat(backup_ts.replace("Z", "+00:00"))
        except ValueError:
            backup_at = None
        if backup_at is not None:
            rpo = round((started - backup_at).total_seconds() / 60.0, 1)
            rto = round((finished - started).total_seconds() / 60.0, 1)
            if not failed and not skipped:
                verdict = "PASS"
            elif failed:
                verdict = "FAIL"
    elif failed:
        verdict = "FAIL"

    return {
        "schema_version": "dr-restore-verify.v1",
        "verdict": verdict,
        "verified_at": finished.isoformat(timespec="seconds"),
        "backup_timestamp": backup_ts or None,
        "rpo_minutes": rpo,
        "rto_minutes": rto,
        "targets": {"rpo_minutes": 5, "rto_minutes": 60},
        "failed_checks": failed,
        "skipped_checks": skipped,
        "checks": checks,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Verifică un restore izolat (NX-248)")
    ap.add_argument("--dsn", default=os.environ.get("DR_VERIFY_DSN", ""))
    ap.add_argument("--business-id", default="", help="tenant pentru testul de izolare")
    ap.add_argument("--backup-timestamp", default="", help="ISO-8601 UTC; fără el → UNVERIFIED")
    ap.add_argument("--out", default="")
    args = ap.parse_args(argv)

    if not args.dsn:
        print("DR_VERIFY_DSN (sau --dsn) lipsește", file=sys.stderr)
        return 2
    try:
        _guard_target(args.dsn)
    except RefusedError as e:
        print(str(e), file=sys.stderr)
        return 2

    report = asyncio.run(verify(args.dsn, args.business_id or None, args.backup_timestamp or None))
    text = json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    print(text)
    print(f"DR RESTORE: {report['verdict']}", file=sys.stderr)
    # `UNVERIFIED` NU e succes: blochează NX-249 (cod 1), la fel ca un FAIL.
    return 0 if report["verdict"] == "PASS" else 1


if __name__ == "__main__":
    from src.ops.cli import enable_utf8_stdout

    enable_utf8_stdout()

    raise SystemExit(main())
