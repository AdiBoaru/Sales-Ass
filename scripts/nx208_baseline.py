"""NX-208 — dovada cu cifre a stratului de ÎNȚELEGERE a interogării (query rewrite + concern_map).

Rulează retrieval-ul pe cele 12 interogări grele (adevăr NX-202) în TREI regimuri, ca să izoleze
exact ce aduce NX-208 peste baseline-ul NX-203:

  - `raw_hybrid`              — text brut, zero filtre (reper: baseline-ul NX-203).
  - `rewritten_hybrid`        — query RESCRIS determinist (search_text expandat + referință),
                                tot fără oracol de constrângeri → câștigul de query understanding.
  - `hybrid_with_constraints` — price_max + category din adevăr (plafon cu înțelegere perfectă);
                                reflectă și fix-ul de date ser-pentru-ten → seruri-pentru-ten.

Necesită DB live (tenant_conn) + OpenAI (embeddings). Rulare de dev, NU CI. Read-only pe catalog.
"""

import asyncio
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.agent.llm import get_llm  # noqa: E402
from src.db.connection import close_pool, tenant_conn  # noqa: E402
from src.db.queries.businesses import load_business  # noqa: E402
from src.evals.retrieval.adaptor import (  # noqa: E402
    retrieve_products,
    retrieve_products_rewritten,
)
from src.evals.retrieval.harness import RunConfig, run_benchmark  # noqa: E402
from src.evals.retrieval.schema import QrelsSet  # noqa: E402

QRELS = ROOT / "tests" / "golden" / "retrieval_qrels_compound.json"
REPORT = ROOT / "reports" / "nx208-rewrite-compound.json"


async def _prefetch(qset: QrelsSet, regime: str) -> dict[str, list[str]]:
    """Pre-încarcă retrieval-ul real per query (async), o dată per regim."""
    out: dict[str, list[str]] = {}
    llm = get_llm()
    if llm is None:
        raise SystemExit("OPENAI_API_KEY lipsă — baseline-ul are nevoie de embeddings.")
    async with tenant_conn(qset.business_id) as conn:
        business = await load_business(conn, qset.business_id)
        domain_pack = business.domain_pack if business else None
        for q in qset.queries:
            if regime == "rewritten_hybrid":
                out[q.query] = await retrieve_products_rewritten(
                    conn, llm, qset.business_id, q.query, domain_pack
                )
            else:
                out[q.query] = await retrieve_products(
                    conn,
                    llm,
                    qset.business_id,
                    q.query,
                    hard_constraints=[hc.model_dump() for hc in q.hard_constraints],
                    apply_constraints=(regime == "hybrid_with_constraints"),
                )
    return out


async def main() -> None:
    raw = json.loads(QRELS.read_text(encoding="utf-8"))
    qset = QrelsSet(**{k: v for k, v in raw.items() if not k.startswith("_")})
    print(f"qrels: {len(qset.queries)} interogări grele\n")

    reports = {}
    for label in ("raw_hybrid", "rewritten_hybrid", "hybrid_with_constraints"):
        fetched = await _prefetch(qset, label)
        report = run_benchmark(
            qset,
            lambda query, f=fetched: f.get(query, []),
            RunConfig(
                label=label,
                embedding_model="text-embedding-3-small",
                reranker="none",
                split="all-compound",
            ),
        )
        reports[label] = report.model_dump()
        print(f"=== {label}")
        print(f"  Recall@20:    {report.recall_at_20:.3f}")
        print(f"  nDCG@6:       {report.ndcg_at_6:.3f}")
        print(f"  Top-6 hit:    {report.top_6_hit_rate:.3f}")
        print(f"  MRR:          {report.mrr:.3f}")
        print(f"  Forbidden@6:  {report.forbidden_violation_rate:.3f} (interzis in top-6)")
        print()

    await close_pool()

    REPORT.parent.mkdir(exist_ok=True)
    REPORT.write_text(
        json.dumps(
            {
                "_meta": {
                    "generated": "NX-208 — retrieval pe 3 regimuri (12 interogări grele)",
                    "source_qrels": "tests/golden/retrieval_qrels_compound.json",
                    "catalog": "demo, read-only",
                    "note": "raw_hybrid = reperul NX-203 (text brut). rewritten_hybrid = query "
                    "understanding NX-208 (search_text expandat + referință), fără oracol. "
                    "hybrid_with_constraints = plafon cu price_max+category din adevăr (include "
                    "fix-ul de date ser-pentru-ten → seruri-pentru-ten). Δ(rewritten − raw) = "
                    "câștigul atribuibil stratului de înțelegere.",
                },
                "configs": reports,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"raport scris: {REPORT.relative_to(ROOT)}")


if __name__ == "__main__":
    asyncio.run(main())
