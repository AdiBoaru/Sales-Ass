"""Importă catalogul SOLE din SQLite în Postgres. LOSSLESS: nimic nu se exclude la scriere.

Principiul, decis de owner: **proveniența intră în schemă, filtrarea e politică la citire.**
Proza generată de asistentul AURA intră, dar cu `source='aura'`; badge-urile de marketing ale
magazinului sursă intră, dar cu `kind='merchant_marketing'`; toate cele 183.003 de recenzii
intră. „Nu rosti X" devine un `where`, nu un re-import de 2.767 de produse.

Regulile de citire a sursei trăiesc în `src/catalog/sole_source.py` (pur, testat separat).
Aici e doar bucla care scrie, plus deciziile de scriere: idempotență, batching, proveniență.

Idempotent pe toate tabelele: a doua rulare nu dublează nimic. Cheile naturale:
    products              (business_id, external_id)
    product_variants      (business_id, sku)
    product_sections      (business_id, product_id, locale, source, source_key)
    product_badges        (business_id, product_id, locale, label)
    product_images        (business_id, product_id, url)
    product_faqs          (business_id, product_id, locale, question)
    reviews               (business_id, source, external_id)
    product_ingredients   (product_id, ingredient_id)

Rulare:
    TARGET_DB_URL=postgresql://... python scripts/import_sole.py --apply
    ... --limit 50        # doar primele N produse, pentru o probă rapidă
    ... --skip-reviews    # sare peste cele 183k, pentru iterații rapide
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import asyncpg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.catalog import sole_source as ss  # noqa: E402

SOURCE_DB = os.environ.get("SOLE_SOURCE_DB", r"D:\Work\SOLE SCRIPT\sole_data.db")
SOURCE_SITE = "sole.ro"
LOCALE = "ro"
BATCH = 2000

OK, WARN = "  ok  ", " ATENTIE "


def say(status: str, msg: str) -> None:
    print(f"[{status}] {msg}", flush=True)


# ============================================================================
# Tenantul
# ============================================================================


async def ensure_business(conn: asyncpg.Connection, slug: str) -> uuid.UUID:
    """`business_id` e SERVER-OWNED: se derivă aici, nu vine niciodată din date."""
    row = await conn.fetchrow("select id from businesses where slug = $1", slug)
    if row:
        return row["id"]
    return await conn.fetchval(
        """insert into businesses (slug, name, vertical, status, default_locale,
                                   supported_locales, timezone)
           values ($1, 'SOLE', 'ecommerce', 'active', 'ro', array['ro'], 'Europe/Bucharest')
           returning id""",
        slug,
    )


# ============================================================================
# Rularea de sync + alertele de calitate
# ============================================================================


async def start_run(conn: asyncpg.Connection, biz: uuid.UUID) -> uuid.UUID:
    return await conn.fetchval(
        "insert into catalog_sync_runs (business_id, source, status) "
        "values ($1, $2, 'running') returning id",
        biz,
        SOURCE_SITE,
    )


async def finish_run(
    conn: asyncpg.Connection, run: uuid.UUID, stats: dict[str, int], error: str | None
) -> None:
    await conn.execute(
        "update catalog_sync_runs set status = $2, stats = $3, error = $4, finished_at = now() "
        "where id = $1",
        run,
        "failed" if error else "succeeded",
        json.dumps(stats),
        error,
    )


async def flush_alerts(
    conn: asyncpg.Connection, biz: uuid.UUID, run: uuid.UUID, alerts: list[tuple[Any, str, dict]]
) -> None:
    """`catalog_quality_alerts`: „alertă, nu publicare".

    Datele imperfecte intră oricum (import lossless), dar nu în tăcere. Un preț promoțional mai
    mare decât cel normal, un volum compus care nu se poate parsa, o cheie de secțiune nouă la
    sursă: toate se importă și toate lasă urmă.
    """
    if not alerts:
        return
    await conn.executemany(
        "insert into catalog_quality_alerts (business_id, sync_run_id, product_id, kind, details) "
        "values ($1, $2, $3, $4, $5)",
        [(biz, run, pid, kind, json.dumps(details)) for pid, kind, details in alerts],
    )


# ============================================================================
# Dimensiuni: branduri, categorii, ingrediente
# ============================================================================


async def import_brands(conn: asyncpg.Connection, biz: uuid.UUID, src: sqlite3.Connection) -> dict:
    names = [
        r[0] for r in src.execute("select distinct brand from products where brand is not null")
    ]
    rows = [(biz, n, ss.slugify(n)) for n in names]
    await conn.executemany(
        "insert into brands (business_id, name, slug) values ($1,$2,$3) "
        "on conflict (business_id, slug) do update set name = excluded.name",
        rows,
    )
    got = await conn.fetch("select slug, id from brands where business_id = $1", biz)
    say(OK, f"branduri: {len(names)}")
    return {r["slug"]: r["id"] for r in got}


async def import_categories(
    conn: asyncpg.Connection, biz: uuid.UUID, src: sqlite3.Connection
) -> dict:
    """Ierarhie din „Ten > Ingrijirea tenului": părintele întâi, ca `parent_id` să existe."""
    paths = [
        ss.parse_category_path(r[0]) for r in src.execute("select distinct category from products")
    ]
    tops = {p[0] for p in paths if p}
    children = {(p[0], p[1]) for p in paths if len(p) > 1}

    await conn.executemany(
        "insert into categories (business_id, name, slug, path) values ($1,$2,$3,$4) "
        "on conflict (business_id, slug) do update set name = excluded.name",
        [(biz, t, ss.slugify(t), ss.slugify(t)) for t in sorted(tops)],
    )
    ids = {
        r["slug"]: r["id"]
        for r in await conn.fetch("select slug, id from categories where business_id = $1", biz)
    }
    await conn.executemany(
        "insert into categories (business_id, parent_id, name, slug, path) values ($1,$2,$3,$4,$5) "
        "on conflict (business_id, slug) do update set name = excluded.name, "
        "parent_id = excluded.parent_id, path = excluded.path",
        [
            (
                biz,
                ids[ss.slugify(top)],
                child,
                ss.slugify(f"{top}-{child}"),
                f"{ss.slugify(top)}/{ss.slugify(child)}",
            )
            for top, child in sorted(children)
        ],
    )
    got = await conn.fetch("select slug, id from categories where business_id = $1", biz)
    say(OK, f"categorii: {len(tops)} nivel 1, {len(children)} nivel 2")
    return {r["slug"]: r["id"] for r in got}


