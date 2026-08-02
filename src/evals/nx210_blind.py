"""NX-210 blind paired evaluation and pre-registered decision rule."""

from __future__ import annotations

import hashlib
import json
import math
import random
from dataclasses import dataclass
from statistics import mean
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from src.safety.external_data import contains_pii

Side = Literal["A", "B"]
Decision = Literal["insufficient_data", "no_go", "candidate_for_adi_review"]
DeterministicFailure = Literal[
    "fact_error",
    "hard_constraint_violation",
    "missing_link",
    "missing_price",
    "missing_stock",
    "silence",
    "ungrounded_claim",
    "validator_rejected",
]


class ResponseArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str
    product_ids: tuple[str, ...] = ()


class RunMetrics(BaseModel):
    """Sealed until ratings are complete so metadata cannot unblind evaluators."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    deterministic_failures: tuple[DeterministicFailure, ...] = ()
    latency_ms: float = Field(ge=0)
    cost_usd: float | None = Field(default=None, ge=0)


@dataclass(frozen=True, slots=True)
class BlindCase:
    case_id: str
    prompt: str
    baseline: ResponseArtifact
    candidate: ResponseArtifact
    baseline_metrics: RunMetrics
    candidate_metrics: RunMetrics


class BlindPair(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    pair_id: str
    prompt: str
    response_a: ResponseArtifact
    response_b: ResponseArtifact


class RevealEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    pair_id: str
    case_id: str
    candidate_side: Side
    baseline_metrics: RunMetrics
    candidate_metrics: RunMetrics


class RubricScores(BaseModel):
    """Human rubric frozen before H3; all dimensions use the same 1-5 scale."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_success: float = Field(ge=1, le=5)
    factual_grounding: float = Field(ge=1, le=5)
    constraint_adherence: float = Field(ge=1, le=5)
    clarity: float = Field(ge=1, le=5)

    @property
    def overall(self) -> float:
        return mean(self.model_dump().values())


