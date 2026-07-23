"""NX-203 — teste pentru scheletul de benchmark: corectitudinea metricilor (valori calculate de
mână), integritatea qrels, spliturile single-use și rularea harness-ului pe exemplul minuscul.

NU testează retrieval-ul real (aia = popularea NX-203, după etichetele NX-202) — testează că
SCHELETUL e corect: metrici, contract, anti-contaminare.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.evals.retrieval import metrics
from src.evals.retrieval.harness import RunConfig, run_benchmark
from src.evals.retrieval.schema import (
    HardConstraint,
    Provenance,
    QrelJudgment,
    QrelsQuery,
    QrelsSet,
    Relevance,
)
from src.evals.retrieval.splits import Split, holdout_slice_for_gate, partition

EXAMPLE = Path(__file__).parent / "golden" / "retrieval_qrels_example.json"


def _q(**kw) -> QrelsQuery:
    base = dict(id="q", query="x", provenance=Provenance.synthetic, catalog_version="v0")
    base.update(kw)
    return QrelsQuery(**base)


# --- metrici (valori calculate de mână) --------------------------------------


def test_recall_at_k():
    q = _q(
        judgments=[
            QrelJudgment(product_id="a", relevance=Relevance.ideal),
            QrelJudgment(product_id="b", relevance=Relevance.relevant),
            QrelJudgment(product_id="c", relevance=Relevance.marginal),
        ]
    )
    # 3 relevante (a,b,c); ranked prinde a și c în primele 20 → 2/3
    assert metrics.recall_at_k(q, ["a", "z", "c", "y"], 20) == pytest.approx(2 / 3)
    # niciun relevant → 1.0 (nimic de ratat)
    assert metrics.recall_at_k(_q(), ["a"], 20) == 1.0


def test_ndcg_at_k_perfect_and_imperfect():
    q = _q(
        judgments=[
            QrelJudgment(product_id="a", relevance=Relevance.ideal),  # gain 3
            QrelJudgment(product_id="b", relevance=Relevance.relevant),  # gain 2
        ]
    )
    # ordine perfectă a,b → nDCG=1
    assert metrics.ndcg_at_k(q, ["a", "b"], 6) == pytest.approx(1.0)
    # ordine inversată b,a: DCG = 2/log2(2) + 3/log2(3); IDCG = 3/log2(2) + 2/log2(3)
    dcg = 2 / math.log2(2) + 3 / math.log2(3)
    idcg = 3 / math.log2(2) + 2 / math.log2(3)
    assert metrics.ndcg_at_k(q, ["b", "a"], 6) == pytest.approx(dcg / idcg)


def test_top_k_hit_and_mrr():
    q = _q(judgments=[QrelJudgment(product_id="a", relevance=Relevance.relevant)])
    assert metrics.top_k_hit(q, ["z", "a"], 6) == 1.0
    assert metrics.top_k_hit(q, ["z", "y"], 6) == 0.0
    assert metrics.mrr(q, ["z", "a"]) == pytest.approx(0.5)
    assert metrics.mrr(q, ["z", "y"]) == 0.0


def test_forbidden_violations():
    q = _q(
        judgments=[QrelJudgment(product_id="a", relevance=Relevance.ideal)],
        forbidden_products=["bad"],
    )
    assert metrics.forbidden_violations(q, ["a", "bad", "c"], 6) == 1
    assert metrics.forbidden_violations(q, ["a", "c"], 6) == 0


# --- integritatea qrels ------------------------------------------------------


def test_qrels_rejects_relevant_and_forbidden_overlap():
    with pytest.raises(ValidationError):
        _q(
            judgments=[QrelJudgment(product_id="a", relevance=Relevance.ideal)],
            forbidden_products=["a"],
        )


def test_qrels_rejects_duplicate_forbidden():
    with pytest.raises(ValidationError):
        _q(forbidden_products=["a", "a"])


def test_qrelsset_rejects_duplicate_ids():
    with pytest.raises(ValidationError):
        QrelsSet(business_id="b", queries=[_q(id="dup"), _q(id="dup")])


# --- splituri single-use -----------------------------------------------------


def test_split_is_deterministic():
    from src.evals.retrieval.splits import assign_split

    assert assign_split("ex-crema-gras-buget") == assign_split("ex-crema-gras-buget")


def test_gate_to_holdout_mapping_is_single_use():
    assert holdout_slice_for_gate("NX-207") == Split.holdout_h1
    assert holdout_slice_for_gate("NX-209") == Split.holdout_h2
    assert holdout_slice_for_gate("NX-210") == Split.holdout_h3
    # cele trei gate-uri folosesc felii DISTINCTE (anti-contaminare)
    slices = {holdout_slice_for_gate(g) for g in ("NX-207", "NX-209", "NX-210")}
    assert len(slices) == 3
    with pytest.raises(ValueError):
        holdout_slice_for_gate("NX-999")


def test_partition_covers_all_queries_without_overlap():
    qset = QrelsSet(
        business_id="b",
        queries=[_q(id=f"q{i}", category="creme" if i % 2 else "seruri") for i in range(20)],
    )
    parts = partition(qset)
    total = sum(len(v) for v in parts.values())
    assert total == 20
    seen = [q.id for v in parts.values() for q in v]
    assert len(seen) == len(set(seen))  # zero suprapunere


# --- harness pe exemplul minuscul --------------------------------------------


def test_harness_runs_on_example_qrels():
    qset = QrelsSet(**json.loads(EXAMPLE.read_text(encoding="utf-8")))
    assert len(qset.queries) == 3

    # retrieval fake perfect: întoarce produsele relevante în ordine + evită interzisele
    truth = {q.id: [j.product_id for j in q.judgments] for q in qset.queries}
    by_query = {q.query: q.id for q in qset.queries}

    def perfect(query: str):
        return truth[by_query[query]]

    report = run_benchmark(qset, perfect, RunConfig(label="skeleton-selftest", split="example"))
    assert report.n_queries == 3
    assert report.recall_at_20 == pytest.approx(1.0)
    assert report.ndcg_at_6 == pytest.approx(1.0)
    assert report.top_6_hit_rate == pytest.approx(1.0)
    assert report.forbidden_violation_rate == 0.0


def test_harness_detects_forbidden_and_misses():
    qset = QrelsSet(**json.loads(EXAMPLE.read_text(encoding="utf-8")))
    by_query = {q.query: q for q in qset.queries}

    def bad(query: str):
        q = by_query[query]
        # întoarce produsele INTERZISE + rateaza relevantele
        return list(q.forbidden_products) or ["nonexistent"]

    report = run_benchmark(qset, bad, RunConfig(label="skeleton-badcase"))
    # query-urile cu forbidden trebuie semnalate
    assert report.forbidden_violation_rate > 0.0
    assert report.top_6_hit_rate < 1.0


def test_example_hard_constraints_parse():
    """Exemplul are hard_constraints valide (schema acceptă structura truth-first)."""
    qset = QrelsSet(**json.loads(EXAMPLE.read_text(encoding="utf-8")))
    hc = qset.queries[0].hard_constraints
    assert hc and isinstance(hc[0], HardConstraint)
    assert hc[0].facet == "category"