def _parse_scraped_at(raw: str | None) -> datetime | None:
    """`scraped_at` din sursă (ISO), nu `now()`.

    `now()` ar înregistra când am rulat IMPORTUL, nu când a fost citită sursa. Diferența
    contează la prima întrebare serioasă despre prospețime: „de când e prețul ăsta?".
    Neparsabil → `None`, iar SQL-ul cade pe `now()` cu `coalesce`.
    """
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def category_slug_for(path: list[str]) -> str | None:
    if not path:
        return None
    return ss.slugify(f"{path[0]}-{path[1]}") if len(path) > 1 else ss.slugify(path[0])


# ============================================================================
# Produse
# ============================================================================

PRODUCT_COLS = (
    "business_id, external_id, source_fingerprint, brand_id, primary_category_id, name, slug, "
    "description, currency, price, sale_price, coupon_code, coupon_price, availability, "
    "stock_total, rating, review_count, status, content_status, attributes, product_url, "
    "min_durability_date, pao_months, storage_temp_min_c, storage_temp_max_c, "
    "source_site, last_sync_run_id, extractor_version, synced_at"
)

EXTRACTOR_VERSION = "sole.v1"


async def import_products(
    conn: asyncpg.Connection,
    biz: uuid.UUID,
    run: uuid.UUID,
    src: sqlite3.Connection,
    brands: dict,
    cats: dict,
    limit: int | None,
) -> tuple[dict[int, uuid.UUID], list]:
    """Produsele + varianta implicită. Întoarce maparea `sqlite id → uuid`."""
    src.row_factory = sqlite3.Row
    q = "select * from products where name is not null and price_regular is not null"
    if limit:
        q += f" limit {limit}"
    rows = src.execute(q).fetchall()

    alerts: list[tuple[Any, str, dict]] = []
    raw_batch, prod_batch, var_batch = [], [], []

    for r in rows:
        price = ss.parse_price(r["price_regular"], r["price_promo"], r["promo_code"])
        if price is None:
            continue
        sections = json.loads(r["sections_json"] or "{}")
        storage = ss.parse_storage(sections.get("Depozitare si valabilitate"))
        volume, unit = ss.parse_volume(r["volume"])
        ext = r["memox_code"] or r["sku"]
        path = ss.parse_category_path(r["category"])
        cat_slug = category_slug_for(path)

        if volume is None and (r["volume"] or "").strip():
            alerts.append((None, "volume_unparsable", {"sku": ext, "raw": r["volume"]}))
        for flag in price.anomalies:
            alerts.append(
                (
                    None,
                    flag,
                    {
                        "sku": ext,
                        "regular": r["price_regular"],
                        "promo": r["price_promo"],
                        "code": r["promo_code"],
                    },
                )
            )
        for key in sections:
            if ss.classify_section(key) is None:
                alerts.append((None, "unknown_section_key", {"sku": ext, "key": key}))

        payload = {k: r[k] for k in r.keys()}
        raw_batch.append(
            (
                biz,
                SOURCE_SITE,
                r["url"],
                json.dumps(payload, ensure_ascii=False),
                _parse_scraped_at(r["scraped_at"]),
            )
        )

        attributes = {
            "mpn": r["mpn"],
            "sku": r["sku"],
            "volume_raw": r["volume"],
            "price_per_unit_source": r["price_per_unit"],
            "routine_time": ss.parse_routine_time(sections.get("Cand se utilizeaza")),
            "compliance": [
                b for b in json.loads(r["badges"] or "[]") if ss.classify_badge(b) == "compliance"
            ],
        }
        prod_batch.append(
            (
                biz,
                ext,
                ss.content_hash(*[r[k] for k in r.keys()]),
                brands.get(ss.slugify(r["brand"])) if r["brand"] else None,
                cats.get(cat_slug) if cat_slug else None,
                r["name"],
                ss.product_slug(r["name"], ext),
                r["description"],
                r["currency"] or "RON",
                price.price,
                price.sale_price,
                price.coupon_code,
                price.coupon_price,
                ss.parse_availability(r["availability"]),
                None,  # stock_total: sursa da doar binar. UNKNOWN nu e 0.
                r["rating"] or 0,
                r["review_count"] or 0,
                "active",
                "published",
                json.dumps(attributes, ensure_ascii=False),
                r["url"],
                storage.min_durability_date,
                storage.pao_months,
                storage.temp_min_c,
                storage.temp_max_c,
                SOURCE_SITE,
                run,
                EXTRACTOR_VERSION,
            )
        )
        var_batch.append(
            (biz, ext, r["sku"], volume, unit, price.price, price.sale_price, r["ean"], r["name"])
        )

    # `do update`, nu `do nothing`: ancora lossless trebuie să reflecte ULTIMUL scrape. Cu
    # `do nothing`, un produs al cărui preț s-a schimbat la sursă ar păstra la infinit payloadul
    # din prima rulare, iar `source_fingerprint` de pe `products` ar semnala o schimbare pe care
    # ancora n-o mai poate explica.
    await conn.executemany(
        "insert into source_products_raw (business_id, source_site, source_url, payload, "
        "  scraped_at) values ($1,$2,$3,$4::jsonb, coalesce($5, now())) "
        "on conflict (business_id, source_url) do update set "
        "  payload = excluded.payload, scraped_at = excluded.scraped_at",
        raw_batch,
    )
    placeholders = ", ".join(f"${i}" for i in range(1, 29))
    await conn.executemany(
        f"insert into products ({PRODUCT_COLS}) values ({placeholders}, now()) "
        "on conflict (business_id, external_id) do update set "
        "  name=excluded.name, slug=excluded.slug, description=excluded.description, "
        "  price=excluded.price, sale_price=excluded.sale_price, "
        "  coupon_code=excluded.coupon_code, coupon_price=excluded.coupon_price, "
        "  availability=excluded.availability, rating=excluded.rating, "
        "  review_count=excluded.review_count, attributes=excluded.attributes, "
        "  brand_id=excluded.brand_id, primary_category_id=excluded.primary_category_id, "
        "  min_durability_date=excluded.min_durability_date, "
        "  storage_temp_min_c=excluded.storage_temp_min_c, "
        "  storage_temp_max_c=excluded.storage_temp_max_c, "
        "  source_fingerprint=excluded.source_fingerprint, "
        "  last_sync_run_id=excluded.last_sync_run_id, synced_at=now()",
        prod_batch,
    )
    ids = {
        r["external_id"]: r["id"]
        for r in await conn.fetch(
            "select external_id, id from products where business_id = $1", biz
        )
    }
    say(OK, f"produse: {len(prod_batch)}")

    # Varianta IMPLICITĂ: sursa n-are variante, dar `net_content` și `price_per_unit`
    # (coloană generated) trăiesc pe variantă, iar 026 declară varianta sursa de adevăr
    # comercială. Fără ea am pierde prețul per unitate, pe care sursa îl are.
    # `stock` = NULL, nu 0 — ACEEAȘI regulă ca `stock_total` la nivel de produs: sursa dă doar
    # binar (`in stoc` / `stoc epuizat`), deci cantitatea e UNKNOWN, iar UNKNOWN nu e 0.
    #
    # Aici scria literal `0`, iar consecința era măsurabilă și tăcută: `facts_provider` tratează
    # un stoc CUNOSCUT 0 pe variantă drept `out_of_stock` pentru acea variantă („faptul mai
    # specific bate faptul produsului") — și pe bună dreptate. Cu 0 scris peste tot, **2.364 din
    # cele 2.367 de produse `in_stock` erau raportate ca epuizate** către revalidarea de coș
    # (NX-237) și către faptele turului (NX-240), iar modelul vedea „stoc 0" pe un produs pe care
    # magazinul îl are. Regula fusese respectată la produs și încălcată la variantă.
    #
    # `stock` intră și în `do update`, ca o re-rulare să REPARE rândurile deja scrise: fără asta,
    # importul idempotent ar lăsa la infinit zeroul vechi.
    await conn.executemany(
        "insert into product_variants (business_id, product_id, label, sku, price, sale_price, "
        "  stock, net_content_value, net_content_unit, gtin) "
        "values ($1,$2,$3,$4,$5,$6,null,$7,$8,$9) "
        "on conflict (business_id, sku) do update set price=excluded.price, "
        "  sale_price=excluded.sale_price, stock=excluded.stock, "
        "  net_content_value=excluded.net_content_value, "
        "  net_content_unit=excluded.net_content_unit",
        [
            (biz, ids[ext], "Standard", sku, price, sale, vol, unit, ean if ean else None)
            for biz, ext, sku, vol, unit, price, sale, ean, _name in var_batch
            if ext in ids
        ],
    )
    say(OK, f"variante implicite: {len(var_batch)}")
    return ids, alerts


