"""Populează `product_images` cu poze REALE beauty per categorie (demo MVP).

Catalogul demo e fictiv: cele 2500 de imagini existente sunt placeholdere text
(`placehold.co/...?text=NUME`) — arată fals în orice UI. Pentru MVP vrem poze
care CORESPUND tipului de produs (un „ser" arată un ser, o „cremă de zi" o cremă)
și sunt ASEMĂNĂTOARE în interiorul categoriei — adică reprezentative, nu fotografia
exactă a fiecărui SKU fictiv.

Strategie:
  • mapăm fiecare categorie primară (slug RO) → un query EN pentru Pexels
    (ex. seruri-pentru-ten → "face serum bottle");
  • o singură căutare Pexels per categorie → un POOL de ~15 poze reale;
  • fiecare produs primește N imagini (default 3) rotind prin pool-ul categoriei
    după indexul produsului → seruri vecine NU arată pixel-identic, dar rămân
    on-topic. Stocăm URL-ul CDN Pexels (licența permite hot-link), fără descărcare.

Idempotent: înlocuiește DOAR imaginile placeholder (`url like '%placehold.co%'`).
Produsele care au deja poze Pexels sunt sărite (re-rulare gratuită) dacă nu dai
`--force`. Catalogul e read-only pentru bot → rulăm ca ADMIN (SUPABASE_DB_URL),
scoped pe business_id. Necesită PEXELS_API_KEY.

    python scripts/seed_product_images.py --limit 5 --dry-run   # vezi ce ar face
    python scripts/seed_product_images.py --limit 5             # test pe 5 produse
    python scripts/seed_product_images.py                       # toate (placeholder)
    python scripts/seed_product_images.py --force               # rescrie TOT
"""

import argparse
import asyncio
import os
import socket
import ssl
import sys
from collections import defaultdict
from urllib.parse import unquote, urlparse

import asyncpg
import httpx
from dotenv import load_dotenv

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    sys.stdout.reconfigure(encoding="utf-8")

load_dotenv()

DSN = os.environ.get("SUPABASE_DB_URL") or os.environ.get("DATABASE_URL")
PEXELS_API_KEY = os.environ.get("PEXELS_API_KEY")
BIZ = "6098812a-50fc-44bd-a1ba-bc77e6399158"

IMAGES_PER_PRODUCT = 3
POOL_SIZE = 15  # poze cerute de la Pexels per categorie (per_page)

