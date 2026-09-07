"""Derivă `attributes.product_type` din numele de catalog. Dry-run implicit.

De ce e nevoie de fațeta asta, ce reguli o produc și ce s-a încercat și nu merge: vezi
`src/catalog/product_type.py`. Aici e doar jobul — citește numele, aplică modulul pur, raportează,
și scrie DOAR cu `--apply`.

Scrierea e idempotentă (`jsonb_set` pe aceeași cheie, cu aceeași valoare) și NU șterge nimic: un
produs al cărui tip nu se poate determina rămâne fără cheia `product_type`, fiindcă absența
înseamnă „nu știm" iar `load_vocabulary` ignoră oricum valorile sub prag. O a doua rulare nu
schimbă niciun rând.

    python scripts/derive_product_type.py --business <uuid>            # dry-run + raport
    python scripts/derive_product_type.py --business <uuid> --sample 15
    python scripts/derive_product_type.py --business <uuid> --apply    # scrie
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

from src.catalog.product_type import (  # noqa: E402
    MIN_SUPPORT,
    build_vocabulary,
    classify,
    split_name,
)
from src.db.connection import admin_conn, close_pool, get_pool  # noqa: E402

_SELECT = """
    select id, name, attributes->>'product_type' as current
    from products
    where business_id = $1 and status = 'active'
    order by id
"""

# `jsonb_set` cu `create_if_missing` — scrie doar cheia, nu înlocuiește `attributes`.
_UPDATE = """
    update products
    set attributes = jsonb_set(
            coalesce(attributes, '{}'::jsonb), '{product_type}', to_jsonb($2::text), true
        ),
        updated_at = now()
    where business_id = $3 and id = $1
      and attributes->>'product_type' is distinct from $2
"""


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--business", required=True, help="business_id (uuid)")
    ap.add_argument("--apply", action="store_true", help="scrie în catalog (altfel dry-run)")
    ap.add_argument("--sample", type=int, default=0, help="afișează N exemple de clasificare")
    ap.add_argument("--min-support", type=int, default=MIN_SUPPORT)
    args = ap.parse_args()

    # `admin_conn`, nu `tenant_conn`: job OFFLINE care scrie în catalog, iar `bot_runtime` are
    # SELECT-only pe `products` prin proiectare („NU scriere în catalog din worker"). Pe conexiunea
    # de runtime scrierea moare cu `InsufficientPrivilegeError` — și moare DOAR cu `--apply`,
    # fiindcă dry-run-ul citește; asimetria asta a ascuns defectul până la prima rulare reală.
    # RLS e bypass-at aici, deci izolarea cade integral pe `where business_id = $1`, prezent în
    # fiecare statement al jobului. Același tipar ca `scripts/derive_product_attributes.py`.
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        rows = await conn.fetch(_SELECT, args.business)
        if not rows:
            print("0 produse active — nimic de derivat.")
            return 2

        mapping, counts = build_vocabulary((r["name"] for r in rows), min_support=args.min_support)
        assigned = {r["id"]: classify(r["name"], mapping) for r in rows}
        n_typed = sum(1 for v in assigned.values() if v)
        n_change = sum(1 for r in rows if assigned[r["id"]] != r["current"] and assigned[r["id"]])
        head_lens = [len(split_name(r["name"])[0]) for r in rows]

        print(f"produse active: {len(rows)}")
        print(f"tip determinat: {n_typed} ({100 * n_typed / len(rows):.1f}%)")
        print(f"chei canonice:  {len(counts)} (suport minim {args.min_support})")
        print(
            f"nume: întreg avg={sum(len(r['name']) for r in rows) // len(rows)} car. | "
            f"cap avg={sum(head_lens) // len(head_lens)} car."
        )
        print(f"rânduri de actualizat: {n_change}\n")

        print("--- vocabular ---")
        for t, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
            merged = sorted({s for s, c in mapping.items() if c == t and s != t})
            tail = f"   ← {', '.join(merged[:3])}{'…' if len(merged) > 3 else ''}" if merged else ""
            print(f"  {n:5}  {t}{tail}")

        if args.sample:
            print(f"\n--- {args.sample} exemple ---")
            for r in rows[: args.sample]:
                head, _ = split_name(r["name"])
                print(f"  {str(assigned[r['id']]):28} ← {head[:64]}")

        if not args.apply:
            print("\nDRY-RUN. Nimic scris. Adaugă --apply ca să scrii.")
            return 0

        written = 0
        async with conn.transaction():
            for r in rows:
                if (t := assigned[r["id"]]) is None:
                    continue
                res = await conn.execute(_UPDATE, r["id"], t, args.business)
                written += int(res.rsplit(" ", 1)[-1] or 0)
        print(f"\nSCRIS: {written} rânduri.")

        # Verificare pe DB, nu pe intenție.
        check = await conn.fetch(
            """select attributes->>'product_type' v, count(*) n from products
               where business_id = $1 and status = 'active' and attributes ? 'product_type'
               group by 1 order by n desc limit 5""",
            args.business,
        )
        print(
            "verificat în DB, top 5:",
            json.dumps([(c["v"], c["n"]) for c in check], ensure_ascii=False),
        )
    return 0


async def _run() -> int:
    try:
        return await main()
    finally:
        await close_pool()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_run()))