# ============================================================================
# Secțiuni, evidence, badge-uri, imagini, ingrediente
# ============================================================================


async def import_sections(
    conn: asyncpg.Connection, biz: uuid.UUID, src: sqlite3.Connection, ids: dict
) -> tuple[int, int]:
    """AMBELE familii. F1 devine și evidence citabil; F2 doar secțiune etichetată."""
    sections, chunks = [], []
    src.row_factory = sqlite3.Row
    for r in src.execute("select memox_code, sku, sections_json from products"):
        ext = r["memox_code"] or r["sku"]
        pid = ids.get(ext)
        if not pid:
            continue
        for pos, (key, body) in enumerate(json.loads(r["sections_json"] or "{}").items()):
            if not isinstance(body, str) or not body.strip():
                continue
            cls = ss.classify_section(key)
            kind = cls.kind if cls else "unclassified"
            source = cls.source if cls else "unknown"
            voice = cls.voice if cls else "brand"
            sections.append(
                (biz, pid, kind, key, body, pos, LOCALE, voice, source, ss.content_hash(body), key)
            )
            # `role` are vocabular ÎNCHIS în schemă (contractul NX-205). Vine din modulul pur,
            # unde e testat contra `EvidenceChunk`, nu dintr-un `kind` reciclat aici: un rol
            # inventat n-ar da eroare la clasificare, ci la INSERT, în mijlocul importului,
            # după ce produsele au fost deja scrise.
            if cls and cls.evidence_role:
                chunks.append(
                    (biz, pid, cls.evidence_role, body, SOURCE_SITE, LOCALE, ss.content_hash(body))
                )

    await conn.executemany(
        "insert into product_sections (business_id, product_id, kind, title, body, position, "
        "  locale, voice, source, content_hash, source_key) "
        "values ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11) "
        # Predicatul `where source_key is not null` NU e decorativ aici: `uq_product_sections_key`
        # e un index PARȚIAL, iar inferența din `on conflict` potrivește un index doar dacă îi
        # repetă și predicatul. Fără el: „no unique or exclusion constraint matching".
        "on conflict (business_id, product_id, locale, source, source_key) "
        "where source_key is not null "
        "do update set body=excluded.body, content_hash=excluded.content_hash",
        sections,
    )
    await conn.executemany(
        "insert into product_evidence_chunks (business_id, product_id, role, text, source, "
        "  locale, content_hash) values ($1,$2,$3,$4,$5,$6,$7) on conflict do nothing",
        chunks,
    )
    say(OK, f"sectiuni: {len(sections)} (din care evidence citabil: {len(chunks)})")
    return len(sections), len(chunks)


