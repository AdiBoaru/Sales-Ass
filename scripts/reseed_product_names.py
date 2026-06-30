"""Val2 (CONV-COMMERCE) — curăță ID-ul rezidual de seed din `products.name` (demo).

Gap din analiză: nume cu ID rezidual („…348", „…053"). Restul Val2 (rating variat, product_url,
review_count, concerns) e DEJA re-seedat — verificat 2026-06-30: rating 4.3–4.9, url_null=0,
concerns_empty=0. A rămas DOAR coada numerică de ID în NUME (slug-ul + product_url sunt corecte și
NU se ating — sunt chei stabile).

Identificarea e PRECISĂ (nu „strip orice număr de la final"): ID-ul rezidual e segmentul numeric
din slug ÎNAINTE de hash-ul de 8 hex (`-<id>-<hash8>$`). Ștergem din NUME exact acel ID dacă e la
coadă → un „SPF 50" legitim (fără tiparul de slug) rămâne neatins. Idempotent: după curățare numele
nu mai are coada → re-rularea e no-op.

Implicit DRY-RUN (doar previzualizare). Scrie efectiv DOAR cu `--apply`.

    python scripts/reseed_product_names.py            # dry-run (preview)
    python scripts/reseed_product_names.py --apply    # aplică UPDATE-urile
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys

# Rulat ca `python scripts/reseed_product_names.py` → adaugă repo root pe sys.path pt `import src`.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DEMO_BIZ = "6098812a-50fc-44bd-a1ba-bc77e6399158"

# ID-ul rezidual = numărul din slug înaintea hash-ului final de 8 hex (ex. `...-348-474c8dae`).
_SLUG_ID_RE = re.compile(r"-(\d+)-[0-9a-f]{8}$")


def residual_id(slug: str | None) -> str | None:
    """ID-ul rezidual de seed din slug, sau None dacă slug-ul nu are tiparul `-<id>-<hash8>`."""
    m = _SLUG_ID_RE.search(slug or "")
    return m.group(1) if m else None


def clean_name(name: str, slug: str | None) -> str:
    """Numele fără coada de ID rezidual (PRECIS, derivat din slug). Idempotent. Dacă numele nu se
    termină cu acel ID (deja curat / nume diferit), îl întoarce neschimbat. Nu golește numele."""
    sid = residual_id(slug)
    if not sid:
        return name
    cleaned = re.sub(rf"\s+{re.escape(sid)}\s*$", "", name).rstrip()
    return cleaned or name


async def run(*, apply: bool, business_id: str) -> int:
    """Întoarce numărul de nume care s-ar schimba (dry-run) / s-au schimbat (--apply)."""
    from src.db.connection import admin_conn, close_pool, get_pool

    pool = await get_pool()
    changed = 0
    samples: list[tuple[str, str]] = []
    try:
        async with admin_conn(pool) as conn:
            rows = await conn.fetch(
                "select id::text, name, slug from products where business_id = $1", business_id
            )
            updates: list[tuple[str, str]] = []  # (id, new_name)
            for r in rows:
                new = clean_name(r["name"], r["slug"])
                if new != r["name"]:
                    updates.append((r["id"], new))
                    if len(samples) < 10:
                        samples.append((r["name"], new))
            changed = len(updates)
            print(f"produse: {len(rows)} | nume de curățat: {changed}")
            for old, new in samples:
                print(f"  {old!r}\n   → {new!r}")
            if apply and updates:
                async with conn.transaction():
                    await conn.executemany(
                        "update products set name = $2 where business_id = $3 and id = $1",
                        [(pid, new, business_id) for pid, new in updates],
                    )
                print(f"APLICAT: {changed} nume actualizate.")
            elif updates:
                print("DRY-RUN (fără scriere). Rulează cu --apply ca să aplici.")
            else:
                print("Nimic de curățat (idempotent / deja curate).")
    finally:
        await close_pool()
    return changed


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Curăță ID-ul rezidual de seed din products.name (demo)."
    )
    ap.add_argument(
        "--apply", action="store_true", help="aplică UPDATE-urile (altfel doar dry-run)"
    )
    ap.add_argument("--business-id", default=DEMO_BIZ, help="tenant (default: demo)")
    args = ap.parse_args()
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(run(apply=args.apply, business_id=args.business_id))


if __name__ == "__main__":
    main()
