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
from urllib.parse import quote_plus

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import pdp_content  # noqa: E402 — NX-168e-2 graf PDP derivat
from scripts.audit_catalog_v2 import (  # noqa: E402 — pre-flight gate + arbore
    _gtin_valid,
    build_roots,
    evaluate,
)

# NB: importul DB (`src.db.connection`) + politica asyncio Windows sunt LAZY (în `main()` /
# `__main__`) ca importul acestui modul — ex. din teste, pt `gate_violations` — să NU atingă DB
# și să NU schimbe event-loop policy global la colectarea pytest.

DEMO_BIZ = "6098812a-50fc-44bd-a1ba-bc77e6399158"
STORE_BASE = "https://shop.sole-demo.ro/p/"
DATA = ROOT / "db" / "seed" / "catalog_v2.json"

# Placeholder-e consistente pe categorie (runtime W1 citește prima poză). Culoare de fundal per
# RAMURĂ top-level → cardurile arată coerent vizual pe categorie. placehold.co = serviciu care
# randează efectiv (ca în catalogul demo vechi). Explicit `images` în JSON are prioritate.
_ROOT_BG = {
    "ingrijirea-tenului": "e8f0ea",
    "machiaj": "f6e3ea",
    "ingrijirea-parului": "f3e9d8",
    "ingrijire-corp": "e6eef2",
    "protectie-solara": "fdf3d8",
    "buze": "f7e6ea",
}


def _placeholder_image(name: str, root: str) -> str:
    bg = _ROOT_BG.get(root, "f5eee9")
    return f"https://placehold.co/900x1200/{bg}/222222?text={quote_plus(name)}"


def gate_violations(data: dict, contract: str = "v2") -> list[dict]:
    """Poarta pre-flight a seed-ului: lista PLATĂ de blocaje (SCHEMĂ + reguli), din `evaluate()`
    (sursă UNICĂ, partajată cu backfill-ul NX-171c). `warnings` excluse STRUCTURAL (citim DOAR
    `['violations']`). Exercitat direct de teste — codul REAL, nu o formulă duplicată (NX-168d)."""
    violations = evaluate(data, contract=contract)["violations"]
    return [entry for viol in violations.values() for entry in viol]


def clean_gtin(raw) -> str | None:
    """NX-171a: GTIN valid GS1 (mod-10) → păstrat ca string; invalid/absent → None (nu scriem un
    cod fals pe variantă, aliniat cu audit R9). Testabil separat de calea DB."""
    return str(raw) if raw and _gtin_valid(str(raw)) else None


# NX-171b: ordinea canonică a pașilor de rutină (familie, rang). Sursa semnalului = `routine_step`
# din attributes (168e). Două familii: skincare (0) + makeup (1). Un produs se leagă doar în familia
# lui → nu amestecăm un fond de ten în rutina de îngrijire.
_ROUTINE_STEPS = {
    "cleanse": (0, 0),
    "tone": (0, 1),
    "treat": (0, 2),
    "moisturize": (0, 3),
    "protect": (0, 4),
    "makeup_base": (1, 0),
    "makeup_color": (1, 1),
    "finish": (1, 2),
}


def _rel_rating(p: dict) -> float:
    return float(p.get("rating") or 0)


def _rel_price(p: dict) -> float:
    return float(p.get("price") or 0)


def _rel_concerns(p: dict) -> set[str]:
    c = (p.get("attributes") or {}).get("concerns")
    return set(c) if isinstance(c, list) else set()


def _rel_step(p: dict) -> tuple[int, int] | None:
    step = (p.get("attributes") or {}).get("routine_step")
    return _ROUTINE_STEPS.get(step) if isinstance(step, str) else None