async def import_badges(
    conn: asyncpg.Connection, biz: uuid.UUID, src: sqlite3.Connection, ids: dict
) -> int:
    rows, seen = [], set()
    src.row_factory = sqlite3.Row
    for r in src.execute("select memox_code, sku, badges from products"):
        pid = ids.get(r["memox_code"] or r["sku"])
        if not pid:
            continue
        for pos, label in enumerate(json.loads(r["badges"] or "[]")):
            if not isinstance(label, str) or (pid, label) in seen:
                continue
            seen.add((pid, label))
            rows.append((biz, pid, label, ss.classify_badge(label), LOCALE, pos, "sole_scrape"))
    await conn.executemany(
        "insert into product_badges (business_id, product_id, label, kind, locale, position, "
        "  source) values ($1,$2,$3,$4,$5,$6,$7) "
        "on conflict (business_id, product_id, locale, label) do update set kind=excluded.kind",
        rows,
    )
    say(OK, f"badge-uri: {len(rows)}")
    return len(rows)


async def import_images(
    conn: asyncpg.Connection, biz: uuid.UUID, src: sqlite3.Connection, ids: dict
) -> int:
    """TOATE rândurile. `storage='source'` până când le găzduim noi (val separat de upload)."""
    rows = []
    src.row_factory = sqlite3.Row
    for r in src.execute(
        """select p.memox_code, p.sku, i.url, i.local_path, i.id,
                  row_number() over (partition by i.product_id order by i.id) - 1 as pos
           from images i join products p on p.id = i.product_id"""
    ):
        pid = ids.get(r["memox_code"] or r["sku"])
        if pid:
            rows.append((biz, pid, r["url"], r["url"], "source", int(r["pos"])))
    for i in range(0, len(rows), BATCH):
        await conn.executemany(
            "insert into product_images (business_id, product_id, url, source_url, storage, "
            "  position) values ($1,$2,$3,$4,$5,$6) "
            "on conflict (business_id, product_id, url) do update set position=excluded.position",
            rows[i : i + BATCH],
        )
    say(OK, f"imagini (randuri): {len(rows)}")
    return len(rows)


