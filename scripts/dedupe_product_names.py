"""Disambiguate duplicate product display names on the pilot tenant (NX-155).

The demo catalog was seeded with a low-cardinality name template
(`{brand} {line} {type} pentru {concern}`), so ~141 distinct SKUs (distinct slug,
EAN, price, category) share an identical display name. Three cards literally named
"Mira Atelier Clear Toner pentru hidratare" in one list reads as broken.

Fix: for each colliding product, append its hero ingredient (from
`attributes.key_ingredients`) — real, natural for cosmetics, no numeric suffixes (we
must NOT regress the earlier `…348` cleanup). Greedy assignment guarantees the new
name is unique both inside the collision group and globally. Only `name` changes;
`slug` and `product_url` stay intact, so links and embeddings are untouched.

Idempotent: after a successful run names are unique, so a re-run finds no collisions
and does nothing. Writes ONLY products.name, filtered on business_id (P7).

Usage:
    python scripts/dedupe_product_names.py --business <business_id> --dry-run
    python scripts/dedupe_product_names.py --business <business_id>
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import ssl
import sys
from urllib.parse import unquote, urlparse

import asyncpg

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None


DEMO_BIZ = "6098812a-50fc-44bd-a1ba-bc77e6399158"

# Words we do not want as a differentiator (too generic to disambiguate nicely).
_SKIP_INGREDIENTS = {"apa", "apă", "aqua", "parfum", "glicerina", "glicerină"}


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


_SMALL_WORDS = {"de", "cu", "și", "si", "a", "al", "ale", "din", "la", "pe", "d'"}


def _titlecase_ro(token: str) -> str:
    """Title-case a RO ingredient phrase, keeping connectors lowercase:
    'ulei de jojoba' -> 'Ulei de Jojoba', 'acid hialuronic' -> 'Acid Hialuronic'."""
    words = token.split()
    out: list[str] = []
    for idx, w in enumerate(words):
        if idx > 0 and w.lower() in _SMALL_WORDS:
            out.append(w.lower())
        else:
            out.append(w[:1].upper() + w[1:] if w else w)
    return " ".join(out)


def _ingredients(attrs: dict) -> list[str]:
    raw = attrs.get("key_ingredients") or []
    out: list[str] = []
    for ing in raw:
        s = (ing or "").strip()
        if s and s.lower() not in _SKIP_INGREDIENTS:
            out.append(s)
    return out


def _candidates(attrs: dict, category: str | None) -> list[str]:
    """Ordered differentiator suffixes (connector 'cu' for ingredients). Ingredient-first,
    then ingredient pairs, then category as a last resort — all real, no numbers."""
    ings = _ingredients(attrs)
    cands = [f"cu {_titlecase_ro(i)}" for i in ings]
    color = (attrs.get("Culoare") or "").strip()
    if color:
        cands.append(f"— {color}")
    # ingredient pairs widen cardinality when single ingredients collide
    for i in range(len(ings)):
        for j in range(i + 1, len(ings)):
            cands.append(f"cu {_titlecase_ro(ings[i])} și {_titlecase_ro(ings[j])}")
    if category:
        cands.append(f"— {category}")
    return cands


def _attrs(record: asyncpg.Record) -> dict:
    attrs = record["attributes"]
    if isinstance(attrs, str):
        try:
            return json.loads(attrs)
        except json.JSONDecodeError:
            return {}
    return attrs or {}


def _plan(rows: list[asyncpg.Record]) -> tuple[list[tuple[str, str, str]], list[dict]]:
    """Return (renames, residual). renames = (id, old_name, new_name).

    Exactly one member per collision group may keep the base name (it becomes unique once
    the others move). We rename the members that CAN be differentiated first and leave the
    candidate-poorest one on the base, so `residual` only holds genuinely-identical rows
    (2+ with no free differentiator) — inventing a fake difference there would be dishonest."""
    by_name: dict[str, list[asyncpg.Record]] = {}
    for r in rows:
        by_name.setdefault((r["name"] or "").strip().lower(), []).append(r)

    # Singletons already own their names; renamed rows claim new ones. A base name is free
    # for one group member to keep once no one else holds it.
    assigned = {k for k, v in by_name.items() if len(v) == 1}
    renames: list[tuple[str, str, str]] = []
    residual: list[dict] = []

    for key, members in by_name.items():
        if len(members) < 2:
            continue
        # richest-differentiator first → the poorest row is left to keep the base name
        ranked = sorted(
            members,
            key=lambda r: (-len(_candidates(_attrs(r), r["category"])), r["id"]),
        )
        base_kept = False
        for r in ranked:
            base = (r["name"] or "").strip()
            placed = False
            for suffix in _candidates(_attrs(r), r["category"]):
                candidate = f"{base} {suffix}"
                if candidate.strip().lower() not in assigned:
                    assigned.add(candidate.strip().lower())
                    renames.append((r["id"], base, candidate))
                    placed = True
                    break
            if placed:
                continue
            if not base_kept and base.lower() not in assigned:
                assigned.add(base.lower())  # keep original name; now unique
                base_kept = True
            else:
                residual.append({"id": r["id"], "name": base})
    return renames, residual


async def _run(conn: asyncpg.Connection, business_id: str, dry_run: bool) -> None:
    rows = await conn.fetch(
        """
        select p.id::text as id, p.name, p.attributes, c.name as category
        from products p
        left join categories c on c.id = p.primary_category_id
        where p.business_id = $1 and p.status = 'active'
        """,
        business_id,
    )
    renames, residual = _plan(rows)
    print(f"business={business_id}  active={len(rows)}  renames={len(renames)}  "
          f"residual={len(residual)}  dry_run={dry_run}")
    for _id, old, new in renames[:20]:
        print(f"  '{old}'\n    -> '{new}'")
    if len(renames) > 20:
        print(f"  ... (+{len(renames) - 20} more)")
    for u in residual:
        print(f"  RESIDUAL (identical seed row, no real differentiator): '{u['name']}' ({u['id']})")

    if dry_run or not renames:
        print("dry-run or nothing to do: no writes")
        return

    async with conn.transaction():
        for _id, _old, new in renames:
            await conn.execute(
                "update products set name = $2, updated_at = now() "
                "where id = $1 and business_id = $3",
                _id,
                new,
                business_id,
            )
    remaining = await conn.fetchval(
        """
        select count(*) from (
            select 1 from products where business_id = $1 and status = 'active'
            group by lower(trim(name)) having count(*) > 1
        ) d
        """,
        business_id,
    )
    print(f"done: renamed {len(renames)} products; duplicate_name_groups now = {remaining}")


def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Disambiguate duplicate product names (NX-155)")
    ap.add_argument(
        "--business",
        default=os.environ.get("PILOT_BUSINESS_ID", DEMO_BIZ),
        help="business_id (default: PILOT_BUSINESS_ID or demo tenant)",
    )
    ap.add_argument("--dry-run", action="store_true", help="print planned renames, no writes")
    return ap.parse_args()


async def _main() -> int:
    args = _parse_args()
    conn = await _connect()
    try:
        await _run(conn, args.business, args.dry_run)
    finally:
        await conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
