"""NX-203 — harness de benchmark (SCHELET). Rulează o funcție de retrieval peste qrels și produce
metrici comparabile + config-ul rulării. Fără dataset masiv: primește orice `QrelsSet` (exemplul
minuscul sau, ulterior, setul complet de 200-500 populat din etichetele NX-202).

Retrieval-ul e injectat ca `RetrieveFn` (query → listă ordonată de product_id) — harness-ul NU știe
de `search_products`/`search_entities`, ca aceeași măsurare să compare configurații diferite
(lexical vs +semantic vs +reranker; embeddings A vs B) fără să se schimbe codul de măsurare.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from statistics import mean

from pydantic import BaseModel

from src.evals.retrieval import metrics
from src.evals.retrieval.schema import QrelsQuery, QrelsSet

# query text → listă ordonată de product_id (cel mai relevant primul).
RetrieveFn = Callable[[str], Sequence[str]]


class RunConfig(BaseModel):
    """Config-ul complet al rulării — înregistrat în output ca rezultatele să fie reproductibile
    și comparabile (Codex: model embeddings, document_version, ponderi, reranker, data)."""

    label: str  # ex. "baseline-lexical+semantic+fusion"
    embedding_model: str | None = None
    document_version: str | None = None
    reranker: str | None = None
    weights: dict[str, float] | None = None
    split: str | None = None  # pe ce felie s-a rulat (tuning/holdout_hN)


class QueryResult(BaseModel):
    id: str
    recall_at_20: float
    ndcg_at_6: float
    top_6_hit: float
    mrr: float
    forbidden_in_6: int


class BenchmarkReport(BaseModel):
    config: RunConfig
    n_queries: int
    recall_at_20: float
    ndcg_at_6: float
    top_6_hit_rate: float
    mrr: float
    forbidden_violation_rate: float  # fracția query-urilor cu ≥1 produs interzis în top-6
    per_query: list[QueryResult]


class BenchmarkComparison(BaseModel):
    """Comparație machine-readable între baseline și candidat pe exact același qrels split."""

    baseline: BenchmarkReport
    candidate: BenchmarkReport
    delta_recall_at_20: float
    delta_ndcg_at_6: float
    delta_top_6_hit_rate: float
    delta_forbidden_violation_rate: float


def compare_reports(baseline: BenchmarkReport, candidate: BenchmarkReport) -> BenchmarkComparison:
    """Diferențe candidat-minus-baseline; condiția de switch se decide în afara harness-ului."""
    if baseline.n_queries != candidate.n_queries:
        raise ValueError("nu pot compara rapoarte cu număr diferit de query-uri")
    return BenchmarkComparison(
        baseline=baseline,
        candidate=candidate,
        delta_recall_at_20=candidate.recall_at_20 - baseline.recall_at_20,
        delta_ndcg_at_6=candidate.ndcg_at_6 - baseline.ndcg_at_6,
        delta_top_6_hit_rate=candidate.top_6_hit_rate - baseline.top_6_hit_rate,
        delta_forbidden_violation_rate=(
            candidate.forbidden_violation_rate - baseline.forbidden_violation_rate
        ),
    )


def evaluate_query(q: QrelsQuery, ranked: Sequence[str]) -> QueryResult:
    return QueryResult(
        id=q.id,
        recall_at_20=metrics.recall_at_k(q, ranked, 20),
        ndcg_at_6=metrics.ndcg_at_k(q, ranked, 6),
        top_6_hit=metrics.top_k_hit(q, ranked, 6),
        mrr=metrics.mrr(q, ranked),
        forbidden_in_6=metrics.forbidden_violations(q, ranked, 6),
    )


def run_benchmark(qset: QrelsSet, retrieve: RetrieveFn, config: RunConfig) -> BenchmarkReport:
    """Rulează retrieval-ul pe fiecare query și agregă. Determinist dacă `retrieve` e."""
    per_query = [evaluate_query(q, list(retrieve(q.query))) for q in qset.queries]
    n = len(per_query) or 1
    return BenchmarkReport(
        config=config,
        n_queries=len(per_query),
        recall_at_20=mean(r.recall_at_20 for r in per_query) if per_query else 0.0,
        ndcg_at_6=mean(r.ndcg_at_6 for r in per_query) if per_query else 0.0,
        top_6_hit_rate=mean(r.top_6_hit for r in per_query) if per_query else 0.0,
        mrr=mean(r.mrr for r in per_query) if per_query else 0.0,
        forbidden_violation_rate=sum(1 for r in per_query if r.forbidden_in_6 > 0) / n,
        per_query=per_query,
    )
