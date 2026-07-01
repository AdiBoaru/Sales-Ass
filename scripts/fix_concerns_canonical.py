"""Aliniază products.attributes.concerns la CHEILE CANONICE (intenția de design, taxonomy.py).

Bug: pe demo, `attributes.concerns` a fost seedat RO („ten uscat") dar `map_concerns` produce
canonical („dry") → operatorul `?|` din filtrul de search nu prinde nimic → filtrul de tip-ten se
relaxează silențios (și scorul de concern din fusion.py nu se aliniază). Design-ul (taxonomy.py)
cere CANONICAL în DB. Convertim termenii CUNOSCUȚI (din concern_map) RO→canonical; cei necunoscuți
(uz zilnic/calmare/...) rămân ca atare (afișare). Fațeta de comparație „Potrivit pentru" re-mapează
canonical→RO prin value_labels (beauty_salon.json), deci afișarea rămâne în română.

Idempotent (sursa = backup-ul `_concerns_ro` dacă există), reversibil (`--revert`).

    python scripts/fix_concerns_canonical.py            # DRY-RUN
    python scripts/fix_concerns_canonical.py --apply
    python scripts/fix_concerns_canonical.py --revert
"""

import argparse
import asyncio
import json
import os
import socket
import ssl
import sys
from urllib.parse import unquote, urlparse

import asyncpg
from dotenv import load_dotenv

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    sys.stdout.reconfigure(encoding="utf-8")

load_dotenv(".env")
DSN = os.environ.get("SUPABASE_DB_URL") or os.environ.get("DATABASE_URL")
BIZ = "6098812a-50fc-44bd-a1ba-bc77e6399158"

sys.path.insert(0, os.getcwd())
from src.domain.loader import load_domain_pack  # noqa: E402
from src.domain.normalize import normalize  # noqa: E402
from src.models import BusinessConfig  # noqa: E402


def _canonical(source: list[str], cmap: dict[str, str]) -> list[str]:
    """Termeni → canonical (dacă cunoscuți), altfel ca atare. Dedup, ordine păstrată."""
    out: list[str] = []
    for c in source:
        if not isinstance(c, str):
            continue
        mapped = cmap.get(normalize(c), c)
        if mapped not in out:
            out.append(mapped)
    return out


async def _connect() -> asyncpg.Connection:
    p = urlparse(DSN)
    ip = socket.getaddrinfo(p.hostname, p.port or 5432, socket.AF_INET, socket.SOCK_STREAM)[0][4][0]
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return await asyncpg.connect(
        host=ip, port=p.port or 5432, user=unquote(p.username), password=unquote(p.password),
        database=(p.path or "/postgres").lstrip("/"), ssl=ctx,
    )


async def revert(conn: asyncpg.Connection) -> None:
    res = await conn.execute(
        "update products set attributes = "
        "jsonb_set(attributes, '{concerns}', attributes->'_concerns_ro') - '_concerns_ro' "
        "where business_id = $1 and attributes ? '_concerns_ro'",
        BIZ,
    )
    print(f"REVERT: {res} (concerns restaurate din _concerns_ro)")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--revert", action="store_true")
    args = ap.parse_args()
    if not DSN:
        sys.exit("SUPABASE_DB_URL lipsește în .env")

    biz = BusinessConfig(id=BIZ, slug="nativex-demo", name="Sole Demo", vertical="beauty")
    pack = load_domain_pack(biz)
    cmap = pack.concern_map if pack else {}
    if not cmap:
        sys.exit("concern_map gol — nu pot converti")

    conn = await _connect()
    try:
        if args.revert:
            await revert(conn)
            return

        rows = await conn.fetch(
            "select p.id::text as id, p.name as name, "
            "coalesce(p.attributes->'_concerns_ro', p.attributes->'concerns') as source, "
            "p.attributes->'concerns' as current "
            "from products p where p.business_id = $1 order by p.name",
            BIZ,
        )
        planned = []
        changed_examples = []
        for r in rows:
            source = json.loads(r["source"]) if r["source"] else []
            current = json.loads(r["current"]) if r["current"] else []
            canonical = _canonical(source, cmap)
            if canonical != current:
                planned.append((r["id"], canonical, source))
                if len(changed_examples) < 12:
                    changed_examples.append((r["name"], current, canonical))

        print(f"=== FIX concerns canonical — {len(rows)} produse ===")
        print(f"de convertit (canonical != actual): {len(planned)}\n")
        for name, cur, can in changed_examples:
            print(f"  „{name[:40]}”  {cur}  →  {can}")

        if not args.apply:
            print("\nDRY-RUN (nimic scris). --apply ca să scrii.")
            return

        async with conn.transaction():
            for pid, canonical, source in planned:
                await conn.execute(
                    "update products set attributes = attributes || "
                    "jsonb_build_object('concerns', $2::jsonb, '_concerns_ro', $3::jsonb) "
                    "where id = $1::uuid and business_id = $4",
                    pid,
                    json.dumps(canonical, ensure_ascii=False),
                    json.dumps(source, ensure_ascii=False),
                    BIZ,
                )
        print(f"\nAPLICAT: {len(planned)} produse. Reversibil: --revert (din _concerns_ro).")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