async def import_ingredients(
    conn: asyncpg.Connection, biz: uuid.UUID, src: sqlite3.Connection, ids: dict
) -> tuple[int, int]:
    src.row_factory = sqlite3.Row
    per_product: list[tuple[uuid.UUID, list[str]]] = []
    vocabulary: dict[str, str] = {}
    for r in src.execute("select memox_code, sku, ingredients, sections_json from products"):
        pid = ids.get(r["memox_code"] or r["sku"])
        if not pid:
            continue
        names = ss.split_inci(r["ingredients"])
        per_product.append((pid, names))
        for n in names:
            vocabulary.setdefault(ss.ingredient_slug(n), n)

    await conn.executemany(
        "insert into ingredients (business_id, name, slug, inci_name) values ($1,$2,$3,$2) "
        "on conflict (business_id, slug) do nothing",
        [(biz, name, slug) for slug, name in vocabulary.items()],
    )
    ing_ids = {
        r["slug"]: r["id"]
        for r in await conn.fetch("select slug, id from ingredients where business_id = $1", biz)
    }
    links = [
        (pid, ing_ids[ss.ingredient_slug(n)], pos)
        for pid, names in per_product
        for pos, n in enumerate(names)
        if ss.ingredient_slug(n) in ing_ids
    ]
    for i in range(0, len(links), BATCH):
        await conn.executemany(
            "insert into product_ingredients (product_id, ingredient_id, position) "
            "values ($1,$2,$3) on conflict (product_id, ingredient_id) do nothing",
            links[i : i + BATCH],
        )
    say(OK, f"ingrediente: {len(vocabulary)} distincte, {len(links)} legaturi")
    return len(vocabulary), len(links)


async def import_faqs(
    conn: asyncpg.Connection, biz: uuid.UUID, src: sqlite3.Connection, ids: dict
) -> int:
    src.row_factory = sqlite3.Row
    rows, seen = [], set()
    for r in src.execute(
        """select p.memox_code, p.sku, f.question, f.answer,
                  row_number() over (partition by f.product_id order by f.id) - 1 as pos
           from faq f join products p on p.id = f.product_id
           where f.question is not null and f.answer is not null"""
    ):
        pid = ids.get(r["memox_code"] or r["sku"])
        key = (pid, r["question"])
        if not pid or key in seen:
            continue
        seen.add(key)
        rows.append((biz, pid, LOCALE, r["question"], r["answer"], int(r["pos"]), "brand", False))
    for i in range(0, len(rows), BATCH):
        await conn.executemany(
            "insert into product_faqs (business_id, product_id, locale, question, answer, "
            "  position, source, derived) values ($1,$2,$3,$4,$5,$6,$7,$8) "
            "on conflict (business_id, product_id, locale, question) "
            "do update set answer=excluded.answer",
            rows[i : i + BATCH],
        )
    say(OK, f"FAQ per produs: {len(rows)}")
    return len(rows)


