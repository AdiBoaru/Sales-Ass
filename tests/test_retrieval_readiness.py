import pytest

from src.evals.retrieval.readiness import gate_readiness
from src.evals.retrieval.schema import Provenance, QrelJudgment, QrelsQuery, QrelsSet, Relevance
from src.evals.retrieval.splits import Split, assign_split


def _id_in(split: Split) -> str:
    for number in range(10_000):
        candidate = f"q-{number}"
        if assign_split(candidate) is split:
            return candidate
    raise AssertionError(f"No id found for {split.value}")


def _q(query_id: str) -> QrelsQuery:
    return QrelsQuery(
        id=query_id,
        query="ser pentru ten sensibil",
        provenance=Provenance.real_sanitized,
        category="seruri",
        human_verified=True,
        catalog_version="catalog-v1",
        judgments=[QrelJudgment(product_id="product-1", relevance=Relevance.ideal)],
    )


def test_gate_readiness_reports_the_target_holdout_without_opening_it():
    qset = QrelsSet(business_id="business-1", queries=[_q(_id_in(Split.holdout_h1))])

    report = gate_readiness(qset, "NX-207")

    assert report.split == "holdout_h1"
    assert report.query_count == 1
    assert report.verified_family_count == 1
    assert report.ready is False
    assert any("sub pragul" in issue for issue in report.blocking)


def test_gate_readiness_rejects_unknown_gate():
    qset = QrelsSet(business_id="business-1", queries=[])

    with pytest.raises(ValueError, match="unknown gate"):
        gate_readiness(qset, "NX-999")
