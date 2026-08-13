"""NX-234 — acoperirea faptelor de catalog pentru contextul de pagină (READ-ONLY).

De ce există: contextul poate ancora un produs, dar nu garantează că faptele necesare există.
Înainte de a lăsa evidence-ul în prompt (pasul 2 al rolloutului), măsurăm pe catalogul REAL —
nu pe fixture-uri de frontend, nu pe ce spune modelul, nu pe câmpuri vechi din conversation state.

Zero scrieri, zero OpenAI. Un singur query agregat per tenant.

    python scripts/web_context_coverage.py --business-id <uuid>
    python scripts/web_context_coverage.py --business-id <uuid> --json

Citire: `docs/WEB-CONTEXT-DATA-READINESS.md` §2. O acoperire mică nu blochează cardul — blochează
CLAIMUL dependent (NX-240 omite afirmația și CTA-ul), fiindcă alternativa e să-l inventăm.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Consola Windows e cp1252 by default: fara asta, un „ș" din raport
# omoara rularea cu UnicodeEncodeError.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from src.db.connection import admin_conn, close_pool, get_pool  # noqa: E402

# Fiecare linie = un fapt din matricea de data-readiness + predicatul lui de „e cunoscut".
# `rating` are gardul pe `review_count`: `products.rating` are `default 0`, deci fără el un produs
# fără recenzii ar arăta ca unul evaluat cu zero (vezi `_product_evidence`).
_FACTS: dict[str, str] = {
    "price": "p.price is not null",
    "url": "nullif(trim(p.product_url), '') is not null",
    "image": "exists (select 1 from product_images i where i.product_id = p.id)",
    "rating": "coalesce(p.review_count, 0) > 0",
    "review_summary": (
        "exists (select 1 from product_review_summaries s"
        " where s.product_id = p.id and nullif(trim(s.summary), '') is not null)"
    ),
    "delivery_class": "nullif(trim(p.delivery_class), '') is not null",
    "stock_total": "p.stock_total is not null",
    "variant_price": (
        "exists (select 1 from product_variants v"
        " where v.product_id = p.id and v.business_id = p.business_id)"
    ),
}

_FRESHNESS = {
    "fresh_lt_24h": "coalesce(p.synced_at, p.updated_at) > now() - interval '24 hours'",
    "stale_1_7d": (
        "coalesce(p.synced_at, p.updated_at) <= now() - interval '24 hours'"
        " and coalesce(p.synced_at, p.updated_at) > now() - interval '7 days'"
    ),
    "stale_gt_7d": "coalesce(p.synced_at, p.updated_at) <= now() - interval '7 days'",
    "no_timestamp": "coalesce(p.synced_at, p.updated_at) is null",
}


def _coverage_sql() -> str:
    """Un singur query: acoperire + prospețime, grupate pe categoria RĂDĂCINĂ.

    Rădăcina, nu categoria frunză: „lipsește delivery_class pe 40 de subcategorii de seruri" nu e
    o decizie acționabilă; „lipsește pe îngrijire" e."""
    facts = ",\n        ".join(
        f"count(*) filter (where {pred}) as have_{name}" for name, pred in _FACTS.items()
    )
    fresh = ",\n        ".join(
        f"count(*) filter (where {pred}) as fresh_{name}" for name, pred in _FRESHNESS.items()
    )
    return f"""
        select
        coalesce(nullif(split_part(c.path, '/', 1), ''), '(fără categorie)') as root,
        count(*) as total,
        {facts},
        {fresh}
        from products p
        left join categories c on c.id = p.primary_category_id
        where p.business_id = $1 and p.status = 'active'
        group by 1
        order by 2 desc
    """


async def measure(business_id: str) -> list[dict]:
    pool = await get_pool()
    try:
        async with admin_conn(pool) as conn:
            rows = await conn.fetch(_coverage_sql(), business_id)
    finally:
        await close_pool()
    return [dict(r) for r in rows]


def _pct(have: int, total: int) -> str:
    return "—" if not total else f"{100.0 * have / total:5.1f}%"


def render(rows: list[dict]) -> str:
    if not rows:
        return "Niciun produs activ pe tenantul ăsta — nimic de măsurat."
    out: list[str] = []
    names = list(_FACTS)
    header = f"{'categorie rădăcină':<24}{'n':>6}  " + "  ".join(f"{n[:12]:>12}" for n in names)
    out.append(header)
    out.append("-" * len(header))
    totals = {n: 0 for n in names}
    grand = 0
    for r in rows:
        grand += r["total"]
        cells = []
        for n in names:
            have = r[f"have_{n}"]
            totals[n] += have
            cells.append(f"{_pct(have, r['total']):>12}")
        out.append(f"{str(r['root'])[:24]:<24}{r['total']:>6}  " + "  ".join(cells))
    out.append("-" * len(header))
    out.append(
        f"{'TOTAL':<24}{grand:>6}  " + "  ".join(f"{_pct(totals[n], grand):>12}" for n in names)
    )
    out.append("")
    out.append("Prospețime (coalesce(synced_at, updated_at)):")
    for key in _FRESHNESS:
        n = sum(r[f"fresh_{key}"] for r in rows)
        out.append(f"  {key:<14} {n:>6}  {_pct(n, grand)}")
    out.append("")
    out.append(
        "Fără sursă canonică, deci UNKNOWN prin construcție (nu apar mai sus): "
        "promisiunea de livrare, eligibilitatea de promoție/voucher, coșul magazinului."
    )
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description="Acoperirea faptelor de context (NX-234, read-only)")
    ap.add_argument("--business-id", required=True)
    ap.add_argument("--json", action="store_true", help="output brut, pentru raportare")
    args = ap.parse_args()
    rows = asyncio.run(measure(args.business_id))
    print(json.dumps(rows, indent=2, default=str) if args.json else render(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
