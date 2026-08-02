from src.evals.nx210_blind import (
    BlindCase,
    DecisionPolicy,
    PairedObservation,
    ResponseArtifact,
    build_blind_pairs,
    evaluate_decision,
    paired_bootstrap_ci,
)


def _artifact(text: str) -> ResponseArtifact:
    return ResponseArtifact(text=text, latency_ms=100, cost_usd=0.01)


def _policy() -> DecisionPolicy:
    return DecisionPolicy(
        provisional_slo_ms=1_000,
        max_cost_usd=0.05,
        bootstrap_samples=1_000,
    )


def _observation(index: int, *, delta=1.0, facts=0, hard=0, cost=0.01):
    return PairedObservation(
        pair_id=f"pair-{index}",
        baseline_score=3.0,
        candidate_score=3.0 + delta,
        baseline_fact_failures=0,
        candidate_fact_failures=facts,
        candidate_hard_failures=hard,
        candidate_latency_ms=500,
        candidate_cost_usd=cost,
    )


def test_blind_packets_are_deterministic_and_keep_reveal_separate():
    cases = [
        BlindCase(
            case_id=f"case-{index}",
            prompt="caut un ser",
            baseline=_artifact("baseline"),
            candidate=_artifact("candidate"),
        )
        for index in range(8)
    ]

    pairs, reveal = build_blind_pairs(cases, seed="sealed-seed")
    repeated, repeated_reveal = build_blind_pairs(cases, seed="sealed-seed")

    assert pairs == repeated and reveal == repeated_reveal
    assert {item.candidate_side for item in reveal} == {"A", "B"}
    assert all("candidate_side" not in pair.model_dump() for pair in pairs)


def test_blind_artifact_fails_closed_on_pii():
    pairs, _reveal = build_blind_pairs(
        [
            BlindCase(
                case_id="pii",
                prompt="Sunt Ion Popescu si caut un ser",
                baseline=_artifact("telefon 0712 345 678"),
                candidate=_artifact("raspuns sigur"),
            )
        ],
        seed="sealed",
    )

    assert pairs[0].prompt == "[REDACTED]"
    assert "0712" not in str(pairs[0].model_dump())


def test_decision_refuses_small_sample_before_opening_gate():
    report = evaluate_decision([_observation(index) for index in range(29)], _policy())

    assert report.decision == "insufficient_data"
    assert report.failures == ("insufficient_pairs",)
    assert report.ci_low is None


def test_clear_paired_win_only_becomes_candidate_for_adi_review():
    report = evaluate_decision([_observation(index) for index in range(30)], _policy())

    assert report.decision == "candidate_for_adi_review"
    assert report.adi_signature_required is True
    assert report.mean_delta == 1.0
    assert report.ci_low == 1.0
    assert report.failures == ()


def test_one_fact_regression_forces_no_go():
    observations = [_observation(index) for index in range(30)]
    observations[7] = _observation(7, facts=1)

    report = evaluate_decision(observations, _policy())

    assert report.decision == "no_go"
    assert report.fact_regressions == 1
    assert "fact_regression" in report.failures


def test_missing_cost_is_not_interpreted_as_zero():
    observations = [_observation(index) for index in range(30)]
    observations[0] = _observation(0, cost=None)

    report = evaluate_decision(observations, _policy())

    assert report.decision == "no_go"
    assert "cost_unavailable" in report.failures


def test_paired_bootstrap_is_reproducible():
    values = [0.1, 0.2, 0.4, 0.8]

    first = paired_bootstrap_ci(values, confidence=0.95, samples=1_000, seed=210)
    second = paired_bootstrap_ci(values, confidence=0.95, samples=1_000, seed=210)

    assert first == second