def derive_relations(products: list[dict]) -> list[dict]:
    """NX-171b: derivă DETERMINIST relații explicite din faptele catalogului (fără random/LLM).

    - `routine_next`: produsul de la pasul N → produse de la următorul pas OCUPAT din ACEEAȘI
      familie de rutină (skincare: cleanse→tone→treat→moisturize→protect; makeup: base→color→
      finish), preferând același brand, apoi concern comun, apoi rating. Cap 3.
    - `complement`: același brand, categorie primară DIFERITĂ (gama „se poartă cu"). Cap 3, rating.
    - `substitute`: aceeași categorie primară, preț ≤ ancoră (alternativă mai ieftină/egală). Cap 3.

    Întoarce `{product_slug, related_slug, kind, position}`. Fără self-relation; sortări STABILE pe
    slug → aceeași ieșire la re-rulare (idempotent). Testabil fără DB."""
    out: list[dict] = []

    # index familie → rang → [produse] (pentru routine_next)
    fam_rank: dict[int, dict[int, list[dict]]] = {}
    for p in products:
        rk = _rel_step(p)
        if rk:
            fam, rank = rk
            fam_rank.setdefault(fam, {}).setdefault(rank, []).append(p)

    for p in products:
        rk = _rel_step(p)
        if not rk:
            continue
        fam, rank = rk
        later = sorted(r for r in fam_rank.get(fam, {}) if r > rank)
        if not later:
            continue
        pc = _rel_concerns(p)
        cands = [q for q in fam_rank[fam][later[0]] if q["slug"] != p["slug"]]
        cands.sort(
            key=lambda q: (
                q.get("brandSlug") != p.get("brandSlug"),  # same-brand întâi (False < True)
                not (_rel_concerns(q) & pc),  # concern comun apoi
                -_rel_rating(q),
                q["slug"],
            )
        )
        for pos, q in enumerate(cands[:3]):
            out.append(_rel(p, q, "routine_next", pos))

    by_brand: dict[str, list[dict]] = {}
    for p in products:
        if p.get("brandSlug"):
            by_brand.setdefault(p["brandSlug"], []).append(p)
    for p in products:
        b = p.get("brandSlug")
        if not b:
            continue
        cands = [
            q
            for q in by_brand[b]
            if q["slug"] != p["slug"]
            and q.get("primaryCategorySlug") != p.get("primaryCategorySlug")
        ]
        cands.sort(key=lambda q: (-_rel_rating(q), q["slug"]))
        for pos, q in enumerate(cands[:3]):
            out.append(_rel(p, q, "complement", pos))

    by_cat: dict[str, list[dict]] = {}
    for p in products:
        if p.get("primaryCategorySlug"):
            by_cat.setdefault(p["primaryCategorySlug"], []).append(p)
    for p in products:
        c = p.get("primaryCategorySlug")
        if not c:
            continue
        cands = [q for q in by_cat[c] if q["slug"] != p["slug"] and _rel_price(q) <= _rel_price(p)]
        cands.sort(key=lambda q: (_rel_price(q), q["slug"]))
        for pos, q in enumerate(cands[:3]):
            out.append(_rel(p, q, "substitute", pos))

    return out


def _rel(p: dict, q: dict, kind: str, position: int) -> dict:
    return {
        "product_slug": p["slug"],
        "related_slug": q["slug"],
        "kind": kind,
        "position": position,
    }


async def _upsert_brand(conn, slug: str, name: str) -> str:
    row = await conn.fetchrow(
        "select id from brands where business_id=$1 and slug=$2", DEMO_BIZ, slug
    )
    if row:
        await conn.execute(
            "update brands set name=$1 where id=$2 and business_id=$3", name, row["id"], DEMO_BIZ
        )
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
            "update categories set name=$1, parent_id=$2, path=$3 where id=$4 and business_id=$5",
            name,
            parent_id,
            path,
            row["id"],
            DEMO_BIZ,
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