# slug categorie primară (RO, din catalog.json) → query Pexels (EN, produs-centric).
# Țintim fotografii de PRODUS, nu lifestyle: cuvinte ca "bottle", "jar", "tube".
CATEGORY_QUERY = {
    "seruri-pentru-ten": "face serum bottle dropper",
    "seruri-de-noapte": "night face serum bottle",
    "masti-pentru-ten": "facial sheet mask skincare",
    "masti-de-par": "hair mask jar",
    "creme-de-zi": "face cream jar skincare",
    "creme-de-noapte": "night cream jar",
    "creme-de-corp": "body cream jar",
    "creme-de-maini": "hand cream tube",
    "creme-bb-si-cc": "bb cream cosmetic tube",
    "creme-si-geluri-pentru-ochi": "eye cream jar",
    "curatarea-tenului": "facial cleanser bottle",
    "demachiante-pentru-ten": "makeup remover bottle",
    "exfolierea-tenului": "face scrub exfoliator",
    "lotiuni-tonice": "facial toner bottle",
    "lotiuni-de-corp": "body lotion bottle",
    "mist-pentru-fata": "face mist spray bottle",
    "uleiuri-pentru-ten": "facial oil bottle",
    "uleiuri-pentru-par": "hair oil bottle",
    "ingrijire-pentru-zona-buzelor": "lip care balm",
    "remediu-local": "acne spot treatment",
    "pensule-si-bureti-de-machiaj": "makeup brushes set",
    "accesorii": "beauty accessories cosmetics",
    "accesorii-pentru-par": "hair brush comb",
    "accesorii-de-masaj-pentru-fata": "facial roller gua sha",
    "aparate-ingrijire": "facial cleansing device",
    "perie-pentru-curatarea-tenului": "facial cleansing brush",
    "protectie-solara": "sunscreen bottle",
    "creme-de-protectie-solara-pentru-corp": "body sunscreen lotion",
    "creme-de-protectie-solara-pentru-fata": "face sunscreen bottle",
    "creme-cu-spf-pentru-fata-si-corp": "spf sunscreen cream",
    "creme-de-protectie-solara-pentru-copii-cu-spf-ridicat": "kids sunscreen bottle",
    "sampoane": "shampoo bottle",
    "balsamuri": "hair conditioner bottle",
    "ingrijire-fara-clatire": "leave in hair care spray",
    "spume-si-spray-uri-pentru-volum": "hair volume spray",
    "geluri-de-dus": "shower gel bottle",
    "produse-pentru-baie": "bath products bubble",
    "sapunuri-lichide": "liquid soap bottle",
    "deodorant": "deodorant stick",
    "deodorante-si-antiperspirante": "deodorant antiperspirant",
    "rujuri": "lipstick cosmetic",
    "gloss-uri-de-buze": "lip gloss",
    "balsam-pentru-buze": "lip balm",
    "farduri-de-ochi": "eyeshadow palette",
    "palete": "makeup palette",
    "cushion": "cushion foundation makeup",
    "iluminatoare": "highlighter makeup",
    "creioane-pentru-sprancene": "eyebrow pencil",
    "rimeluri-si-geluri-pentru-sprancene": "mascara cosmetic",
    "seturi-cosmetice": "cosmetics gift set",
    "creme-autobronzante-si-bronzere": "self tanning cream",
    "gel-autobronzant": "self tanner gel bottle",
    "spuma-autobronzanta": "self tanning mousse",
    "spray-autobronzant": "self tanning spray",
    "mist-autobronzant": "tanning mist spray",
    "manusa-pentru-aplicarea-auto-bronzantului": "tanning applicator mitt",
    "absorbante": "sanitary pads package",
    "chiloti-menstruali": "period underwear",
}
FALLBACK_QUERY = "cosmetic beauty product"


async def connect() -> asyncpg.Connection:
    p = urlparse(DSN)
    ip = socket.getaddrinfo(p.hostname, p.port or 5432, socket.AF_INET, socket.SOCK_STREAM)[0][4][0]
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return await asyncpg.connect(
        host=ip,
        port=p.port or 5432,
        user=unquote(p.username),
        password=unquote(p.password),
        database=(p.path or "/postgres").lstrip("/"),
        ssl=ctx,
    )


