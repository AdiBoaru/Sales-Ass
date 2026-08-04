import pytest
from pydantic import ValidationError

from src.evals.nx210_blind import (
    DecisionPolicy,
    ResponseArtifact,
    RubricScores,
    RunMetrics,
    evaluate_decision,
)
from src.evals.nx210_h3 import (
    BlindRating,
    QualityH3Case,
    QualityH3Set,
    QualityHistoryMessage,
    RatingsEnvelope,
    RunArtifact,
    RunCase,
    assess_h3_readiness,
    build_h3_packets,
    reveal_observations,
)
from src.evals.retrieval.schema import (
    Provenance,
    QrelJudgment,
    QrelsQuery,
    QrelsSet,
    Relevance,
)
from src.evals.retrieval.splits import Split, assign_split


def _policy() -> DecisionPolicy:
    return DecisionPolicy(
        provisional_slo_ms=1_000,
        max_cost_usd=0.05,
        bootstrap_samples=1_000,
    )


def _scores(value: float) -> RubricScores:
    return RubricScores(
        task_success=value,
        factual_grounding=value,
        constraint_adherence=value,
        clarity=value,
    )


def _quality_h3() -> QualityH3Set:
    return QualityH3Set(
        business_id="business-1",
        sealed=True,
        cases=tuple(
            QualityH3Case(
                case_id=f"quality-{index}",
                prompt=f"recomanda produsul controlat {index}",
                history=(
                    QualityHistoryMessage(role="user", content="am ten sensibil"),
                    QualityHistoryMessage(role="assistant", content="am notat preferinta"),
                ),
                profile={"skin_type": "sensitive"},
                provenance="real_sanitized",
                category="seruri",
                case_class="hard" if index < 30 else "simple_fact",
                human_verified=True,
                holdout_sealed=True,
            )
            for index in range(50)
        ),
    )


def _retrieval_qrels_ready() -> QrelsSet:
    queries = []
    counts = {split: 0 for split in Split}
    target = {split: 50 for split in Split}
    index = 0
    while any(counts[split] < target[split] for split in Split):
        case_id = f"retrieval-{index}"
        split = assign_split(case_id)
        index += 1
        if counts[split] >= target[split]:
            continue
        counts[split] += 1
        queries.append(
            QrelsQuery(
                id=case_id,
                query=f"query controlat {case_id}",
                provenance=Provenance.real_sanitized,
                category="seruri",
                human_verified=True,
                family_id=f"family-{case_id}",
                split_group_id=case_id,
                catalog_version="catalog-v1",
                judgments=[
                    QrelJudgment(product_id=f"product-{case_id}", relevance=Relevance.ideal)
                ],
            )
        )
    return QrelsSet(business_id="business-1", queries=queries)


def _artifact(quality_h3: QualityH3Set, variant: str) -> RunArtifact:
    return RunArtifact(
        business_id=quality_h3.business_id,
        variant=variant,
        cases=tuple(
            RunCase(
                case_id=case.case_id,
                response=ResponseArtifact(
                    text=f"raspuns {variant}",
                    product_ids=(f"product-{case.case_id}",),
                ),
                metrics=RunMetrics(latency_ms=500, cost_usd=0.01),
            )
            for case in quality_h3.cases
        ),
    )


def test_readiness_fails_closed_when_inputs_are_unavailable():
    report = assess_h3_readiness(None, None, None, inventory_count=75, rewrite_count=27)

    assert report.ready is False
    assert report.sealed_case_count == 0
    assert report.inventory_count == 75
    assert "quality_h3_sample_too_small" in report.blocking_codes
    assert "quality_holdout_unavailable" in report.unavailable_codes
    assert "decision_policy_unavailable" in report.unavailable_codes
    assert "nx209_retrieval_readiness_unavailable" in report.unavailable_codes


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("prompt", "scrie-mi la test@example.com"),
        ("history", ({"role": "user", "content": "telefon 0712345678"},)),
        ("profile", {"contact": "0712345678"}),
    ],
)
def test_quality_case_rejects_pii(field, value):
    kwargs = {
        "case_id": "quality-safe",
        "prompt": "recomanda un ser",
        "history": (),
        "profile": {},
        "provenance": "real_sanitized",
        "category": "seruri",
        "case_class": "hard",
        "human_verified": True,
        "holdout_sealed": True,
    }
    kwargs[field] = value

    with pytest.raises(ValidationError):
        QualityH3Case(**kwargs)


def test_retrieval_qrels_are_an_independent_readiness_dependency():
    report = assess_h3_readiness(
        _quality_h3(),
        _policy(),
        QrelsSet(business_id="business-1", queries=[]),
    )

    assert report.sealed_case_count == 50
    assert report.hard_case_count == 30
    assert report.blocking_codes == ("nx209_retrieval_gate_blocked",)


def test_full_h3_flow_keeps_sources_separate_and_only_nominates_for_adi():
    quality_h3 = _quality_h3()
    retrieval_qrels = _retrieval_qrels_ready()
    policy = _policy()
    bundle = build_h3_packets(
        quality_h3,
        retrieval_qrels,
        _artifact(quality_h3, "baseline"),
        _artifact(quality_h3, "candidate"),
        policy,
        seed="sealed-seed",
    )

    assert bundle.readiness.ready is True
    assert bundle.readiness.hard_case_count == 30
    assert bundle.readiness.simple_fact_case_count == 20
    assert len(bundle.packets.pairs) == 50
    assert "PROFILE" in bundle.packets.pairs[0].prompt
    assert all("candidate_side" not in pair.model_dump() for pair in bundle.packets.pairs)

    side_by_pair = {entry.pair_id: entry.candidate_side for entry in bundle.reveal.reveal}
    ratings = RatingsEnvelope(
        policy_fingerprint=policy.fingerprint,
        ratings=tuple(
            BlindRating(
                pair_id=pair.pair_id,
                response_a_scores=_scores(4 if side_by_pair[pair.pair_id] == "A" else 3),
                response_b_scores=_scores(4 if side_by_pair[pair.pair_id] == "B" else 3),
            )
            for pair in bundle.packets.pairs
        ),
    )
    observations = reveal_observations(ratings, bundle.reveal)
    report = evaluate_decision(list(observations), policy)

    assert report.decision == "candidate_for_adi_review"
    assert report.mean_delta == 1.0
    assert report.adi_signature_required is True