async def _upsert_product(conn, p: dict, brand_id: str, cat_id: str, root: str) -> str:
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
        ai_summary=p.get("ai_summary") or p.get("shortDescription"),
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
            f"update products set {set_sql} where id=$1 and business_id=${len(keys) + 2}",
            row["id"],
            *[cols[k] for k in keys],
            DEMO_BIZ,
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

    # category_map: primary + toate categorySlugs (idempotent — șterge + reinserează).
    # NB: product_category_map / product_images NU au business_id (scoped prin FK products) —
    # tenant guard-ul e pe `pid`, obținut mai sus cu filtru business_id.
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
    await conn.execute(
        "delete from product_variants where product_id=$1 and business_id=$2", pid, DEMO_BIZ
    )
    for i, v in enumerate(variants):
        sku = v.get("sku") or f"V2-{p['slug']}-{i:02d}"
        # NX-171a: coloane comerciale pe variantă (sursa de adevăr). GTIN invalid GS1 → NULL (nu
        # scriem un cod fals; aliniat cu audit R9). net_content = fapt comercial (preț/unitate).
        gtin = clean_gtin(v.get("gtin"))
        nc = v.get("net_content") or {}
        await conn.execute(
            "insert into product_variants "
            "(business_id, product_id, label, sku, external_id, price, sale_price, stock, "
            " color_hex, attributes, gtin, net_content_value, net_content_unit, image_url) "
            "values ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)",
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
            gtin,
            nc.get("value"),
            nc.get("unit"),
            v.get("image"),
        )

    # review summary (D3): sursă de adevăr = JSON → upsert pe product_id (PK).
    # `review_count_at_build` NOT NULL (schema) = reviewCount-ul produsului la build.
    rs = p.get("reviewSummary")
    if rs:
        await conn.execute(
            "insert into product_review_summaries "
            "(product_id, business_id, summary, top_pros, top_cons, review_count_at_build) "
            "values ($1,$2,$3,$4,$5,$6) "
            "on conflict (product_id) do update set summary=excluded.summary, "
            "top_pros=excluded.top_pros, top_cons=excluded.top_cons, "
            "review_count_at_build=excluded.review_count_at_build "
            "where product_review_summaries.business_id=excluded.business_id",
            pid,
            DEMO_BIZ,
            rs.get("summary"),
            list(rs.get("topPros") or []),
            list(rs.get("topCons") or []),
            int(p.get("reviewCount", 0)),
        )

    # imagini (runtime W1 citește prima): explicit din JSON, altfel un placeholder consistent pe
    # categorie. Idempotent (șterge + reinserează, sursa de adevăr = JSON/placeholder).
    await conn.execute("delete from product_images where product_id=$1", pid)
    images = p.get("images") or [{"url": _placeholder_image(p["name"], root), "alt": p["name"]}]
    for pos, im in enumerate(images):
        await conn.execute(
            "insert into product_images (product_id, url, alt, position) values ($1,$2,$3,$4)",
            pid,
            im["url"],
            im.get("alt", p["name"]),
            im.get("position", pos),
        )

    # === NX-168e-2: graf PDP derivat determinist (sections/ingredients/badges/reviews) ===
    # Toate idempotente (șterge + reinserează pe cheile stabile). Tabele scoped prin FK products
    # (sections/product_ingredients/product_badges NU au business_id); reviews ARE business_id.
    await conn.execute("delete from product_sections where product_id=$1", pid)
    for pos, sec in enumerate(pdp_content.sections(p)):
        await conn.execute(
            "insert into product_sections (product_id, kind, title, body, position) "
            "values ($1,$2,$3,$4,$5)",
            pid,
            sec["kind"],
            sec["title"],
            sec["body"],
            pos,
        )
    # ingrediente normalizate (upsert pe (business_id, slug)) + legături is_key
    await conn.execute("delete from product_ingredients where product_id=$1", pid)
    for pos, ing in enumerate(pdp_content.ingredient_list(p)):
        ing_id = await conn.fetchval(
            "insert into ingredients (business_id, name, slug) values ($1,$2,$3) "
            "on conflict (business_id, slug) do update set name=excluded.name returning id",
            DEMO_BIZ,
            ing,
            pdp_content.slugify(ing),
        )
        await conn.execute(
            "insert into product_ingredients (product_id, ingredient_id, position, is_key) "
            "values ($1,$2,$3,true) on conflict (product_id, ingredient_id) do update "
            "set position=excluded.position, is_key=true",
            pid,
            ing_id,
            pos,
        )
    # badge-uri de trust (derivate din atribute reale)
    await conn.execute("delete from product_badges where product_id=$1", pid)
    for label in pdp_content.badges(p):
        await conn.execute(
            "insert into product_badges (product_id, label) values ($1,$2)", pid, label
        )
    # recenzii individuale (sursă seed_demo; idempotent pe (business_id, source, external_id))
    await conn.execute(
        "delete from reviews where product_id=$1 and business_id=$2 and source='seed_demo'",
        pid,
        DEMO_BIZ,
    )
    for rv in pdp_content.reviews(p):
        await conn.execute(
            "insert into reviews "
            "(business_id, product_id, source, external_id, author, rating, body) "
            "values ($1,$2,'seed_demo',$3,$4,$5,$6) "
            "on conflict (business_id, source, external_id) do nothing",
            DEMO_BIZ,
            pid,
            rv["external_id"],
            rv["author"],
            rv["rating"],
            rv["body"],
        )
    return pid


