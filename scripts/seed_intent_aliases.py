"""Seed approved intent_aliases for the pilot tenant (NX-155).

The alias stage (src/worker/stages/alias.py, NX-73) is an EXACT-match free layer that
runs before cache + triage: a normalized phrase hit routes the turn with zero LLM/token.
`phrase_norm` MUST be produced by the same `canonicalize()` the lookup uses, otherwise the
exact match silently misses. We seed only generic, universally-correct phrasings:

  • route=handoff  — "vreau sa vorbesc cu un om" and variants (deterministic escalation)
  • route=order    — "unde e comanda mea" and variants (deterministic order intent)

These are safe for any ecommerce tenant: they short-circuit a nano triage call to the same
route triage would have picked. Category/FAQ aliases are intentionally NOT seeded here — they
depend on tenant-specific slugs/FAQ ids and belong to a data-curation step, not this bootstrap.

Idempotent: upsert on (business_id, phrase_norm, target_kind). Re-running is a no-op.
Writes ONLY intent_aliases, filtered on business_id (P7). Read the phrasings below before
running against a real tenant.

Usage:
    python scripts/seed_intent_aliases.py --business <business_id>
    python scripts/seed_intent_aliases.py --business <business_id> --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import os
import socket
import ssl
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

import asyncpg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.cache.canonical import canonicalize  # noqa: E402

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None


DEMO_BIZ = "6098812a-50fc-44bd-a1ba-bc77e6399158"

# (route, [phrasings]) — generic RO intents. Diacritics/punctuation are irrelevant:
# canonicalize() lowercases + strips them, so these match "unde e comanda mea?" too.
ROUTE_ALIASES: dict[str, tuple[str, ...]] = {
    "handoff": (
        "vreau sa vorbesc cu un om",
        "vreau sa vorbesc cu cineva",
        "vreau sa vorbesc cu un agent",
        "vreau un operator",
        "pot vorbi cu un om",
        "vreau sa discut cu un consultant",
    ),
    "order": (
        "unde e comanda mea",
        "unde este comanda mea",
        "status comanda",
        "statusul comenzii",
        "unde e coletul meu",
        "vreau sa urmaresc comanda",
        "urmarire comanda",
    ),
}


if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass


def _dsn() -> str | None:
    if load_dotenv:
        load_dotenv()
    return os.environ.get("SUPABASE_DB_URL") or os.environ.get("DATABASE_URL")


def _connect_kwargs(dsn: str) -> dict:
    if sys.platform != "win32":
        return {"dsn": dsn, "statement_cache_size": 0}
    parsed = urlparse(dsn)
    ipv4 = socket.getaddrinfo(
        parsed.hostname, parsed.port or 5432, socket.AF_INET, socket.SOCK_STREAM
    )[0][4][0]
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return {
        "host": ipv4,
        "port": parsed.port or 5432,
        "user": unquote(parsed.username or ""),
        "password": unquote(parsed.password or ""),
        "database": (parsed.path or "/postgres").lstrip("/"),
        "ssl": ctx,
        "statement_cache_size": 0,
    }


async def _connect() -> asyncpg.Connection:
    dsn = _dsn()
    if not dsn:
        raise SystemExit("SUPABASE_DB_URL or DATABASE_URL is missing")
    return await asyncpg.connect(**_connect_kwargs(dsn))


def _rows(business_id: str) -> list[tuple[str, str, str]]:
    """(phrase_norm, target_kind='route', target_value=route). Dedupe on phrase_norm+kind."""
    seen: set[tuple[str, str]] = set()
    rows: list[tuple[str, str, str]] = []
    for route, phrases in ROUTE_ALIASES.items():
        for phrase in phrases:
            norm, _ = canonicalize(phrase)
            if not norm:
                continue
            key = (norm, "route")
            if key in seen:
                continue
            seen.add(key)
            rows.append((norm, "route", route))
    return rows


async def _seed(conn: asyncpg.Connection, business_id: str, dry_run: bool) -> None:
    rows = _rows(business_id)
    print(f"business={business_id}  candidate aliases={len(rows)}  dry_run={dry_run}")
    for norm, kind, value in rows:
        print(f"  [{kind:8}] {value:8} <- '{norm}'")
    if dry_run:
        print("dry-run: no writes")
        return

    inserted = 0
    for norm, kind, value in rows:
        status = await conn.execute(
            """
            insert into intent_aliases
                (business_id, phrase_norm, target_kind, target_value, source, status)
            values ($1, $2, $3, $4, 'manual', 'approved')
            on conflict (business_id, phrase_norm, target_kind)
            do update set target_value = excluded.target_value,
                          status = 'approved',
                          source = 'manual'
            """,
            business_id,
            norm,
            kind,
            value,
        )
        inserted += 1
        print(f"  {status}  {kind}/{value}  '{norm}'")

    total = await conn.fetchval(
        "select count(*) from intent_aliases where business_id = $1 and status = 'approved'",
        business_id,
    )
    print(f"done: upserted {inserted}, approved aliases now = {total}")


def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Seed approved intent_aliases (NX-155)")
    ap.add_argument(
        "--business",
        default=os.environ.get("PILOT_BUSINESS_ID", DEMO_BIZ),
        help="business_id to seed (default: PILOT_BUSINESS_ID or demo tenant)",
    )
    ap.add_argument("--dry-run", action="store_true", help="print planned aliases, no writes")
    return ap.parse_args()


async def _main() -> int:
    args = _parse_args()
    conn = await _connect()
    try:
        await _seed(conn, args.business, args.dry_run)
    finally:
        await conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
