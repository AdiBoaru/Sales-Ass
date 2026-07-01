"""Îmbogățește products.attributes.key_ingredients pe catalogul demo — DETERMINIST din INCI.

De ce: fațeta „Ingrediente cheie" din tabelul de comparație (Tier 2 IZI-parity) are nevoie de
ingredientele HERO (marketing-relevante) per produs, nu lista INCI completă (50+). Le derivăm
determinist: intersectăm lista INCI reală (tabelele `ingredients` + `product_ingredients`) cu o
hartă curată RO→markeri INCI. Zero LLM, zero halucinație — un ingredient apare DOAR dacă chiar e
în formula produsului. Idempotent (nu re-scrie dacă valoarea e identică), reversibil (`--revert`).

    python scripts/enrich_key_ingredients.py            # DRY-RUN (preview, nu scrie)
    python scripts/enrich_key_ingredients.py --apply    # scrie key_ingredients în attributes
    python scripts/enrich_key_ingredients.py --revert    # șterge cheia key_ingredients

Hartă HERO: ordinea = PRIORITATEA de afișare (actives recunoscute întâi); cap 4 / produs.
Excludem umectanții ubicui (glicerină/apă) — nu diferențiază. Markerii sunt substring-uri pe
numele INCI normalizat (lower) → prind variantele („Sodium Hyaluronate"/„Hydrolyzed Hyaluronic
Acid" = acid hialuronic).
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
MAX_PER_PRODUCT = 4

# RO (afișat) → markeri INCI (substring, lower). Ordine = prioritate. Actives diferențiatori întâi.
HERO_INGREDIENTS: list[tuple[str, list[str]]] = [
    ("acid hialuronic", ["hyaluron"]),
    ("niacinamidă", ["niacinamide"]),
    ("retinol", ["retinol", "retinyl", "retinal"]),
    ("vitamina C", ["ascorbic", "ascorbyl", "ascorbat"]),
    ("ceramide", ["ceramide"]),
    ("peptide", ["peptide"]),
    ("centella", ["centella", "madecassoside", "asiaticoside"]),
    ("acid salicilic", ["salicylic"]),
    ("acid glicolic/lactic (AHA)", ["glycolic acid", "lactic acid", "mandelic"]),
    ("acid azelaic", ["azelaic"]),
    ("ferment de orez/galactomyces", ["galactomyces", "rice ferment", "lactobacillus ferment"]),
    ("colagen", ["collagen"]),
    ("cofeină", ["caffeine"]),
    ("squalane", ["squalane", "squalene"]),
    ("unt de shea", ["butyrospermum", "shea butter"]),
    ("ulei de jojoba", ["jojoba"]),
    ("aloe vera", ["aloe barbadensis", "aloe vera"]),
    ("ceai verde", ["camellia sinensis"]),
    ("panthenol", ["panthenol"]),
    ("vitamina E", ["tocopherol", "tocopheryl"]),
    ("alantoină", ["allantoin"]),
]


def hero_for(ingredient_names: list[str]) -> list[str]:
    """Ingredientele HERO (RO) prezente în lista INCI, în ordinea de prioritate, cap MAX."""
    blob = " | ".join(ingredient_names)  # deja lower din SQL
    out: list[str] = []
    for ro_name, markers in HERO_INGREDIENTS:
        if any(m in blob for m in markers):
            out.append(ro_name)
            if len(out) >= MAX_PER_PRODUCT:
                break
    return out


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


async def revert(conn: asyncpg.Connection) -> None:
    res = await conn.execute(
        "update products set attributes = attributes - 'key_ingredients' "
        "where business_id = $1 and attributes ? 'key_ingredients'",
        BIZ,
    )
    print(f"REVERT: {res} (key_ingredients șters)")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="scrie în DB (altfel dry-run)")
    ap.add_argument("--revert", action="store_true", help="șterge cheia key_ingredients")
    ap.add_argument("--examples", type=int, default=12)
    args = ap.parse_args()
    if not DSN:
        sys.exit("SUPABASE_DB_URL lipsește în .env")

    conn = await _connect()
    try:
        if args.revert:
            await revert(conn)
            return

        rows = await conn.fetch(
            """
            select p.id::text as id, p.name as name,
                   coalesce(
                     array_agg(lower(i.name)) filter (where i.name is not null), '{}'
                   ) as ingredients,
                   p.attributes->'key_ingredients' as current_ki
            from products p
            left join product_ingredients pi on pi.product_id = p.id
            left join ingredients i on i.id = pi.ingredient_id
            where p.business_id = $1 and p.status = 'active'
            group by p.id, p.name, p.attributes
            order by p.name
            """,
            BIZ,
        )

        planned: list[tuple[str, list[str]]] = []  # (id, hero) de scris (diferit de actual)
        n_with = n_empty = n_unchanged = 0
        examples: list[tuple[str, list[str]]] = []
        for r in rows:
            hero = hero_for(list(r["ingredients"]))
            if hero:
                n_with += 1
            else:
                n_empty += 1
            current = json.loads(r["current_ki"]) if r["current_ki"] else None
            if hero and current == hero:
                n_unchanged += 1
                continue
            if hero:
                planned.append((r["id"], hero))
                if len(examples) < args.examples:
                    examples.append((r["name"], hero))

        total = len(rows)
        print(f"=== ENRICH key_ingredients — {total} produse active ===")
        print(f"cu hero găsit: {n_with} | fără (INCI sărac/necunoscut): {n_empty}")
        print(f"deja la zi (idempotent): {n_unchanged} | de scris: {len(planned)}\n")
        print(f"--- exemple (primele {len(examples)}) ---")
        for name, hero in examples:
            print(f"  „{name[:46]}”  →  {', '.join(hero)}")

        if not args.apply:
            print("\nDRY-RUN (nimic scris). Rulează cu --apply ca să scrii.")
            return

        async with conn.transaction():
            for pid, hero in planned:
                await conn.execute(
                    "update products set attributes = jsonb_set("
                    "coalesce(attributes, '{}'::jsonb), '{key_ingredients}', $2::jsonb, true) "
                    "where id = $1::uuid and business_id = $3",
                    pid,
                    json.dumps(hero, ensure_ascii=False),
                    BIZ,
                )
        print(f"\nAPLICAT: {len(planned)} produse actualizate. Reversibil: --revert.")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
