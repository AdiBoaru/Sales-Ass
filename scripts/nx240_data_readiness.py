"""NX-240 — măsurarea coverage-ului real al faptelor comerciale, per tenant și per categorie.

De ce un script și nu un test: un test spune „regula e implementată", un raport spune „ce se
poate afișa AZI, pe datele astea". Al doilea e cel care decide dacă widgetul are ce arăta —
și e singurul care poate contrazice o presupunere de produs cu o cifră.

Citește DOAR (`SELECT` pe catalog, tenant-scoped, prin `tenant_conn`). Zero OpenAI, zero scriere.
Rulare:

    python scripts/nx240_data_readiness.py                    # tenantul demo, raport în stdout
    python scripts/nx240_data_readiness.py --json reports/nx240/coverage.json

Ce măsoară, pentru fiecare câmp din `FIELD_POLICY`:
  • `known`  — se poate AFIȘA (valoare prezentă, în interiorul SLA-ului dacă i se aplică);
  • `stale`  — verificat, dar peste SLA ⇒ omis;
  • `unknown`— fără valoare sau fără sursă;
  • `verified` — are `verified_at` real ⇒ poate susține o PROMISIUNE (CTA de comerț).

Ultima coloană e cea care contează pentru comerț: un catalog cu 100% `known` și 0% `verified` e
un catalog pe care poți afișa prețuri, dar nu poți promite că se poate cumpăra.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.agent.evidence_bundle import FACT_FIELDS, FIELD_POLICY, build_product_evidence
from src.config import get_settings
from src.db.connection import close_pool, tenant_conn

DEMO_BUSINESS_ID = "6098812a-50fc-44bd-a1ba-bc77e6399158"

# Aceleași expresii de preț/variante ca `catalog.py` — coverage-ul TREBUIE măsurat pe exact
# valorile pe care le vede clientul, nu pe coloanele brute (altfel raportăm alt catalog).
_SQL = """
    select
        p.id::text                                     as id,
        p.name                                         as name,
        b.name                                         as brand,
        coalesce(c.name, '(fără categorie)')           as category,
        coalesce(vp.price, case when p.sale_price is not null and p.sale_price < p.price
                                then p.sale_price else p.price end)::float8 as price,
        (case when p.sale_price is not null and p.sale_price < p.price
              then p.price end)::float8                as list_price,
        p.currency                                     as currency,
        p.product_url                                  as url,
        img.url                                        as image,
        p.availability                                 as availability,
        p.stock_total                                  as stock,
        p.rating::float8                               as rating,
        p.review_count                                 as review_count,
        prs.summary                                    as review_summary,
        p.synced_at                                    as synced_at,
        p.updated_at                                   as updated_at
    from products p
    left join brands b on b.id = p.brand_id
    left join categories c on c.id = p.primary_category_id
    left join product_review_summaries prs on prs.product_id = p.id
    left join lateral (
        select min(case when v.sale_price is not null and v.sale_price < v.price
                        then v.sale_price else v.price end) as price
        from product_variants v
        where v.product_id = p.id and v.business_id = p.business_id
    ) vp on true
    left join lateral (
        select pi.url from product_images pi
        where pi.product_id = p.id order by pi.position asc nulls last limit 1
    ) img on true
    where p.business_id = $1 and p.status = 'active'
"""


def _pct(part: int, total: int) -> str:
    return f"{(100 * part / total):5.1f}%" if total else "    -"


async def measure(business_id: str, sla_s: int) -> dict[str, Any]:
    now = datetime.now(UTC)
    async with tenant_conn(business_id) as conn:
        rows = [dict(r) for r in await conn.fetch(_SQL, business_id)]

    overall: dict[str, Counter] = {name: Counter() for name in FACT_FIELDS}
    verified: Counter = Counter()
    by_category: dict[str, dict[str, Counter]] = defaultdict(
        lambda: {name: Counter() for name in FACT_FIELDS}
    )
    for raw in rows:
        category = raw.pop("category")
        evidence = build_product_evidence(raw, business_id=business_id, now=now, sla_s=sla_s)
        if evidence is None:
            continue
        for name in FACT_FIELDS:
            fact = evidence.fact(name)
            overall[name][fact.status] += 1
            by_category[category][name][fact.status] += 1
            if fact.verified:
                verified[name] += 1
    return {
        "business_id": business_id,
        "measured_at": now.isoformat(),
        "sla_s": sla_s,
        "products": len(rows),
        "fields": {
            name: {
                "known": overall[name]["known"],
                "stale": overall[name]["stale"],
                "unknown": overall[name]["unknown"],
                "verified": verified[name],
                "source": FIELD_POLICY[name].source,
                "blocks_commerce_cta": FIELD_POLICY[name].blocks_commerce_cta,
            }
            for name in FACT_FIELDS
        },
        "categories": {
            category: {
                name: dict(counts[name]) for name in FACT_FIELDS if counts[name]["known"] == 0
            }
            for category, counts in sorted(by_category.items())
        },
    }


def render(report: dict[str, Any]) -> str:
    total = report["products"]
    lines = [
        f"NX-240 data-readiness — business {report['business_id']}",
        f"produse active: {total} · SLA prospețime: {report['sla_s']}s"
        f" · măsurat: {report['measured_at']}",
        "",
        f"{'câmp':<18}{'known':>8}{'stale':>8}{'unknown':>9}{'verified':>10}  sursă",
        "-" * 84,
    ]
    for name, data in report["fields"].items():
        source = data["source"] or "— (fără adaptor)"
        lines.append(
            f"{name:<18}{_pct(data['known'], total):>8}{_pct(data['stale'], total):>8}"
            f"{_pct(data['unknown'], total):>9}{_pct(data['verified'], total):>10}  {source}"
        )
    lines.append("")
    blocking = [n for n, d in report["fields"].items() if d["blocks_commerce_cta"]]
    sellable = min((report["fields"][n]["known"] for n in blocking), default=0)
    verified = min((report["fields"][n]["verified"] for n in blocking), default=0)
    lines.append(
        f"CTA de comerț: cel mult {sellable}/{total} produse ({_pct(sellable, total).strip()}) — "
        f"cere `known` (nu expirat) pe {', '.join(blocking)}. Garanția reală rămâne la mutație "
        f"(CartService revalidează), deci butonul e o ofertă de a încerca, nu o promisiune."
    )
    lines.append(
        f"Pipeline de VERIFICARE: {verified}/{total} produse ({_pct(verified, total).strip()}) au "
        "`synced_at`. Consecința lui 0% nu e absența butoanelor, ci imposibilitatea de a declara "
        "un fapt EXPIRAT: nu avem de unde ști că s-a stricat. Un sync real ar aduce ambele — "
        "prospețime afișabilă și expirare detectabilă."
    )
    return "\n".join(lines)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--business", default=DEMO_BUSINESS_ID)
    parser.add_argument("--json", type=Path, default=None, help="scrie raportul și ca JSON")
    args = parser.parse_args()
    try:
        report = await measure(args.business, get_settings().commerce_facts_sla_s)
    finally:
        await close_pool()
    print(render(report))
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"\nraport JSON: {args.json}")


if __name__ == "__main__":
    asyncio.run(main())