async def import_reviews(
    conn: asyncpg.Connection, biz: uuid.UUID, src: sqlite3.Connection, ids: dict
) -> int:
    """TOATE cele 183.003. Ratingul 0 devine NULL: textul e bun, nota lipsește."""
    src.row_factory = sqlite3.Row
    rows = []
    for r in src.execute(
        "select p.memox_code, p.sku, r.id, r.author, r.rating, r.text "
        "from reviews r join products p on p.id = r.product_id"
    ):
        pid = ids.get(r["memox_code"] or r["sku"])
        if not pid:
            continue
        rating = int(r["rating"]) if r["rating"] and 1 <= r["rating"] <= 5 else None
        rows.append((biz, pid, "sole_scrape", str(r["id"]), r["author"], rating, r["text"]))

    done = 0
    for i in range(0, len(rows), BATCH):
        await conn.executemany(
            "insert into reviews (business_id, product_id, source, external_id, author, rating, "
            "  body) values ($1,$2,$3,$4,$5,$6,$7) "
            "on conflict (business_id, source, external_id) do nothing",
            rows[i : i + BATCH],
        )
        done += len(rows[i : i + BATCH])
        if done % 40000 < BATCH:
            say(OK, f"  recenzii: {done}/{len(rows)}")
    say(OK, f"recenzii: {len(rows)}")
    return len(rows)


# ============================================================================


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="chiar scrie (fara el: doar numara)")
    ap.add_argument("--slug", default="sole-ro", help="slug-ul tenantului tinta")
    ap.add_argument("--limit", type=int, help="doar primele N produse (proba rapida)")
    ap.add_argument("--skip-reviews", action="store_true")
    args = ap.parse_args()

    dsn = os.environ.get("TARGET_DB_URL")
    if not dsn:
        sys.stderr.write("TARGET_DB_URL lipseste\n")
        return 2
    if not Path(SOURCE_DB).exists():
        sys.stderr.write(f"sursa lipseste: {SOURCE_DB}\n")
        return 2

    src = sqlite3.connect(f"file:{SOURCE_DB}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    counts = {
        t: src.execute(f"select count(*) from {t}").fetchone()[0]
        for t in ("products", "reviews", "faq", "images")
    }
    say(OK, f"sursa: {counts}")
    if not args.apply:
        say(WARN, "fara --apply: nu scriu nimic")
        return 0

    conn = await asyncpg.connect(dsn, statement_cache_size=0, timeout=60)
    started = time.monotonic()
    stats: dict[str, int] = {}
    run = None
    try:
        biz = await ensure_business(conn, args.slug)
        say(OK, f"tenant `{args.slug}` = {biz}")
        run = await start_run(conn, biz)

        brands = await import_brands(conn, biz, src)
        cats = await import_categories(conn, biz, src)
        ids, alerts = await import_products(conn, biz, run, src, brands, cats, args.limit)
        stats["products"] = len(ids)

        stats["sections"], stats["evidence"] = await import_sections(conn, biz, src, ids)
        stats["badges"] = await import_badges(conn, biz, src, ids)
        stats["images"] = await import_images(conn, biz, src, ids)
        stats["ingredients"], stats["ingredient_links"] = await import_ingredients(
            conn, biz, src, ids
        )
        stats["faqs"] = await import_faqs(conn, biz, src, ids)
        if not args.skip_reviews:
            stats["reviews"] = await import_reviews(conn, biz, src, ids)

        await flush_alerts(conn, biz, run, alerts)
        stats["quality_alerts"] = len(alerts)
        await finish_run(conn, run, stats, None)
    except Exception as exc:
        if run:
            await finish_run(conn, run, stats, f"{type(exc).__name__}: {exc}"[:2000])
        raise
    finally:
        src.close()
        await conn.close()

    say(OK, f"gata in {time.monotonic() - started:.0f}s")
    for k, v in stats.items():
        print(f"        {k:20s} {v:>8,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
