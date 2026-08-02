"""NX-210 quality H3 readiness, packet sealing, and post-rating reveal."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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
from src.safety.external_data import contains_pii

MIN_QUALITY_H3_PAIRS = 50
MIN_HARD_CASES = 30
MIN_SIMPLE_FACT_CASES = 10

_FACT_FAILURES = frozenset(
    {"fact_error", "missing_link", "missing_price", "missing_stock", "ungrounded_claim"}
)
_HARD_FAILURES = frozenset({"hard_constraint_violation", "silence", "validator_rejected"})


def _reject_pii(value: str, *, field: str) -> str:
    if contains_pii(value):
        raise ValueError(f"{field} contains PII")
    return value


class QualityHistoryMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    role: Literal["user", "assistant"]
    content: str

    @field_validator("content")
    @classmethod
    def _content_has_no_pii(cls, value: str) -> str:
        return _reject_pii(value, field="history content")


class QualityH3Case(BaseModel):
    """One sealed conversational quality case owned by NX-202, not retrieval qrels."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str
    prompt: str
    history: tuple[QualityHistoryMessage, ...] = ()
    profile: dict[str, str | int | float | bool] = Field(default_factory=dict)
    provenance: Literal["real_sanitized", "synthetic", "paraphrase"]
    category: str
    case_class: Literal["hard", "simple_fact"]
    human_verified: Literal[True]
    holdout_sealed: Literal[True]

    @field_validator("case_id", "prompt", "category")
    @classmethod
    def _text_has_no_pii(cls, value: str, info) -> str:
        return _reject_pii(value, field=info.field_name)

    @field_validator("profile")
    @classmethod
    def _profile_has_no_pii(
        cls, value: dict[str, str | int | float | bool]
    ) -> dict[str, str | int | float | bool]:
        for key, item in value.items():
            _reject_pii(key, field="profile key")
            if isinstance(item, str):
                _reject_pii(item, field=f"profile[{key}]")
        return value


