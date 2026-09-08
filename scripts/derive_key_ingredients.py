"""Derivă `attributes.key_ingredients` din secțiunea de fișă `key_ingredients`. Dry-run implicit.

De ce: fațeta `key_ingredients` e declarată `searchable` în pachet, dar pe primul catalog real cheia
nu exista în `attributes` (și `product_ingredients.is_key` era `false` peste tot), deci filtrul
„cu niacinamidă" nu putea potrivi nimic — vezi `src/catalog/key_ingredients.py` pentru regulile
parserului și pentru ce s-a încercat și nu merge. Aici e doar jobul: citește secțiunile, aplică
modulul pur, raportează acoperirea și cele mai frecvente valori, și scrie DOAR cu `--apply`.

Scrierea e idempotentă (`jsonb_set` pe aceeași cheie, cu aceeași valoare) și NU șterge nimic: un
produs fără secțiune sau cu o secțiune din care nu iese niciun nume rămâne fără cheie — absența
înseamnă „nu știm", iar `load_vocabulary` ignoră oricum valorile sub prag.

    python scripts/derive_key_ingredients.py --business <uuid>            # dry-run + raport
    python scripts/derive_key_ingredients.py --business <uuid> --sample 10
    python scripts/derive_key_ingredients.py --business <uuid> --apply    # scrie
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

from src.catalog.key_ingredients import canonical_values, parse_key_ingredients  # noqa: E402
from src.db.connection import admin_conn, close_pool, get_pool  # noqa: E402

#: Butoanele de UI copiate în textul secțiunii odată cu pagina. Sunt ale importului, nu ale
#: domeniului, de aceea stau în script și nu în modulul pur.
UI_FILLERS = ("Vezi mai multe detalii", "Ascunde")

_SELECT = """
    select p.id, p.name, p.attributes->'key_ingredients' as current, s.body
      from products p
      join product_sections s on s.product_id = p.id and s.kind = 'key_ingredients'
     where p.business_id = $1 and p.status = 'active'
     order by p.id
"""

_UPDATE = """
    update products
       set attributes = jsonb_set(
               coalesce(attributes, '{}'::jsonb), '{key_ingredients}', $2::jsonb, true
           ),
           updated_at = now()
     where business_id = $3 and id = $1
       and attributes->'key_ingredients' is distinct from $2::jsonb
"""


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--business", required=True, help="business_id (uuid)")
    ap.add_argument("--apply", action="store_true", help="scrie în catalog (altfel dry-run)")
    ap.add_argument("--sample", type=int, default=6, help="câte exemple să afișeze")
    ap.add_argument("--top", type=int, default=25, help="câte valori frecvente să afișeze")
    args = ap.parse_args()

    pool = await get_pool()
    async with admin_conn(pool) as conn:
        rows = await conn.fetch(_SELECT, args.business)
        total_active = await conn.fetchval(
            "select count(*) from products where business_id = $1 and status = 'active'",
            args.business,
        )

    derived: list[tuple[str, list[str], list[str]]] = []
    freq: Counter[str] = Counter()
    for r in rows:
        names = parse_key_ingredients(r["body"], drop=UI_FILLERS)
        values = canonical_values(names)
        if values:
            derived.append((str(r["id"]), values, names))
            freq.update(values)

    print(f"produse active: {total_active} · cu secțiune: {len(rows)} · cu valori: {len(derived)}")
    print(f"acoperire: {len(derived) / max(total_active, 1):.1%}")
    print(
        f"valori distincte: {len(freq)} · medie/produs: "
        f"{sum(len(v) for _, v, _ in derived) / max(len(derived), 1):.1f}"
    )
    print("\ncele mai frecvente valori:")
    for value, n in freq.most_common(args.top):
        print(f"  {n:5d}  {value}")
    print("\nexemple:")
    for pid, values, names in derived[: args.sample]:
        print(f"  {pid}  {names[:6]}")

    if not args.apply:
        print("\nDRY-RUN. Nimic scris. Adaugă --apply ca să scrii.")
        await close_pool()
        return 0

    written = 0
    async with admin_conn(pool) as conn:
        async with conn.transaction():
            for pid, values, _ in derived:
                status = await conn.execute(_UPDATE, pid, json.dumps(values), args.business)
                written += int(status.split()[-1])
    print(f"\nSCRIS: {written} rânduri actualizate din {len(derived)} derivate.")
    await close_pool()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
