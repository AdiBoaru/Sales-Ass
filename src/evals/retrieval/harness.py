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
    family_id: str | None = None  # din qrels, NU derivat din text la runtime


class BenchmarkReport(BaseModel):
    """Scorurile headline sunt agregate pe FAMILIE, nu pe query.

    Fără asta, un query prezent în două variante de formă (diacritice, typo) cântărește dublu: nu
    pentru că ar conta mai mult, ci pentru că a fost cules de două ori. Media pe familie şi apoi
    macro peste familii scoate din scor exact acest artefact de colectare.

    `n_queries` şi `n_families` sunt AMBELE expuse, ca să se vadă când ponderarea headline nu mai
    coincide cu numărul brut de interogări."""

    config: RunConfig
    n_queries: int
    n_families: int
    recall_at_20: float
    ndcg_at_6: float
    top_6_hit_rate: float
    mrr: float
    #: Fracția FAMILIILOR cu ≥1 produs interzis în top-6. OR în interiorul familiei: dacă o singură
    #: variantă scoate un produs interzis, familia e violată — o încălcare de constrângere nu se
    #: mediază, se numără. Altfel, adăugarea unei variante „curate" ar dilua o violare reală.
    forbidden_violation_rate: float
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
    if baseline.n_families != candidate.n_families:
        # Scorurile headline sunt macro pe familie; cu alt număr de familii, deltele compară
        # ponderări diferite şi arată ca o schimbare de calitate.
        raise ValueError("nu pot compara rapoarte cu număr diferit de familii")
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
        family_id=q.family_id,
        recall_at_20=metrics.recall_at_k(q, ranked, 20),
        ndcg_at_6=metrics.ndcg_at_k(q, ranked, 6),
        top_6_hit=metrics.top_k_hit(q, ranked, 6),
        mrr=metrics.mrr(q, ranked),
        forbidden_in_6=metrics.forbidden_violations(q, ranked, 6),
    )


def _families(results: Sequence[QueryResult]) -> dict[str, list[QueryResult]]:
    """Grupează rezultatele pe `family_id`.

    Un set LEGACY complet negrupat rămâne valid: fiecare query devine familie singleton, deci
    agregarea pe familie coincide exact cu media pe query. Seturile PARȚIAL grupate nu ajung aici —
    `QrelsSet` le respinge, fiindcă ar scuti tăcut tocmai intrările fără id."""
    out: dict[str, list[QueryResult]] = {}
    for r in results:
        out.setdefault(r.family_id or f"__singleton__:{r.id}", []).append(r)
    return out


def run_benchmark(qset: QrelsSet, retrieve: RetrieveFn, config: RunConfig) -> BenchmarkReport:
    """Rulează retrieval-ul pe fiecare query și agregă PE FAMILIE. Determinist dacă `retrieve` e."""
    per_query = [evaluate_query(q, list(retrieve(q.query))) for q in qset.queries]
    fams = _families(per_query)

    def macro(attr: str) -> float:
        """Medie ÎN familie, apoi macro peste familii — nu media globală pe query-uri."""
        if not fams:
            return 0.0
        return mean(mean(getattr(r, attr) for r in group) for group in fams.values())

    violated = sum(1 for group in fams.values() if any(r.forbidden_in_6 > 0 for r in group))
    return BenchmarkReport(
        config=config,
        n_queries=len(per_query),
        n_families=len(fams),
        recall_at_20=macro("recall_at_20"),
        ndcg_at_6=macro("ndcg_at_6"),
        top_6_hit_rate=macro("top_6_hit"),
        mrr=macro("mrr"),
        forbidden_violation_rate=violated / (len(fams) or 1),
        per_query=per_query,
    )
