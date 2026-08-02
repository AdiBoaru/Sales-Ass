from src.evals.nx210_blind import (
    DecisionPolicy,
    ResponseArtifact,
    RubricScores,
    RunMetrics,
    evaluate_decision,
)
from src.evals.nx210_h3 import (
    BlindRating,
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


def _ready_qrels() -> QrelsSet:
    queries = []
    counts = {split: 0 for split in Split}
    target = {
        Split.tuning: 50,
        Split.holdout_h1: 50,
        Split.holdout_h2: 50,
        Split.holdout_h3: 50,
    }
    index = 0
    while any(counts[split] < target[split] for split in Split):
        case_id = f"case-{index}"
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


def _artifact(qrels: QrelsSet, variant: str) -> RunArtifact:
    selected = [
        query for query in qrels.queries if assign_split(query.split_group_id) is Split.holdout_h3
    ]
    return RunArtifact(
        business_id=qrels.business_id,
        variant=variant,
        cases=tuple(
            RunCase(
                case_id=query.id,
                response=ResponseArtifact(
                    text=f"raspuns {variant}",
                    product_ids=(query.judgments[0].product_id,),
                ),
                metrics=RunMetrics(latency_ms=500, cost_usd=0.01),
            )
            for query in selected
        ),
    )


def test_readiness_fails_closed_without_policy_and_sufficient_h3():
    qrels = QrelsSet(business_id="business-1", queries=[])

    report = assess_h3_readiness(qrels, None)

    assert report.ready is False
    assert "h3_sample_too_small" in report.blocking_codes
    assert "decision_policy_unavailable" in report.unavailable_codes
    assert report.policy_fingerprint is None


def test_full_h3_flow_keeps_reveal_separate_and_only_nominates_for_adi():
    qrels = _ready_qrels()
    policy = _policy()
    bundle = build_h3_packets(
        qrels,
        _artifact(qrels, "baseline"),
        _artifact(qrels, "candidate"),
        policy,
        seed="sealed-seed",
    )

    assert bundle.readiness.ready is True
    assert len(bundle.packets.pairs) == 50
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
