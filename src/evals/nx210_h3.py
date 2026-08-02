"""NX-210 H3 readiness, packet sealing, and post-rating reveal."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.evals.nx210_blind import (
    BlindCase,
    BlindPair,
    DecisionPolicy,
    PairedObservation,
    ResponseArtifact,
    RevealEntry,
    RubricScores,
    RunMetrics,
    build_blind_pairs,
)
from src.evals.retrieval.readiness import gate_readiness
from src.evals.retrieval.schema import QrelsSet
from src.evals.retrieval.splits import Split, partition

_FACT_FAILURES = frozenset(
    {"fact_error", "missing_link", "missing_price", "missing_stock", "ungrounded_claim"}
)
_HARD_FAILURES = frozenset({"hard_constraint_violation", "silence", "validator_rejected"})


class RunCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str
    response: ResponseArtifact
    metrics: RunMetrics


class RunArtifact(BaseModel):
    """One system's sealed outputs for the exact H3 case IDs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    business_id: str
    variant: Literal["baseline", "candidate"]
    cases: tuple[RunCase, ...]

    @model_validator(mode="after")
    def _unique_case_ids(self) -> RunArtifact:
        ids = [case.case_id for case in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("run artifact contains duplicate case IDs")
        return self


class BlindRating(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    pair_id: str
    response_a_scores: RubricScores
    response_b_scores: RubricScores


class H3Readiness(BaseModel):
    """PII-safe readiness projection; detailed qrels errors stay local."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    gate: Literal["NX-210"] = "NX-210"
    split: Literal["holdout_h3"] = "holdout_h3"
    query_count: int = Field(ge=0)
    verified_family_count: int = Field(ge=0)
    required_pairs: int = Field(ge=30)
    policy_fingerprint: str | None
    blocking_codes: tuple[str, ...]
    unavailable_codes: tuple[str, ...]

    @property
    def ready(self) -> bool:
        return not self.blocking_codes and not self.unavailable_codes


class PacketEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    policy_fingerprint: str
    pairs: tuple[BlindPair, ...]


class RevealEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    policy_fingerprint: str
    reveal: tuple[RevealEntry, ...]


class RatingsEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    policy_fingerprint: str
    ratings: tuple[BlindRating, ...]


@dataclass(frozen=True, slots=True)
class H3PacketBundle:
    packets: PacketEnvelope
    reveal: RevealEnvelope
    readiness: H3Readiness


class H3NotReadyError(ValueError):
    """Raised before any holdout prompt is copied into evaluator artifacts."""


def assess_h3_readiness(
    qrels: QrelsSet,
    policy: DecisionPolicy | None,
) -> H3Readiness:
    gate = gate_readiness(qrels, "NX-210")
    required_pairs = policy.min_pairs if policy is not None else 50
    blocking: list[str] = []
    unavailable: list[str] = []
    if gate.blocking:
        blocking.append("qrels_integrity_blocked")
    if gate.unavailable:
        unavailable.append("qrels_check_unavailable")
    if gate.query_count < required_pairs:
        blocking.append("h3_sample_too_small")
    if policy is None:
        unavailable.append("decision_policy_unavailable")
    return H3Readiness(
        query_count=gate.query_count,
        verified_family_count=gate.verified_family_count,
        required_pairs=required_pairs,
        policy_fingerprint=policy.fingerprint if policy is not None else None,
        blocking_codes=tuple(dict.fromkeys(blocking)),
        unavailable_codes=tuple(dict.fromkeys(unavailable)),
    )


def _index_run(artifact: RunArtifact, expected_variant: str) -> dict[str, RunCase]:
    if artifact.variant != expected_variant:
        raise ValueError(f"expected {expected_variant} artifact")
    return {case.case_id: case for case in artifact.cases}


def build_h3_packets(
    qrels: QrelsSet,
    baseline: RunArtifact,
    candidate: RunArtifact,
    policy: DecisionPolicy,
    *,
    seed: str,
) -> H3PacketBundle:
    """Open H3 only after readiness passes and seal evaluator/reveal artifacts separately."""

    readiness = assess_h3_readiness(qrels, policy)
    if not readiness.ready:
        codes = (*readiness.blocking_codes, *readiness.unavailable_codes)
        raise H3NotReadyError("H3 is not ready: " + ",".join(codes))
    if baseline.business_id != qrels.business_id or candidate.business_id != qrels.business_id:
        raise ValueError("run artifact business_id does not match qrels")

    selected = partition(qrels)[Split.holdout_h3]
    expected_ids = {query.id for query in selected}
    baseline_by_id = _index_run(baseline, "baseline")
    candidate_by_id = _index_run(candidate, "candidate")
    for label, found in (("baseline", baseline_by_id), ("candidate", candidate_by_id)):
        if set(found) != expected_ids:
            raise ValueError(f"{label} artifact does not exactly cover H3 case IDs")

    cases = [
        BlindCase(
            case_id=query.id,
            prompt=query.query,
            baseline=baseline_by_id[query.id].response,
            candidate=candidate_by_id[query.id].response,
            baseline_metrics=baseline_by_id[query.id].metrics,
            candidate_metrics=candidate_by_id[query.id].metrics,
        )
        for query in selected
    ]
    pairs, reveal = build_blind_pairs(cases, seed=seed)
    return H3PacketBundle(
        packets=PacketEnvelope(policy_fingerprint=policy.fingerprint, pairs=pairs),
        reveal=RevealEnvelope(policy_fingerprint=policy.fingerprint, reveal=reveal),
        readiness=readiness,
    )


def _failure_count(metrics: RunMetrics, allowed: frozenset[str]) -> int:
    return sum(code in allowed for code in metrics.deterministic_failures)


def reveal_observations(
    ratings: RatingsEnvelope,
    reveal: RevealEnvelope,
) -> tuple[PairedObservation, ...]:
    """Join ratings with the sealed key only after human scoring is complete."""

    if ratings.policy_fingerprint != reveal.policy_fingerprint:
        raise ValueError("ratings and reveal use different policy fingerprints")
    ratings_by_id = {rating.pair_id: rating for rating in ratings.ratings}
    if len(ratings_by_id) != len(ratings.ratings):
        raise ValueError("ratings contain duplicate pair IDs")
    reveal_by_id = {entry.pair_id: entry for entry in reveal.reveal}
    if set(ratings_by_id) != set(reveal_by_id):
        raise ValueError("ratings do not exactly cover reveal pair IDs")

    observations: list[PairedObservation] = []
    for entry in reveal.reveal:
        rating = ratings_by_id[entry.pair_id]
        candidate_scores, baseline_scores = (
            (rating.response_a_scores, rating.response_b_scores)
            if entry.candidate_side == "A"
            else (rating.response_b_scores, rating.response_a_scores)
        )
        observations.append(
            PairedObservation(
                pair_id=entry.pair_id,
                baseline_scores=baseline_scores,
                candidate_scores=candidate_scores,
                baseline_fact_failures=_failure_count(entry.baseline_metrics, _FACT_FAILURES),
                candidate_fact_failures=_failure_count(entry.candidate_metrics, _FACT_FAILURES),
                candidate_hard_failures=_failure_count(entry.candidate_metrics, _HARD_FAILURES),
                candidate_latency_ms=entry.candidate_metrics.latency_ms,
                candidate_cost_usd=entry.candidate_metrics.cost_usd,
            )
        )
    return tuple(observations)
