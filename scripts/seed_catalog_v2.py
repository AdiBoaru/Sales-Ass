"""Seed catalogul demo v2 (hand-curated, coerent) în DEMO_BIZ — NX-168b.

Citește `db/seed/catalog_v2.json` și inserează brands + categories (cu parent/path) + products +
product_variants + product_category_map + product_review_summaries. IDEMPOTENT pe slug (rerun =
UPDATE, nu duplicat; variantele + review-summary se șterg + reinserează, sursa de adevăr = JSON).
Rol ADMIN (seeding e op privilegiată, ca celelalte scripturi de catalog).

**PRE-FLIGHT GATE (NX-168a):** rulează auditul static ÎNAINTE de orice scriere; dacă picã, NU
seedează (exit ≠ 0). Un catalog incoerent nu ajunge niciodată în DB.

    python scripts/seed_catalog_v2.py --dry-run          # rulează gate + rollback (nimic scris)
    python scripts/seed_catalog_v2.py                    # gate + seed
    python scripts/seed_catalog_v2.py --archive-old      # + arhivează produsele vechi ne-v2
                                                         #   (status='archived') pe tenant

După seed: re-embed produsele noi (job de embed) pt search semantic — lexical FTS merge oricum.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from pathlib import Path

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.audit_catalog_v2 import audit  # noqa: E402 — pre-flight gate
from src.db.connection import admin_conn, close_pool, get_pool  # noqa: E402

DEMO_BIZ = "6098812a-50fc-44bd-a1ba-bc77e6399158"
STORE_BASE = "https://shop.sole-demo.ro/p/"
DATA = ROOT / "db" / "seed" / "catalog_v2.json"


async def _upsert_brand(conn, slug: str, name: str) -> str:
    row = await conn.fetchrow(
        "select id from brands where business_id=$1 and slug=$2", DEMO_BIZ, slug
    )
    if row:
        await conn.execute("update brands set name=$1 where id=$2", name, row["id"])
        return row["id"]
    return await conn.fetchval(
        "insert into brands (business_id, slug, name) values ($1,$2,$3) returning id",
        DEMO_BIZ,
        slug,
        name,
    )


async def _upsert_category(conn, slug: str, name: str, parent_slug: str | None) -> str:
    parent_id = None
    if parent_slug:
        parent_id = await conn.fetchval(
            "select id from categories where business_id=$1 and slug=$2", DEMO_BIZ, parent_slug
        )
    path = f"{parent_slug}/{slug}" if parent_slug else slug
    row = await conn.fetchrow(
        "select id from categories where business_id=$1 and slug=$2", DEMO_BIZ, slug
    )
    if row:
        await conn.execute(
            "update categories set name=$1, parent_id=$2, path=$3 where id=$4",
            name,
            parent_id,
            path,
            row["id"],
        )
        return row["id"]
    return await conn.fetchval(
        "insert into categories (business_id, slug, name, parent_id, path) "
        "values ($1,$2,$3,$4,$5) returning id",
        DEMO_BIZ,
        slug,
        name,
        parent_id,
        path,
    )


async def _upsert_product(conn, p: dict, brand_id: str, cat_id: str) -> str:
    variants = p.get("variants") or []
    # produse fără variante: stoc default (100) ca să fie in_stock; altfel suma variantelor
    stock_total = sum(int(v.get("stock", 0)) for v in variants) or 100
    availability = "in_stock" if stock_total > 0 else "out_of_stock"
    url = STORE_BASE + p["slug"]
    fp = "V2-" + hashlib.sha256(p["slug"].encode()).hexdigest()[:24]
    attrs = json.dumps(p.get("attributes") or {}, ensure_ascii=False)
    cols = dict(
        brand_id=brand_id,
        primary_category_id=cat_id,
        external_id="V2-" + p["slug"],
        source_fingerprint=fp,
        name=p["name"],
        short_description=p.get("shortDescription"),
        description=p.get("description") or p.get("shortDescription"),
        ai_summary=p.get("shortDescription"),
        currency=p.get("currency", "RON"),
        price=p["price"],
        sale_price=p.get("salePrice"),
        availability=availability,
        stock_total=stock_total,
        rating=p.get("rating", 0),
        review_count=p.get("reviewCount", 0),
        status=p.get("status", "active"),
        attributes=attrs,
        product_url=url,
    )
    row = await conn.fetchrow(
        "select id from products where business_id=$1 and slug=$2", DEMO_BIZ, p["slug"]
    )
    keys = list(cols)
    if row:
        set_sql = ", ".join(f"{k}=${i + 2}" for i, k in enumerate(keys))
        await conn.execute(
            f"update products set {set_sql} where id=$1", row["id"], *[cols[k] for k in keys]
        )
        pid = row["id"]
    else:
        col_sql = ", ".join(["business_id", "slug", *keys])
        ph = ", ".join(f"${i + 1}" for i in range(len(keys) + 2))
        pid = await conn.fetchval(
            f"insert into products ({col_sql}) values ({ph}) returning id",
            DEMO_BIZ,
            p["slug"],
            *[cols[k] for k in keys],
        )

    # category_map: primary + toate categorySlugs (idempotent — șterge + reinserează)
    await conn.execute("delete from product_category_map where product_id=$1", pid)
    for pos, cslug in enumerate(p.get("categorySlugs") or [p["primaryCategorySlug"]]):
        cid = await conn.fetchval(
            "select id from categories where business_id=$1 and slug=$2", DEMO_BIZ, cslug
        )
        if cid:
            await conn.execute(
                "insert into product_category_map (product_id, category_id, position) "
                "values ($1,$2,$3) on conflict do nothing",
                pid,
                cid,
                pos,
            )

    # variante: sursă de adevăr = JSON → șterge + reinserează
    await conn.execute("delete from product_variants where product_id=$1", pid)
    for i, v in enumerate(variants):
        sku = v.get("sku") or f"V2-{p['slug']}-{i:02d}"
        await conn.execute(
            "insert into product_variants "
            "(business_id, product_id, label, sku, external_id, price, sale_price, stock, "
            " color_hex, attributes) values ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)",
            DEMO_BIZ,
            pid,
            v["label"],
            sku,
            sku,
            v.get("price", p["price"]),
            v.get("salePrice", p.get("salePrice")),
            int(v.get("stock", 0)),
            v.get("colorHex"),
            json.dumps(v.get("attributes") or {}, ensure_ascii=False),
        )

    # review summary (D3): sursă de adevăr = JSON → upsert pe product_id (PK)
    rs = p.get("reviewSummary")
    if rs:
        await conn.execute(
            "insert into product_review_summaries "
            "(product_id, business_id, summary, top_pros, top_cons) values ($1,$2,$3,$4,$5) "
            "on conflict (product_id) do update set summary=excluded.summary, "
            "top_pros=excluded.top_pros, top_cons=excluded.top_cons",
            pid,
            DEMO_BIZ,
            rs.get("summary"),
            list(rs.get("topPros") or []),
            list(rs.get("topCons") or []),
        )
    return pid


async def main() -> int:
    dry = "--dry-run" in sys.argv
    archive_old = "--archive-old" in sys.argv
    data = json.loads(DATA.read_text(encoding="utf-8"))

    # === PRE-FLIGHT GATE (NX-168a): audit static ÎNAINTE de orice scriere ===
    results = audit(data)
    total = sum(len(v) for v in results.values())
    if total:
        print(f"✗ AUDIT PICAT — {total} violări; NU seedez. Rulează scripts/audit_catalog_v2.py.")
        for key, viol in results.items():
            for line in viol[:3]:
                print(f"    [{key}] {line}")
        return 1
    print(f"✓ audit static curat ({len(data['products'])} produse) — pornesc seed-ul\n")

    pool = await get_pool()
    async with admin_conn(pool) as conn:
        async with conn.transaction():
            v2_slugs = [p["slug"] for p in data["products"]]
            if archive_old:
                n = await conn.fetchval(
                    "with u as (update products set status='archived' "
                    "where business_id=$1 and slug <> all($2::text[]) and status='active' "
                    "returning 1) select count(*) from u",
                    DEMO_BIZ,
                    v2_slugs,
                )
                print(f"  arhivat {n} produse vechi (ne-v2) → status='archived'")

            brand_ids = {
                b["slug"]: await _upsert_brand(conn, b["slug"], b["name"]) for b in data["brands"]
            }
            for c in data["categories"]:
                await _upsert_category(conn, c["slug"], c["name"], c.get("parentSlug"))
            n_var = 0
            for p in data["products"]:
                cat_id = await conn.fetchval(
                    "select id from categories where business_id=$1 and slug=$2",
                    DEMO_BIZ,
                    p["primaryCategorySlug"],
                )
                await _upsert_product(conn, p, brand_ids[p["brandSlug"]], cat_id)
                n_var += len(p.get("variants") or [])
                print(f"  seedat: {p['name']} ({len(p.get('variants') or [])} variante)")
            print(
                f"\n{len(data['products'])} produse, {n_var} variante, "
                f"{len(data['brands'])} branduri, {len(data['categories'])} categorii."
            )
            if dry:
                raise RuntimeError("--dry-run → rollback (nimic scris)")
    await close_pool()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except RuntimeError as e:
        print(f"\n{e}")
        raise SystemExit(0) from None
