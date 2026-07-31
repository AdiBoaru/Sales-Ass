"""NX-203/NX-207 — compară read-only embeddings-urile legacy cu shadow pe același split.

Necesită un qrels validat uman și catalogul/embedding-urile deja backfill-uite. Nu activează
permanent `SEARCH_SHADOW_ENABLED`, nu scrie în catalog și nu deschide niciun holdout automat.

PYTHONPATH=. python scripts/nx207_compare_shadow.py \
    --qrels tests/golden/retrieval_qrels_compound.json --split holdout_h1
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agent.llm import get_llm  # noqa: E402
from src.config import get_settings  # noqa: E402
from src.db.connection import tenant_conn  # noqa: E402
from src.evals.retrieval.adaptor import retrieve_products  # noqa: E402
from src.evals.retrieval.catalog import (  # noqa: E402
    assert_catalog_unchanged,
    load_catalog,
)
from src.evals.retrieval.harness import RunConfig, compare_reports, run_benchmark  # noqa: E402
from src.evals.retrieval.schema import QrelsSet  # noqa: E402
from src.evals.retrieval.splits import Split, partition  # noqa: E402
from src.evals.retrieval.validation import integrity_issues  # noqa: E402

DEFAULT_QRELS = ROOT / "tests" / "golden" / "retrieval_qrels_compound.json"
DEFAULT_OUT = ROOT / "reports" / "nx207-shadow-comparison.json"


@contextmanager
def _shadow_read_enabled(enabled: bool) -> Iterator[None]:
    """Comută doar în procesul benchmarkului și îl restaurează inclusiv la eșec."""
    settings = get_settings()
    previous = settings.search_shadow_enabled
    settings.search_shadow_enabled = enabled
    try:
        yield
    finally:
        settings.search_shadow_enabled = previous


def _load_split(path: Path, split: Split, *, min_queries: int) -> QrelsSet:
    raw = json.loads(path.read_text(encoding="utf-8"))
    qset = QrelsSet(**{key: value for key, value in raw.items() if not key.startswith("_")})
    issues = integrity_issues(
        qset,
        min_queries=min_queries,
        require_human_verified=True,
        require_real_per_category=True,
        # Feliile de holdout trebuie să fie destul de mari ca să măsoare ceva: sub prag, un singur
        # query greșit mișcă metrica cu zeci de puncte și „a trecut gate-ul" devine zgomot.
        require_split_sizes=True,
    )
    if issues:
        raise ValueError("qrels nu trece gate-ul: " + "; ".join(issues))
    selected = partition(qset)[split]
    if not selected:
        raise ValueError(f"split-ul {split.value} este gol; nu deschid un holdout fără date")
    return QrelsSet(
        schema_version=qset.schema_version,
        business_id=qset.business_id,
        queries=selected,
    )


async def _prefetch(qset: QrelsSet, *, shadow: bool) -> tuple[dict[str, list[str]], float]:
    llm = get_llm()
    if llm is None:
        raise RuntimeError("OPENAI_API_KEY lipsește — comparația are nevoie de query embeddings.")

    started = time.perf_counter()
    out: dict[str, list[str]] = {}
    with _shadow_read_enabled(shadow):
        async with tenant_conn(qset.business_id) as conn:
            for query in qset.queries:
                out[query.query] = await retrieve_products(
                    conn,
                    llm,
                    qset.business_id,
                    query.query,
                    hard_constraints=[item.model_dump() for item in query.hard_constraints],
                    apply_constraints=True,
                )
    return out, (time.perf_counter() - started) * 1000


async def run(qrels_path: Path, split: Split, out_path: Path, *, min_queries: int) -> int:
    qset = _load_split(qrels_path, split, min_queries=min_queries)
    old, old_ms = await _prefetch(qset, shadow=False)
    shadow, shadow_ms = await _prefetch(qset, shadow=True)
    # Acelasi snapshot pentru AMBELE rapoarte: comparatia refuza cataloage diferite, iar doua
    # incarcari separate ar putea prinde un catalog schimbat intre ele.
    async with tenant_conn(qset.business_id) as conn:
        catalog = await load_catalog(conn, qset.business_id)
    settings = get_settings()
    baseline = run_benchmark(
        qset,
        lambda query: old[query],
        RunConfig(
            label="legacy-product-embedding",
            embedding_model=settings.model_embed,
            document_version="product",
            reranker="none",
            split=split.value,
        ),
        catalog,
    )
    candidate = run_benchmark(
        qset,
        lambda query: shadow[query],
        RunConfig(
            label="shadow-search-document-v1",
            embedding_model=settings.model_embed,
            document_version="search_document_v1",
            reranker="none",
            split=split.value,
        ),
        catalog,
    )
    # Garda de final: retrieval-ul a citit DB-ul LIVE. Daca s-a schimbat catalogul sub rulare,
    # ambele rapoarte poarta amprenta veche, deci comparatia ar accepta doua masuratori facute
    # contra unui catalog care nu mai exista.
    async with tenant_conn(qset.business_id) as conn:
        await assert_catalog_unchanged(conn, qset.business_id, catalog)
    comparison = compare_reports(baseline, candidate)
    payload = {
        "_meta": {
            "qrels": str(qrels_path),
            "business_id": qset.business_id,
            "split": split.value,
            "query_count": len(qset.queries),
            "catalog_fingerprint": baseline.catalog_fingerprint,
            "read_only": True,
            "note": "Nu marchează holdout-ul ca deschis; istoricul se actualizează "
            "numai după review uman.",
        },
        "latency_ms": {"legacy_total": round(old_ms, 2), "shadow_total": round(shadow_ms, 2)},
        "comparison": comparison.model_dump(),
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Raport scris: {out_path.relative_to(ROOT)}")
    print(f"Δ Recall@20: {comparison.delta_recall_at_20:+.3f}")
    print(f"Δ nDCG@6: {comparison.delta_ndcg_at_6:+.3f}")
    print(f"Δ Top-6: {comparison.delta_top_6_hit_rate:+.3f}")
    print(f"Δ Forbidden@6: {comparison.delta_forbidden_violation_rate:+.3f}")
    return 0


def main() -> int:
    if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Comparație NX-207 legacy vs shadow, read-only")
    parser.add_argument("--qrels", type=Path, default=DEFAULT_QRELS)
    parser.add_argument(
        "--split",
        choices=[item.value for item in Split],
        default=Split.holdout_h1.value,
    )
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--min-queries", type=int, default=200)
    args = parser.parse_args()
    try:
        return asyncio.run(
            run(args.qrels, Split(args.split), args.out, min_queries=args.min_queries)
        )
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"NX-207 comparison oprită: {exc}", file=sys.stderr)
        return 2
    finally:
        get_settings.cache_clear()


if __name__ == "__main__":
    raise SystemExit(main())