async def main() -> int:
    dry = "--dry-run" in sys.argv
    archive_old = "--archive-old" in sys.argv
    # import DB lazy (importul modulului rămâne fără efecte secundare — vezi nota de sus)
    from src.db.connection import admin_conn, close_pool, get_pool  # noqa: PLC0415

    data = json.loads(DATA.read_text(encoding="utf-8"))

    # === PRE-FLIGHT GATE: audit static ÎNAINTE de orice scriere ===
    # NX-168e: COMUTARE ATOMICĂ pe v3 — catalogul e la contract complet (evaluate v3 = 0).
    blocking = gate_violations(data, contract="v3")  # schema+reguli v3; warnings NU blochează
    if blocking:
        print(
            f"✗ AUDIT PICAT — {len(blocking)} violations; NU seedez. Rulează audit_catalog_v2.py."
        )
        for entry in blocking[:6]:
            print(f"    - {entry['message']}")
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
            roots = build_roots(data["categories"])  # slug → root-branch (pt culoarea placeholder)
            n_var = 0
            for p in data["products"]:
                cat_id = await conn.fetchval(
                    "select id from categories where business_id=$1 and slug=$2",
                    DEMO_BIZ,
                    p["primaryCategorySlug"],
                )
                root = roots.get(p["primaryCategorySlug"], "")
                await _upsert_product(conn, p, brand_ids[p["brandSlug"]], cat_id, root)
                n_var += len(p.get("variants") or [])
                print(f"  seedat: {p['name']} ({len(p.get('variants') or [])} variante)")

            # NX-171b: relații explicite (rutină/complement/substitut) derivate DETERMINIST din
            # faptele catalogului. Sursă de adevăr = JSON → șterge + reinserează (idempotent).
            slug_to_id = {
                r["slug"]: str(r["id"])
                for r in await conn.fetch(
                    "select id, slug from products where business_id=$1", DEMO_BIZ
                )
            }
            await conn.execute("delete from product_relations where business_id=$1", DEMO_BIZ)
            n_rel = 0
            for rel in derive_relations(data["products"]):
                pid_a = slug_to_id.get(rel["product_slug"])
                pid_b = slug_to_id.get(rel["related_slug"])
                if not pid_a or not pid_b or pid_a == pid_b:
                    continue
                await conn.execute(
                    "insert into product_relations "
                    "(business_id, product_id, related_id, kind, position) values ($1,$2,$3,$4,$5) "
                    "on conflict (business_id, product_id, related_id, kind) do nothing",
                    DEMO_BIZ,
                    pid_a,
                    pid_b,
                    rel["kind"],
                    rel["position"],
                )
                n_rel += 1

            print(
                f"\n{len(data['products'])} produse, {n_var} variante, "
                f"{len(data['brands'])} branduri, {len(data['categories'])} categorii, "
                f"{n_rel} relații."
            )
            if dry:
                raise RuntimeError("--dry-run → rollback (nimic scris)")
    await close_pool()
    return 0


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
    try:
        raise SystemExit(asyncio.run(main()))
    except RuntimeError as e:
        print(f"\n{e}")
        raise SystemExit(0) from None
