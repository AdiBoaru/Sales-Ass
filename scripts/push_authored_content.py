"""NX-196 — scrie fișele autorate în DB, PE LOTURI.

De ce separat de `seed_catalog_v2.py`: seed-ul rulează tot într-o SINGURĂ tranzacție (corect —
un catalog pe jumătate scris ar fi mai rău decât unul vechi). Cu fișe de ~2.000 de caractere ×
300 de produse, plus secțiunile, tranzacția depășește limita poolerului Supabase și conexiunea e
închisă la mijloc. Aici scriem în loturi mici, fiecare cu tranzacția lui: dacă pică lotul 7,
primele 6 rămân scrise și re-rularea continuă de unde a rămas (idempotent pe slug).

Atinge DOAR conținutul: short_description, description, attributes.specs, product_sections.
Nu se apropie de preț, stoc, variante sau relații — alea rămân treaba seed-ului.

    python scripts/push_authored_content.py --dry-run   # verifică + raportează, nu scrie
    python scripts/push_authored_content.py             # scrie, în loturi de 20
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import authored_content  # noqa: E402
from src.worker.text_scrub import has_medical_claim  # noqa: E402

DEMO_BIZ = "6098812a-50fc-44bd-a1ba-bc77e6399158"
DATA = ROOT / "db" / "seed" / "catalog_v2.json"
BATCH = 20


async def push(conn, products: list[dict], *, dry: bool) -> tuple[int, list[str]]:
    written = 0
    problems: list[str] = []
    for p in products:
        content = authored_content.compose(p, has_medical_claim)
        if not content:
            problems.append(f"{p['slug']}: fără bloc de categorie")
            continue
        problems += authored_content.validate(p, content, has_medical_claim)
        if dry:
            written += 1
            continue

        row = await conn.fetchrow(
            "select id, attributes from products where business_id=$1 and slug=$2",
            DEMO_BIZ,
            p["slug"],
        )
        if not row:
            problems.append(f"{p['slug']}: nu există în DB")
            continue
        pid = row["id"]
        attrs = row["attributes"]
        attrs = json.loads(attrs) if isinstance(attrs, str) else (attrs or {})
        attrs["specs"] = {**(attrs.get("specs") or {}), **content["specs"]}

        await conn.execute(
            "update products set short_description=$1, description=$2, attributes=$3 "
            "where id=$4 and business_id=$5",
            content["shortDescription"],
            content["description"],
            json.dumps(attrs, ensure_ascii=False),
            pid,
            DEMO_BIZ,
        )
        # secțiunile: șterge + reinserează (sursa de adevăr e fișa compusă)
        await conn.execute("delete from product_sections where product_id=$1", pid)
        for pos, sec in enumerate(content["sections"]):
            await conn.execute(
                "insert into product_sections "
                "(business_id, product_id, kind, title, body, position, locale, voice) "
                "values ($1,$2,$3,$4,$5,$6,'ro',$7)",
                DEMO_BIZ,
                pid,
                sec["kind"],
                sec["title"],
                sec["body"],
                pos,
                sec["voice"],
            )
        written += 1
    return written, problems


async def main() -> int:
    dry = "--dry-run" in sys.argv
    from src.db.connection import admin_conn, close_pool, get_pool  # noqa: PLC0415

    data = json.loads(DATA.read_text(encoding="utf-8"))
    products = data["products"]
    total, all_problems = 0, []

    pool = await get_pool()
    for i in range(0, len(products), BATCH):
        chunk = products[i : i + BATCH]
        async with admin_conn(pool) as conn:
            async with conn.transaction():
                n, probs = await push(conn, chunk, dry=dry)
                total += n
                all_problems += probs
        print(f"  lot {i // BATCH + 1}: {n} produse ({total}/{len(products)})", flush=True)

    await close_pool()
    print(f"\n{'(dry-run) ' if dry else ''}fișe scrise: {total}/{len(products)}")
    if all_problems:
        print(f"⚠ {len(all_problems)} probleme:")
        for x in all_problems[:10]:
            print("   ", x)
        return 1
    print("✓ toate fișele trec porțile de conținut")
    return 0


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(asyncio.run(main()))