class DimensionDeltas(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_success: float
    factual_grounding: float
    constraint_adherence: float
    clarity: float


class PairedObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    pair_id: str
    baseline_scores: RubricScores
    candidate_scores: RubricScores
    baseline_fact_failures: int = Field(default=0, ge=0)
    candidate_fact_failures: int = Field(default=0, ge=0)
    candidate_hard_failures: int = Field(default=0, ge=0)
    candidate_latency_ms: float = Field(ge=0)
    candidate_cost_usd: float | None = Field(default=None, ge=0)


class DecisionPolicy(BaseModel):
    """Criterion frozen before opening H3; its hash belongs in every report."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal["nx210-v0"] = "nx210-v0"
    min_pairs: int = Field(default=50, ge=30)
    practical_delta: float = Field(default=0.25, gt=0)
    confidence: float = Field(default=0.95, gt=0.5, lt=1)
    bootstrap_samples: int = Field(default=5_000, ge=1_000)
    provisional_slo_ms: float = Field(gt=0)
    max_cost_usd: float = Field(ge=0)

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()


class DecisionReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    decision: Decision
    n_pairs: int
    mean_delta: float | None
    ci_low: float | None
    ci_high: float | None
    fact_regressions: int
    dimension_mean_deltas: DimensionDeltas | None
    hard_failures: int
    candidate_latency_p90_ms: float | None
    candidate_cost_mean_usd: float | None
    failures: tuple[str, ...]
    policy_fingerprint: str
    adi_signature_required: Literal[True] = True


def _safe_report_text(text: str) -> str:
    """Fail closed: a report never persists a string that trips the shared PII boundary."""

    return "[REDACTED]" if contains_pii(text) else text


def _safe_artifact(artifact: ResponseArtifact) -> ResponseArtifact:
    return artifact.model_copy(
        update={
            "text": _safe_report_text(artifact.text),
            "product_ids": tuple(
                product_id
                for product_id in artifact.product_ids
                if not contains_pii(product_id)
            ),
        }
    )


def build_blind_pairs(
    cases: list[BlindCase],
    *,
    seed: str,
) -> tuple[tuple[BlindPair, ...], tuple[RevealEntry, ...]]:
    """Create evaluator packets and a separate reveal key with deterministic randomization."""

    pairs: list[BlindPair] = []
    reveal: list[RevealEntry] = []
    for case in cases:
        pair_id = hashlib.sha256(f"{seed}:pair:{case.case_id}".encode()).hexdigest()[:16]
        candidate_side: Side = (
            "A"
            if hashlib.sha256(f"{seed}:side:{case.case_id}".encode()).digest()[0] % 2 == 0
            else "B"
        )
        candidate = _safe_artifact(case.candidate)
        baseline = _safe_artifact(case.baseline)
        response_a, response_b = (
            (candidate, baseline) if candidate_side == "A" else (baseline, candidate)
        )
        pairs.append(
            BlindPair(
                pair_id=pair_id,
                prompt=_safe_report_text(case.prompt),
                response_a=response_a,
                response_b=response_b,
            )
        )
        reveal.append(
            RevealEntry(
                pair_id=pair_id,
                case_id=case.case_id,
                candidate_side=candidate_side,
                baseline_metrics=case.baseline_metrics,
                candidate_metrics=case.candidate_metrics,
            )
        )
    return tuple(pairs), tuple(reveal)


def paired_bootstrap_ci(
    deltas: list[float],
    *,
    confidence: float,
    samples: int,
    seed: int,
) -> tuple[float, float]:
    if not deltas:
        raise ValueError("paired bootstrap requires observations")
    rng = random.Random(seed)
    estimates = sorted(
        mean(rng.choice(deltas) for _ in deltas)
        for _sample in range(samples)
    )
    alpha = (1 - confidence) / 2
    low_index = max(0, int(alpha * samples))
    high_index = min(samples - 1, int((1 - alpha) * samples))
    return estimates[low_index], estimates[high_index]


def _nearest_rank(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(percentile * len(ordered)) - 1))
    return ordered[index]


def _dimension_deltas(observations: list[PairedObservation]) -> DimensionDeltas:
    return DimensionDeltas(
        **{
            dimension: round(
                mean(
                    getattr(item.candidate_scores, dimension)
                    - getattr(item.baseline_scores, dimension)
                    for item in observations
                ),
                4,
            )
            for dimension in RubricScores.model_fields
        }
    )


def evaluate_decision(
    observations: list[PairedObservation],
    policy: DecisionPolicy,
    *,
    bootstrap_seed: int = 210,
) -> DecisionReport:
    """Apply the pre-registered gate. A machine can only nominate a candidate for Adi."""

    deltas = [
        item.candidate_scores.overall - item.baseline_scores.overall
        for item in observations
    ]
    fact_regressions = sum(
        1
        for item in observations
        if item.candidate_fact_failures > item.baseline_fact_failures
    )
    hard_failures = sum(item.candidate_hard_failures for item in observations)
    missing_cost = any(item.candidate_cost_usd is None for item in observations)

    if len(observations) < policy.min_pairs:
        return DecisionReport(
            decision="insufficient_data",
            n_pairs=len(observations),
            mean_delta=round(mean(deltas), 4) if deltas else None,
            ci_low=None,
            ci_high=None,
            fact_regressions=fact_regressions,
            hard_failures=hard_failures,
            candidate_latency_p90_ms=None,
            candidate_cost_mean_usd=None,
            dimension_mean_deltas=_dimension_deltas(observations) if observations else None,
            failures=("insufficient_pairs",),
            policy_fingerprint=policy.fingerprint,
        )

    ci_low, ci_high = paired_bootstrap_ci(
        deltas,
        confidence=policy.confidence,
        samples=policy.bootstrap_samples,
        seed=bootstrap_seed,
    )
    delta = mean(deltas)
    latency_p90 = _nearest_rank(
        [item.candidate_latency_ms for item in observations],
        0.90,
    )
    costs = [
        float(item.candidate_cost_usd)
        for item in observations
        if item.candidate_cost_usd is not None
    ]
    cost_mean = mean(costs) if costs else None
    failures: list[str] = []
    if delta < policy.practical_delta:
        failures.append("practical_delta_not_met")
    if ci_low <= 0:
        failures.append("confidence_interval_crosses_zero")
    if fact_regressions:
        failures.append("fact_regression")
    if hard_failures:
        failures.append("hard_constraint_failure")
    if latency_p90 > policy.provisional_slo_ms:
        failures.append("provisional_slo_exceeded")
    if missing_cost:
        failures.append("cost_unavailable")
    elif cost_mean is not None and cost_mean > policy.max_cost_usd:
        failures.append("cost_cap_exceeded")

    return DecisionReport(
        decision="no_go" if failures else "candidate_for_adi_review",
        n_pairs=len(observations),
        mean_delta=round(delta, 4),
        ci_low=round(ci_low, 4),
        ci_high=round(ci_high, 4),
        fact_regressions=fact_regressions,
        hard_failures=hard_failures,
        candidate_latency_p90_ms=round(latency_p90, 3),
        candidate_cost_mean_usd=round(cost_mean, 6) if cost_mean is not None else None,
        dimension_mean_deltas=_dimension_deltas(observations),
        failures=tuple(failures),
        policy_fingerprint=policy.fingerprint,
    )
