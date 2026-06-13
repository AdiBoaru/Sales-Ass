"""Normalizare catalog demo (rule-based, idempotent) — basics pentru un demo credibil.

Două lucruri, fără LLM, fără cost:
  1. PREȚURI: repară valorile aberante (≤0 sau > MAX_SANE) cu un preț realist pe
     bandă de categorie (determinist per produs). Actualizează și varianta.
     (La catalogul demo: practic un singur outlier de 11M lei — restul sunt ok.)
  2. product_url: generează URL de magazin din slug (acum toate sunt NULL → agentul
     n-are linkuri). Placeholder credibil al „magazinului nostru".

Rulează ca ADMIN (scrie în catalog; bot_runtime n-are voie). Idempotent: prețurile
deja în interval și URL-urile deja setate sunt sărite.

    python scripts/normalize_catalog.py            # aplică
    python scripts/normalize_catalog.py --dry-run  # doar raportează
"""

import asyncio
import os
import socket
import ssl
import sys
from random import Random
from urllib.parse import unquote, urlparse

import asyncpg
from dotenv import load_dotenv

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    sys.stdout.reconfigure(encoding="utf-8")

load_dotenv(".env")

DSN = os.environ.get("SUPABASE_DB_URL") or os.environ.get("DATABASE_URL")
BIZ = "6098812a-50fc-44bd-a1ba-bc77e6399158"
STORE_BASE = "https://shop.sole-demo.ro/p"
MAX_SANE = 1500.0  # peste atât = aberant la beauty → reasignăm

# Benzi de preț (lei) pe tip de produs — keyword în nume/categorie → (min, max).
_BANDS: list[tuple[tuple[str, ...], tuple[int, int]]] = [
    (("aparat", "dispozitiv", "device", "perie electric"), (80, 550)),
    (("parfum", "apa de toaleta", "eau de"), (90, 400)),
    (("ser", "serum"), (60, 250)),
    (("crema", "cremă"), (45, 220)),
    (("masca", "mască"), (30, 130)),
    (("toner", "lotiune", "lotiune", "apa micelara", "tonic"), (35, 110)),
    (("sampon", "balsam", "par", "păr"), (25, 95)),
    (("ruj", "fond", "pudra", "mascara", "creion", "machiaj", "fard", "luciu"), (25, 140)),
    (("pensula", "burete", "accesoriu", "aplicator"), (15, 90)),
]
_DEFAULT_BAND = (30, 180)


def _band(name: str, category: str) -> tuple[int, int]:
    hay = f"{name} {category}".lower()
    for keys, band in _BANDS:
        if any(k in hay for k in keys):
            return band
    return _DEFAULT_BAND


def realistic_price(product_id: str, name: str, category: str) -> float:
    """Preț determinist (același la re-rulare) într-o bandă de categorie, terminat în .99."""
    lo, hi = _band(name, category or "")
    rng = Random(product_id)  # seed pe id → reproductibil
    return float(rng.randint(lo, hi)) - 0.01


async def _connect() -> asyncpg.Connection:
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


async def main() -> None:
    if not DSN:
        sys.exit("SUPABASE_DB_URL lipsește în .env")
    dry = "--dry-run" in sys.argv
    conn = await _connect()
    try:
        # 1. PREȚURI aberante
        bad = await conn.fetch(
            """
            select p.id::text as id, p.name,
                   coalesce(c.name, '') as category, p.price::float8 as price
            from products p
            left join categories c on c.id = p.primary_category_id
            where p.business_id = $1 and (p.price is null or p.price <= 0 or p.price > $2)
            """,
            BIZ,
            MAX_SANE,
        )
        print(f"Prețuri de reparat (≤0 sau > {MAX_SANE:.0f}): {len(bad)}")
        for r in bad:
            new = realistic_price(r["id"], r["name"], r["category"])
            print(f"  {r['price']} → {new:.2f}  | {r['name'][:45]}")
            if not dry:
                await conn.execute(
                    "update products set price = $2 where business_id = $1 and id = $3",
                    BIZ,
                    new,
                    r["id"],
                )
                await conn.execute(
                    "update product_variants set price = $2, sale_price = null "
                    "where business_id = $1 and product_id = $3 "
                    "and (price is null or price > $4)",
                    BIZ,
                    new,
                    r["id"],
                    MAX_SANE,
                )

        # 2. product_url lipsă
        missing = await conn.fetch(
            """select id::text as id, slug from products
               where business_id = $1 and product_url is null""",
            BIZ,
        )
        print(f"\nproduct_url de generat: {len(missing)}")
        if not dry:
            for r in missing:
                slug = r["slug"] or r["id"]
                await conn.execute(
                    "update products set product_url = $2 where business_id = $1 and id = $3",
                    BIZ,
                    f"{STORE_BASE}/{slug}",
                    r["id"],
                )
        if missing:
            ex = missing[0]
            print(f"  ex: {STORE_BASE}/{ex['slug'] or ex['id']}")

        print("\n" + ("DRY-RUN (nimic scris)." if dry else "Aplicat."))
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