class QualityH3Set(BaseModel):
    """Sealed NX-202 conversational holdout used for the NX-210 quality decision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    business_id: str
    sealed: Literal[True]
    cases: tuple[QualityH3Case, ...]

    @model_validator(mode="after")
    def _unique_case_ids(self) -> QualityH3Set:
        ids = [case.case_id for case in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("quality H3 set contains duplicate case IDs")
        return self


class RunCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str
    response: ResponseArtifact
    metrics: RunMetrics


class RunArtifact(BaseModel):
    """One system's sealed outputs for the exact quality H3 case IDs."""

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
    """PII-safe projection; neither sealed prompts nor detailed qrels errors escape."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    gate: Literal["NX-210"] = "NX-210"
    split: Literal["quality_holdout_h3"] = "quality_holdout_h3"
    inventory_count: int = Field(ge=0)
    rewrite_count: int = Field(ge=0)
    sealed_case_count: int = Field(ge=0)
    hard_case_count: int = Field(ge=0)
    simple_fact_case_count: int = Field(ge=0)
    required_pairs: int = Field(ge=30)
    required_hard_cases: int = Field(ge=30)
    required_simple_fact_cases: int = Field(ge=1)
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

    @model_validator(mode="after")
    def _unique_pair_ids(self) -> RevealEnvelope:
        ids = [entry.pair_id for entry in self.reveal]
        if len(ids) != len(set(ids)):
            raise ValueError("reveal contains duplicate pair IDs")
        return self


class RatingsEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    policy_fingerprint: str
    ratings: tuple[BlindRating, ...]

    @model_validator(mode="after")
    def _unique_pair_ids(self) -> RatingsEnvelope:
        ids = [rating.pair_id for rating in self.ratings]
        if len(ids) != len(set(ids)):
            raise ValueError("ratings contain duplicate pair IDs")
        return self


@dataclass(frozen=True, slots=True)
class H3PacketBundle:
    packets: PacketEnvelope
    reveal: RevealEnvelope
    readiness: H3Readiness


class H3NotReadyError(ValueError):
    """Raised before any quality holdout prompt is copied into evaluator artifacts."""


def assess_h3_readiness(
    quality_h3: QualityH3Set | None,
    policy: DecisionPolicy | None,
    retrieval_qrels: QrelsSet | None,
    *,
    inventory_count: int = 0,
    rewrite_count: int = 0,
) -> H3Readiness:
    """Check the quality dataset, frozen policy, and the independent NX-209 dependency."""

    required_pairs = policy.min_pairs if policy is not None else MIN_QUALITY_H3_PAIRS
    cases = quality_h3.cases if quality_h3 is not None else ()
    hard_count = sum(case.case_class == "hard" for case in cases)
    simple_fact_count = sum(case.case_class == "simple_fact" for case in cases)
    blocking: list[str] = []
    unavailable: list[str] = []

    if quality_h3 is None:
        unavailable.append("quality_holdout_unavailable")
    if len(cases) < required_pairs:
        blocking.append("quality_h3_sample_too_small")
    if hard_count < MIN_HARD_CASES:
        blocking.append("quality_h3_hard_slice_too_small")
    if simple_fact_count < MIN_SIMPLE_FACT_CASES:
        blocking.append("quality_h3_simple_fact_slice_too_small")
    if policy is None:
        unavailable.append("decision_policy_unavailable")

    if retrieval_qrels is None:
        unavailable.append("nx209_retrieval_readiness_unavailable")
    else:
        retrieval = gate_readiness(retrieval_qrels, "NX-209")
        if not retrieval.ready:
            blocking.append("nx209_retrieval_gate_blocked")

    return H3Readiness(
        inventory_count=inventory_count,
        rewrite_count=rewrite_count,
        sealed_case_count=len(cases),
        hard_case_count=hard_count,
        simple_fact_case_count=simple_fact_count,
        required_pairs=required_pairs,
        required_hard_cases=MIN_HARD_CASES,
        required_simple_fact_cases=MIN_SIMPLE_FACT_CASES,
        policy_fingerprint=policy.fingerprint if policy is not None else None,
        blocking_codes=tuple(dict.fromkeys(blocking)),
        unavailable_codes=tuple(dict.fromkeys(unavailable)),
    )


def _index_run(artifact: RunArtifact, expected_variant: str) -> dict[str, RunCase]:
    if artifact.variant != expected_variant:
        raise ValueError(f"expected {expected_variant} artifact")
    return {case.case_id: case for case in artifact.cases}


def _evaluation_prompt(case: QualityH3Case) -> str:
    """Render sanitized context without variant or run metadata."""

    sections: list[str] = []
    if case.profile:
        sections.append("PROFILE\n" + json.dumps(case.profile, ensure_ascii=False, sort_keys=True))
    if case.history:
        history = "\n".join(
            f"{message.role.upper()}: {message.content}" for message in case.history
        )
        sections.append("HISTORY\n" + history)
    sections.append("CURRENT USER MESSAGE\n" + case.prompt)
    return "\n\n".join(sections)


def build_h3_packets(
    quality_h3: QualityH3Set,
    retrieval_qrels: QrelsSet,
    baseline: RunArtifact,
    candidate: RunArtifact,
    policy: DecisionPolicy,
    *,
    seed: str,
) -> H3PacketBundle:
    """Open quality H3 only after all independent prerequisites pass."""

    readiness = assess_h3_readiness(quality_h3, policy, retrieval_qrels)
    if not readiness.ready:
        codes = (*readiness.blocking_codes, *readiness.unavailable_codes)
        raise H3NotReadyError("H3 is not ready: " + ",".join(codes))
    business_ids = {
        quality_h3.business_id,
        retrieval_qrels.business_id,
        baseline.business_id,
        candidate.business_id,
    }
    if len(business_ids) != 1:
        raise ValueError("quality, retrieval, and run artifacts use different business_id values")

    expected_ids = {case.case_id for case in quality_h3.cases}
    baseline_by_id = _index_run(baseline, "baseline")
    candidate_by_id = _index_run(candidate, "candidate")
    for label, found in (("baseline", baseline_by_id), ("candidate", candidate_by_id)):
        if set(found) != expected_ids:
            raise ValueError(f"{label} artifact does not exactly cover quality H3 case IDs")

    cases = [
        BlindCase(
            case_id=case.case_id,
            prompt=_evaluation_prompt(case),
            baseline=baseline_by_id[case.case_id].response,
            candidate=candidate_by_id[case.case_id].response,
            baseline_metrics=baseline_by_id[case.case_id].metrics,
            candidate_metrics=candidate_by_id[case.case_id].metrics,
        )
        for case in quality_h3.cases
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
    """Join blind ratings with the sealed key only after human scoring is complete."""

    if ratings.policy_fingerprint != reveal.policy_fingerprint:
        raise ValueError("ratings and reveal use different policy fingerprints")
    ratings_by_id = {rating.pair_id: rating for rating in ratings.ratings}
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
