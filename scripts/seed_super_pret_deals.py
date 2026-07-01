"""Seed „Super Preț" (izi-parity web widget, R9) — introduce o MINORITATE realistă de reduceri
adânci (≥20%) în catalogul demo, ca badge-ul „Super Preț" (deal ≥ prag) să apară pe carduri.

Context (verificat live 2026-07-01): catalogul demo are DEJA prețuri tăiate (≈339 produse, reducere
medie ~16%) și badge-ul „Top Favorit" apare (rating≥4.7 & ≥50 recenzii, ~183 produse). CE LIPSEA:
ZERO produse cu reducere ≥20% → badge-ul „Super Preț" nu se aprindea NICIODATĂ. iZi arată „Super
Preț" pe o minoritate de produse; asta face paritatea.

Ce face: alege DETERMINIST (hash pe product_id) o fracție (default 20%) din produsele ACTIVE și le
adâncește reducerea la un procent ≥20% (din setul {20,22,25,28,30,33,35}). Setează ȘI
`products.sale_price` ȘI `product_variants.sale_price` (prețul de card vine din variantă:
`min(coalesce(v.sale_price, v.price))`) → reducere consecventă cu prețul tăiat (list_price=p.price).
Restul produselor rămân NEATINSE (reducerile lor de 10-19% + badge-urile „Top Favorit" rămân).

Idempotent + REVERSIBIL: marchează fiecare produs atins în `attributes._seed_deal` cu valorile
ORIGINALE (sale_price produs + pe variantă) → `--revert` le restaurează EXACT. Re-rularea `--apply`
sare produsele deja marcate (no-op).

Implicit DRY-RUN. Scrie doar cu `--apply`; anulează cu `--revert`.

    python scripts/seed_super_pret_deals.py                 # dry-run (preview)
    python scripts/seed_super_pret_deals.py --apply         # aplică reducerile adânci
    python scripts/seed_super_pret_deals.py --revert         # restaurează prețurile originale
    python scripts/seed_super_pret_deals.py --fraction 15    # tunează procentul de produse atinse
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys

# Rulat ca `python scripts/seed_super_pret_deals.py` → repo root pe sys.path pt `import src`.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DEMO_BIZ = "6098812a-50fc-44bd-a1ba-bc77e6399158"

# Reduceri ADÂNCI (≥20% = pragul default „Super Preț" din badges.py deal_discount_pct). Toate peste
# prag ⇒ orice produs selectat capătă badge-ul. Variate → nu par toate identice.
_DEEP_DISCOUNTS = (20, 22, 25, 28, 30, 33, 35)

# Cheia de marcare în products.attributes (reversibilitate + idempotență).
_MARK = "_seed_deal"


def _hash(pid: str) -> int:
    """Hash STABIL pe id (nu Math.random) → selecție + procent deterministe și reproductibile."""
    return int(hashlib.sha1(pid.encode()).hexdigest()[:12], 16)


def _selected(pid: str, fraction: int) -> bool:
    """~`fraction`% din produse, determinist (bucket pe hash)."""
    return (_hash(pid) % 100) < fraction


def _discount_pct(pid: str) -> int:
    """Procentul de reducere adânc, determinist per produs (≥20 ⇒ Super Preț)."""
    return _DEEP_DISCOUNTS[(_hash(pid) // 100) % len(_DEEP_DISCOUNTS)]


async def _apply(conn, business_id: str, fraction: int, *, write: bool) -> tuple[int, int]:
    """Adâncește reducerea pe subsetul selectat. Întoarce (nou_atinse, deja_seedate)."""
    prods = await conn.fetch(
        """
        select id::text, price::float8 as price, sale_price::float8 as sale_price,
               coalesce(attributes, '{}'::jsonb) as attributes
        from products
        where business_id = $1 and status = 'active'
        """,
        business_id,
    )
    already = 0
    # (pid, price, old_sale, pct, factor, new_sale)
    picked: list[tuple[str, float, float | None, int, float, float]] = []
    for p in prods:
        pid = p["id"]
        attrs = p["attributes"]
        if isinstance(attrs, str):
            attrs = json.loads(attrs)
        if _MARK in attrs:  # deja seedat → idempotent (nu re-marca, nu pierde originalul)
            already += 1
            continue
        if not _selected(pid, fraction):
            continue
        pct = _discount_pct(pid)
        factor = 1 - pct / 100.0
        new_p_sale = round(p["price"] * factor, 2)
        if new_p_sale <= 0 or new_p_sale >= p["price"]:
            continue
        picked.append((pid, p["price"], p["sale_price"], pct, factor, new_p_sale))

    for pid, price, old, pct, _factor, new in picked[:12]:
        old_s = f"{old:.2f}" if old is not None else "—"
        print(f"  {pid[:8]} price {price:.2f} | sale {old_s} → {new:.2f} (-{pct}%)")
    if len(picked) > 12:
        print(f"  … și încă {len(picked) - 12}")

    if write and picked:
        async with conn.transaction():
            for pid, _price, _old, pct, factor, new in picked:
                variants = await conn.fetch(
                    "select id::text, sale_price::float8 as sale_price "
                    "from product_variants where product_id = $1 and business_id = $2",
                    pid,
                    business_id,
                )
                # marcaj cu ORIGINALELE (pt revert exact): sale_price produs + pe fiecare variantă
                marker = {
                    "v": 1,
                    "pct": pct,
                    "ps": _old,
                    "vs": {v["id"]: v["sale_price"] for v in variants},
                }
                await conn.execute(
                    "update products set sale_price = $2, "
                    "attributes = coalesce(attributes,'{}'::jsonb) || $3::jsonb "
                    "where id = $1 and business_id = $4",
                    pid,
                    new,
                    json.dumps({_MARK: marker}),
                    business_id,
                )
                # prețul de card vine din variantă: scalăm fiecare variantă cu ACELAȘI factor
                await conn.execute(
                    "update product_variants set sale_price = round((price * $2)::numeric, 2) "
                    "where product_id = $1 and business_id = $3",
                    pid,
                    factor,
                    business_id,
                )
    return len(picked), already


async def _revert(conn, business_id: str, *, write: bool) -> int:
    """Restaurează sale_price (produs + variante) din marcaj și șterge marcajul."""
    prods = await conn.fetch(
        f"""
        select id::text, attributes->'{_MARK}' as marker
        from products
        where business_id = $1 and attributes ? '{_MARK}'
        """,
        business_id,
    )
    if write and prods:
        async with conn.transaction():
            for p in prods:
                marker = p["marker"]
                if isinstance(marker, str):
                    marker = json.loads(marker)
                await conn.execute(
                    f"update products set sale_price = $2, attributes = attributes - '{_MARK}' "
                    "where id = $1 and business_id = $3",
                    p["id"],
                    marker.get("ps"),
                    business_id,
                )
                for vid, old_sale in (marker.get("vs") or {}).items():
                    await conn.execute(
                        "update product_variants set sale_price = $2 "
                        "where id = $1 and business_id = $3",
                        vid,
                        old_sale,
                        business_id,
                    )
    return len(prods)


async def run(*, apply: bool, revert: bool, fraction: int, business_id: str) -> None:
    from src.db.connection import admin_conn, close_pool, get_pool

    pool = await get_pool()
    try:
        async with admin_conn(pool) as conn:
            if revert:
                n = await _revert(conn, business_id, write=apply)
                verb = "ar fi restaurate (dry-run)" if not apply else "RESTAURATE"
                print(f"REVERT: {n} produse {verb}.")
                return
            new, already = await _apply(conn, business_id, fraction, write=apply)
            tag = "DRY-RUN" if not apply else "APLICAT"
            suffix = " Rulează cu `--apply`." if not apply else ""
            print(f"{tag}: {new} produse «Super Preț» (≥20%); {already} deja seedate.{suffix}")
    finally:
        await close_pool()


def main() -> None:
    ap = argparse.ArgumentParser(description="Seed «Super Preț»: reduceri ≥20% pe o minoritate.")
    ap.add_argument("--apply", action="store_true", help="scrie efectiv (altfel dry-run)")
    ap.add_argument("--revert", action="store_true", help="restaurează prețurile originale")
    ap.add_argument("--fraction", type=int, default=20, help="%% produse active atinse (def. 20)")
    ap.add_argument("--business-id", default=DEMO_BIZ, help="tenant (default: demo)")
    args = ap.parse_args()
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(
        run(
            apply=args.apply,
            revert=args.revert,
            fraction=args.fraction,
            business_id=args.business_id,
        )
    )


if __name__ == "__main__":
    main()
