"""NX-187 — recall vs SCAN EXHAUSTIV: câte potriviri REALE ratează pool-ul servit.

`MAX_SEARCH_POOL=24` (models.py): un produs care respectă toate hard constraints poate fi în AFARA
setului adus de retrieval → un Match Gate care judecă doar pool-ul ar declara fals „zero exact".
Scriptul rulează hard constraints (qrels NX-208) pe adevărul NX-202 + pe tot catalogul, offline:

  - `relevant_exact`  = produse judecate relevante care satisfac TOATE hard constraints (exact);
  - `relevant_in_pool`= câte apar în primii MAX_SEARCH_POOL candidați serviți de retrieval;
  - `missed_by_pool`  = relevant_exact − pool → potriviri reale ratate de un gate doar-pe-pool.

Per fațetă hard: în câte query-uri e implicată într-un miss (input pt enforce, NX-188). Necesită
DB + OpenAI (embeddings pt pool). Read-only. `WHERE business_id = $1`.
"""

import argparse
import asyncio
import collections
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from src.agent.llm import get_llm  # noqa: E402
from src.agent.match_gate import build_match_set, classify_product  # noqa: E402
from src.agent.query_spec import Constraint  # noqa: E402
from src.db.connection import close_pool, tenant_conn  # noqa: E402
from src.db.queries.businesses import load_business  # noqa: E402
from src.domain.loader import load_domain_pack  # noqa: E402
from src.evals.retrieval.adaptor import retrieve_products  # noqa: E402
from src.models import MAX_SEARCH_POOL, BusinessConfig  # noqa: E402

DEMO_BIZ = "6098812a-50fc-44bd-a1ba-bc77e6399158"
QRELS = ROOT / "tests" / "golden" / "retrieval_qrels_compound.json"
REPORT = ROOT / "reports" / "match-recall-scan-compound.json"


def _hard_constraints(q: dict) -> list[Constraint]:
    """qrels hard_constraints → Constraint hard (sare peste non-registru ca compare_set_size)."""
    out: list[Constraint] = []
    for hc in q.get("hard_constraints", []):
        facet = hc.get("facet")
        if facet in (None, "compare_set_size"):
            continue
        out.append(
            Constraint(
                facet=facet,
                op=hc.get("op"),
                value=hc.get("value"),
                strength="hard",
                source="derived",
            )
        )
    return out


async def _facets(conn, business_id: str, vertical: str):
    business = await load_business(conn, business_id)
    if business and business.domain_pack and business.domain_pack.facets:
        return {f.key: f for f in business.domain_pack.facets}
    fb = load_domain_pack(BusinessConfig(id=business_id, slug="x", name="x", vertical=vertical))
    return {f.key: f for f in (fb.facets if fb else ())}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--business", default=DEMO_BIZ)
    ap.add_argument("--vertical", default="beauty_salon")
    ap.add_argument("--date", required=True, help="AAAA-LL-ZZ (stamp determinist)")
    args = ap.parse_args()

    raw = json.loads(QRELS.read_text(encoding="utf-8"))
    queries = raw.get("queries", [])
    llm = get_llm()
    if llm is None:
        raise SystemExit("OPENAI_API_KEY lipsă — scanul are nevoie de embeddings pentru pool.")

    per_query: list[dict] = []
    facet_miss: collections.Counter = collections.Counter()

    async with tenant_conn(args.business) as conn:
        facets = await _facets(conn, args.business, args.vertical)
        rows = await conn.fetch(
            """
            select p.id::text as id, p.price::float8 as price, p.attributes, c.slug as category_slug
            from products p
            left join categories c on c.id = p.primary_category_id
            where p.business_id = $1 and p.status = 'active' and p.content_status = 'published'
            """,
            args.business,
        )
        catalog = []
        for r in rows:
            d = dict(r)
            a = d["attributes"]
            d["attributes"] = json.loads(a) if isinstance(a, str) else (a or {})
            catalog.append(d)

        by_id = {p["id"]: p for p in catalog}
        for q in queries:
            constraints = _hard_constraints(q)
            if not constraints:
                continue
            scope = (
                [p for p in catalog if p["category_slug"] == q["category"]]
                if q.get("category")
                else catalog
            )
            # „Potriviri reale" = produse JUDECATE relevante (NX-202) care satisfac hard constraints
            # (clasa `exact`). Un filtru pe tot catalogul umflă cu produse doar-preț-ok irelevante
            # — recall-ul semnificativ e pe adevăr, nu pe „orice respectă bugetul".
            judged = [str(j["product_id"]) for j in q.get("judgments", [])]
            relevant_exact = {
                pid
                for pid in judged
                if pid in by_id
                and classify_product(by_id[pid], constraints, facets).match_class == "exact"
            }
            exact_scan_catalog = len(build_match_set(scope, constraints, facets).exact)
            pool = await retrieve_products(conn, llm, args.business, q["query"])
            pool_ids = set(pool[:MAX_SEARCH_POOL])
            missed = relevant_exact - pool_ids
            if missed:
                for c in constraints:
                    facet_miss[c.facet] += 1
            per_query.append(
                {
                    "id": q["id"],
                    "hard_facets": sorted({c.facet for c in constraints}),
                    "scope_size": len(scope),
                    "exact_scan_catalog": exact_scan_catalog,  # câte hard-satisfac (informativ)
                    "relevant_exact": len(relevant_exact),  # judecate relevante ȘI exact
                    "relevant_in_pool": len(relevant_exact & pool_ids),
                    "missed_by_pool": len(missed),
                }
            )

    await close_pool()

    total_scan = sum(r["relevant_exact"] for r in per_query)
    total_missed = sum(r["missed_by_pool"] for r in per_query)
    report = {
        "_meta": {
            "generated": "NX-187 recall vs scan exhaustiv (hard constraints vs pool servit)",
            "business_id": args.business,
            "date": args.date,
            "max_search_pool": MAX_SEARCH_POOL,
            "note": "relevant_exact = produse judecate relevante (NX-202) care satisfac hard "
            "constraints. missed_by_pool = câte lipsesc din primii MAX_SEARCH_POOL. pool_recall = "
            "1 - missed/relevant_exact. exact_scan_catalog = câte hard-satisfac în scope. "
            "facet_miss = în câte query-uri fațeta e implicată în miss.",
        },
        "summary": {
            "queries": len(per_query),
            "total_relevant_exact": total_scan,
            "total_missed_by_pool": total_missed,
            "pool_recall": round(1 - total_missed / total_scan, 3) if total_scan else None,
            "facet_miss": dict(facet_miss.most_common()),
        },
        "per_query": per_query,
    }
    REPORT.parent.mkdir(exist_ok=True)
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    s = report["summary"]
    print(
        f"query-uri: {s['queries']} · relevant_exact: {s['total_relevant_exact']} · "
        f"ratate de pool: {s['total_missed_by_pool']} · pool_recall: {s['pool_recall']}"
    )
    print(f"raport: {REPORT.relative_to(ROOT)}")


if __name__ == "__main__":
    asyncio.run(main())
