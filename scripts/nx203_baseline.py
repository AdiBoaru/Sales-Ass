"""NX-203 baseline — măsoară retrieval-ul ACTUAL pe qrels-ul compus (adevăr NX-202 validat).

Rulează calea reală hibrid+RRF pe cele 12 interogări grele, în două regimuri (raw / cu
constrângeri), și scrie un raport machine-readable în `reports/`. PRIMELE cifre reale: de unde
plecăm pe query-urile grele, înainte de search_entities (NX-209).

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
from src.evals.retrieval.adaptor import retrieve_products  # noqa: E402
from src.evals.retrieval.harness import RunConfig, run_benchmark  # noqa: E402
from src.evals.retrieval.schema import QrelsSet  # noqa: E402

QRELS = ROOT / "tests" / "golden" / "retrieval_qrels_compound.json"
REPORT = ROOT / "reports" / "nx203-baseline-compound.json"


async def _prefetch(qset: QrelsSet, *, apply_constraints: bool) -> dict[str, list[str]]:
    """Pre-încarcă retrieval-ul real pentru fiecare query (async), o dată per regim."""
    out: dict[str, list[str]] = {}
    llm = get_llm()
    if llm is None:
        raise SystemExit("OPENAI_API_KEY lipsă — baseline-ul are nevoie de embeddings.")
    async with tenant_conn(qset.business_id) as conn:
        for q in qset.queries:
            out[q.query] = await retrieve_products(
                conn,
                llm,
                qset.business_id,
                q.query,
                hard_constraints=[hc.model_dump() for hc in q.hard_constraints],
                apply_constraints=apply_constraints,
            )
    return out


async def main() -> None:
    raw = json.loads(QRELS.read_text(encoding="utf-8"))
    qset = QrelsSet(**{k: v for k, v in raw.items() if not k.startswith("_")})
    print(f"qrels: {len(qset.queries)} interogări grele\n")

    reports = {}
    for label, apply_c in (("raw_hybrid", False), ("hybrid_with_constraints", True)):
        fetched = await _prefetch(qset, apply_constraints=apply_c)
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
                    "generated": "NX-203 baseline pe qrels compus (12 interogări grele)",
                    "source_qrels": "tests/golden/retrieval_qrels_compound.json",
                    "catalog": "demo 300 produse, read-only",
                    "note": "raw = retrieval pe text brut, zero filtre; with_constraints = "
                    "price_max+category aplicate (plafon cu înțelegere perfectă). Diferența = "
                    "cât ține de retrieval vs query understanding (NX-208).",
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