async def fetch_pexels_pool(client: httpx.AsyncClient, query: str) -> list[str]:
    """Întoarce până la POOL_SIZE URL-uri de poze pentru un query. [] la eșec (NU crapă)."""
    try:
        r = await client.get(
            "https://api.pexels.com/v1/search",
            params={"query": query, "per_page": POOL_SIZE, "orientation": "portrait"},
            headers={"Authorization": PEXELS_API_KEY},
            timeout=20,
        )
        r.raise_for_status()
        photos = r.json().get("photos", [])
        # src.large = ~940px lățime — bun pentru card de produs, fără să fie greu.
        return [p["src"]["large"] for p in photos if p.get("src", {}).get("large")]
    except Exception as e:  # noqa: BLE001 — script one-off, vrem să continuăm pe alte categorii
        print(f"  ⚠️  Pexels eșec pentru «{query}»: {e}")
        return []


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="doar primele N produse (test)")
    ap.add_argument("--force", action="store_true", help="rescrie TOT, nu doar placeholder")
    ap.add_argument("--per-product", type=int, default=IMAGES_PER_PRODUCT, help="imagini/produs")
    ap.add_argument("--dry-run", action="store_true", help="nu scrie, doar raportează")
    args = ap.parse_args()

    if not DSN:
        print("EROARE: SUPABASE_DB_URL lipsește din .env")
        sys.exit(2)
    if not PEXELS_API_KEY:
        print("EROARE: PEXELS_API_KEY lipsește din .env (ia o cheie gratuită de pe pexels.com/api)")
        sys.exit(2)

    conn = await connect()
    try:
        # Produs + slug categorie primară. Ordonăm pe categorie apoi nume → indexul
        # din categorie e stabil (rotația prin pool e deterministă, re-rulabilă).
        rows = await conn.fetch(
            """
            select p.id, p.name, coalesce(c.slug, '') as cat_slug,
                   exists(
                     select 1 from product_images pi
                     where pi.product_id = p.id and pi.url not like '%placehold.co%'
                   ) as has_real
            from products p
            left join categories c on c.id = p.primary_category_id
            where p.business_id = $1
            order by c.slug nulls last, p.name
            """,
            BIZ,
        )
        if args.limit:
            rows = rows[: args.limit]

        # Selectăm produsele de procesat. Default: cele FĂRĂ poză reală. --force: toate.
        todo = [r for r in rows if args.force or not r["has_real"]]
        skipped = len(rows) - len(todo)
        if not todo:
            print(f"Nimic de făcut: toate {len(rows)} produsele au poze reale (--force rescrie).")
            return

        # Câte poole-uri de categorie ne trebuie → o căutare Pexels per categorie distinctă.
        cats = sorted({r["cat_slug"] for r in todo})
        print(f"{len(todo)} produse de procesat ({skipped} sărite), {len(cats)} categorii.")

        pools: dict[str, list[str]] = {}
        async with httpx.AsyncClient() as client:
            for cat in cats:
                query = CATEGORY_QUERY.get(cat, FALLBACK_QUERY)
                pool = await fetch_pexels_pool(client, query)
                pools[cat] = pool
                print(f"  {cat or '(fără categorie)':50s} ← «{query}»: {len(pool)} poze")

        # Index per categorie (rotația prin pool) — rows e ordonat pe categorie.
        cat_idx: dict[str, int] = defaultdict(int)
        to_insert: list[tuple] = []  # (product_id, url, alt, position)
        no_pool = 0
        for r in todo:
            pool = pools.get(r["cat_slug"]) or []
            if not pool:
                no_pool += 1
                continue
            base = cat_idx[r["cat_slug"]]
            cat_idx[r["cat_slug"]] += 1
            n = min(args.per_product, len(pool))
            for pos in range(n):
                url = pool[(base * args.per_product + pos) % len(pool)]
                to_insert.append((r["id"], url, r["name"], pos))

        affected_products = len({t[0] for t in to_insert})
        print(
            f"\nPregătite {len(to_insert)} imagini pentru {affected_products} produse"
            + (f" ({no_pool} fără pool Pexels → lăsate cu placeholder)" if no_pool else "")
        )

        if args.dry_run:
            print("\n--dry-run: NU scriu nimic. Exemple:")
            for t in to_insert[:6]:
                print(f"  {t[2][:40]:40s} pos={t[3]}  {t[1]}")
            return

        product_ids = list({t[0] for t in to_insert})
        async with conn.transaction():
            if args.force:
                # rescrie tot: șterge orice imagine a produselor atinse
                await conn.execute(
                    "delete from product_images where product_id = any($1::uuid[])",
                    product_ids,
                )
            else:
                # idempotent: scoatem DOAR placeholderele produselor atinse
                await conn.execute(
                    "delete from product_images where product_id = any($1::uuid[]) "
                    "and url like '%placehold.co%'",
                    product_ids,
                )
            await conn.executemany(
                "insert into product_images (product_id, url, alt, position) "
                "values ($1, $2, $3, $4)",
                to_insert,
            )

        total_imgs = await conn.fetchval(
            "select count(*) from product_images pi join products p on p.id = pi.product_id "
            "where p.business_id = $1 and pi.url like '%pexels%'",
            BIZ,
        )
        print(
            f"\nOK ✓ — {len(to_insert)} imagini reale scrise pe {affected_products} produse. "
            f"Total poze Pexels în catalog: {total_imgs}."
        )
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
