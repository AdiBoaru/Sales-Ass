"""NX-317 — sonda comparațiilor reale: axe egale și verdicte repetate. Read-only.

Rulează pe `conversation_traces` (reply-ul complet, cu `comparison`), apoi re-citește fișele
produselor comparate. Răspunde la trei întrebări, cu cifre, înainte de orice prag:

1. **Proza celulelor**: câte axe au celule aproape identice pe toate coloanele (similaritate pe
   cuvinte de conținut, la mai multe praguri)?
2. **Verdictul repetat**: între bucățile de proză ale răspunsului (propozițiile din lead, subtitlul,
   propozițiile din closing), care e similaritatea maximă? Pragul de deduplicare (0,6 în card) se
   alege aici: sub el trebuie să cadă perechile care spun lucruri DIFERITE.
3. **Valorile-sursă**: pe perechile comparate, cât de asemănătoare sunt `recenzii` și `descriere`
   între două produse DIFERITE? Pragul de „aceeași valoare" (0,8 în card) trebuie să stea peste ele.

ZERO scriere, ZERO model. Tenant-scoped (`tenant_conn`), `business_id` explicit în fiecare query.

    PYTHONPATH=. python scripts/nx317_comparison_probe.py --business sole-ro --days 30
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from itertools import combinations
from pathlib import Path
from typing import Any

from src.agent.compare_narrative import sentences, similarity
from src.db.connection import admin_conn, close_pool, get_pool, tenant_conn
from src.db.queries.catalog import get_products_by_ids

_THRESHOLDS = (0.5, 0.6, 0.7, 0.8, 0.9)


def _bucket(x: float) -> str:
    return f"{int(x * 10) / 10:.1f}"


def _pieces(cmp: dict[str, Any]) -> list[tuple[str, str]]:
    out = [("lead", s) for s in sentences(cmp.get("intro"))]
    if cmp.get("subtitle"):
        out.append(("subtitle", str(cmp["subtitle"])))
    for paragraph in cmp.get("closing") or []:
        out += [("closing", s) for s in sentences(paragraph)]
    return out


async def probe(slug: str, days: int) -> dict[str, Any]:
    pool = await get_pool()
    async with admin_conn(pool) as admin:
        row = await admin.fetchrow(
            "select id::text as id, coalesce(default_locale, 'ro') as locale"
            " from businesses where slug = $1 or id::text = $1",
            slug,
        )
    if row is None:
        raise SystemExit(f"tenant necunoscut: {slug!r}")
    business_id, locale = row["id"], row["locale"]
    async with tenant_conn(business_id) as conn:
        traces = await conn.fetch(
            """
            select turn_id::text as turn_id, reply->'comparison' as cmp
            from conversation_traces
            where business_id = $1::uuid and created_at > now() - make_interval(days => $2)
              and reply ? 'comparison' and jsonb_typeof(reply->'comparison') = 'object'
            order by created_at
            """,
            business_id,
            days,
        )
        comparisons = [
            json.loads(t["cmp"]) if isinstance(t["cmp"], str) else t["cmp"] for t in traces
        ]
        ids = sorted(
            {str(c.get("product_id")) for cmp in comparisons for c in cmp.get("columns") or []}
        )
        products: dict[str, dict[str, Any]] = {}
        for i in range(0, len(ids), 6):
            for p in await get_products_by_ids(conn, business_id, ids[i : i + 6]):
                products[str(p["id"])] = p

    same_prose = Counter()
    rows_total = 0
    piece_max: list[float] = []
    repeated = Counter()
    kinds_repeated = Counter()
    source_sims: dict[str, list[float]] = {"recenzii": [], "descriere": [], "tip_produs": []}
    for cmp in comparisons:
        for r in cmp.get("rows") or []:
            values = [v for v in r.get("values") or [] if v]
            if len(values) < 2:
                continue
            rows_total += 1
            low = min(similarity(a, b, locale) for a, b in combinations(values, 2))
            for t in _THRESHOLDS:
                if low >= t:
                    same_prose[t] += 1
        pieces = _pieces(cmp)
        best = 0.0
        for (ka, a), (kb, b) in combinations(pieces, 2):
            s = similarity(a, b, locale)
            if s > best:
                best, pair = s, f"{ka}~{kb}"
        piece_max.append(best)
        for t in _THRESHOLDS:
            if best >= t:
                repeated[t] += 1
        if best >= 0.6:
            kinds_repeated[pair] += 1
        cols = [products.get(str(c.get("product_id"))) for c in cmp.get("columns") or []]
        cols = [c for c in cols if c]
        for a, b in combinations(cols, 2):
            for source, key in (("recenzii", "review_summary"), ("descriere", "ai_summary")):
                if a.get(key) and b.get(key):
                    source_sims[source].append(similarity(a[key], b[key], locale))
            ta = (a.get("attributes") or {}).get("product_type")
            tb = (b.get("attributes") or {}).get("product_type")
            if ta and tb:
                source_sims["tip_produs"].append(1.0 if ta == tb else 0.0)

    return {
        "business": slug,
        "days": days,
        "comparisons": len(comparisons),
        "rows_with_2plus_cells": rows_total,
        "rows_same_prose_at": {str(t): same_prose[t] for t in _THRESHOLDS},
        "max_piece_similarity_hist": dict(sorted(Counter(_bucket(x) for x in piece_max).items())),
        "comparisons_with_repeat_at": {str(t): repeated[t] for t in _THRESHOLDS},
        "repeat_pairs_at_0.6": dict(kinds_repeated),
        "source_similarity_hist": {
            k: dict(sorted(Counter(_bucket(x) for x in v).items())) for k, v in source_sims.items()
        },
        "source_pairs": {k: len(v) for k, v in source_sims.items()},
    }


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--business", default="sole-ro")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--json", type=Path, default=None)
    args = ap.parse_args()
    try:
        report = await probe(args.business, args.days)
    finally:
        await close_pool()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
