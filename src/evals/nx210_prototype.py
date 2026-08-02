"""NX-210 offline-only single-agent prototype orchestration.

This module is deliberately outside ``src.worker`` and ``src.tools``. It owns no production
registration point and only accepts an injected, read-only search function. ``AnswerPlanV0`` is
experimental and disposable by default; it is not the production contract planned by NX-211.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from time import perf_counter
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from src.agent.query_spec import Constraint, RuntimeQuerySpec, SafeQuerySpec, SafeVocabulary
from src.domain.search_entities import SearchEntitiesResult
from src.safety.external_data import contains_pii

Disposition = Literal["answer", "clarify", "fallback"]
SpanStage = Literal["query_spec", "search_entities", "query_refinement"]
StopReason = Literal[
    "sufficient_grounded_candidates",
    "complete_coverage",
    "exact_identifier",
    "clarification_required",
    "no_new_candidates",
    "no_new_query",
    "hard_constraint_relaxation",
    "time_budget",
    "cost_budget",
    "max_searches",
    "query_spec_unavailable",
    "search_unavailable",
    "validator_rejected",
]

_SAFE_DEGRADATIONS = frozenset(
    {
        "answer_plan_invalid",
        "cost_meter_failed",
        "query_refinement_failed",
        "query_spec_failed",
        "report_identifier_redacted",
        "rerank_blocked_pii",
        "rerank_failed",
        "rerank_invalid_response",
        "rerank_skipped_no_provider",
        "rerank_skipped_no_safe_candidates",
        "rerank_timeout",
        "search_entities_failed",
        "semantic_blocked_pii",
        "semantic_failed",
        "semantic_skipped_no_llm",
        "semantic_skipped_no_shadow_embeddings",
    }
)


class QuerySpecAgent(Protocol):
    """Single agent boundary; raw/history/profile remain transient inputs."""

    async def initial_query_spec(
        self,
        raw_message: str,
        *,
        history: Sequence[Mapping[str, str]],
        profile: Mapping[str, Any],
    ) -> RuntimeQuerySpec: ...

    async def refine_query_spec(
        self,
        raw_message: str,
        *,
        history: Sequence[Mapping[str, str]],
        profile: Mapping[str, Any],
        previous: RuntimeQuerySpec,
        result: SearchEntitiesResult,
        attempt: int,
    ) -> RuntimeQuerySpec | None: ...


SearchFn = Callable[[RuntimeQuerySpec], Awaitable[SearchEntitiesResult]]
CostFn = Callable[[], float]


@dataclass(frozen=True, slots=True)
class PrototypeBudget:
    max_searches: int = 3
    max_elapsed_ms: float = 8_000.0
    max_cost_usd: float = 0.20
    min_grounded_candidates: int = 2

    def __post_init__(self) -> None:
        if not 1 <= self.max_searches <= 3:
            raise ValueError("NX-210 prototype allows between one and three searches")
        if self.max_elapsed_ms <= 0 or self.max_cost_usd < 0:
            raise ValueError("time and cost budgets must be non-negative")
        if self.min_grounded_candidates < 1:
            raise ValueError("min_grounded_candidates must be positive")


class OfflineSpan(BaseModel):
    """PII-safe component timing; stage names are a closed vocabulary."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    stage: SpanStage
    attempt: int = Field(ge=0, le=3)
    latency_ms: float = Field(ge=0)


class AnswerPlanV0(BaseModel):
    """Minimal experimental plan. It contains IDs and fixed codes, never free-form text."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[0] = 0
    experimental: Literal[True] = True
    disposition: Disposition
    selected_product_ids: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    missing_information: tuple[str, ...] = ()
    stop_reason: StopReason
    search_attempts: int = Field(ge=0, le=3)
    degradations: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PrototypeRun:
    """In-memory result. ``report_dict`` is the only persistence projection."""

    plan: AnswerPlanV0
    safe_query_spec: SafeQuerySpec
    spans: tuple[OfflineSpan, ...]
    final_result: SearchEntitiesResult | None
    cost_usd: float | None

    def report_dict(self) -> dict[str, object]:
        return {
            "plan": self.plan.model_dump(mode="json"),
            "query_spec": self.safe_query_spec.model_dump(mode="json"),
            "spans": [span.model_dump(mode="json") for span in self.spans],
            "cost_usd": self.cost_usd,
        }


def _constraint_key(constraint: Constraint) -> tuple[str, str, str, str]:
    return (
        constraint.facet,
        constraint.op,
        repr(constraint.value),
        constraint.strength,
    )


def preserves_hard_constraints(initial: RuntimeQuerySpec, candidate: RuntimeQuerySpec) -> bool:
    """A refinement may add constraints but may never relax an initial hard constraint."""

    required = Counter(
        _constraint_key(item) for item in initial.constraints if item.strength == "hard"
    )
    offered = Counter(
        _constraint_key(item) for item in candidate.constraints if item.strength == "hard"
    )
    return all(offered[key] >= count for key, count in required.items())


def _query_fingerprint(spec: RuntimeQuerySpec) -> tuple[object, ...]:
    return (
        spec.normalized_query,
        spec.search_text,
        tuple(_constraint_key(item) for item in spec.constraints),
        spec.sort,
    )


def _eligible_ids(result: SearchEntitiesResult) -> tuple[str, ...]:
    exact = [item.product_id for item in result.candidates if item.match_class == "exact"]
    alternatives = [
        item.product_id for item in result.candidates if item.match_class == "alternative"
    ]
    return tuple([*exact, *alternatives])


def _grounded_ids(result: SearchEntitiesResult) -> tuple[str, ...]:
    return tuple(
        item.product_id
        for item in result.candidates
        if item.match_class != "rejected" and item.evidence_ids
    )


def _stop_after_result(
    result: SearchEntitiesResult,
    *,
    min_grounded_candidates: int,
) -> StopReason | None:
    eligible = _eligible_ids(result)
    if result.identifier_status == "resolve" and eligible:
        return "exact_identifier"
    if (
        len(_grounded_ids(result)) >= min_grounded_candidates
        and not result.missing_information
    ):
        return "sufficient_grounded_candidates"
    if eligible and not result.needs_refinement:
        return "complete_coverage"
    return None


def _make_plan(
    result: SearchEntitiesResult | None,
    *,
    stop_reason: StopReason,
    attempts: int,
    degradations: Sequence[str],
    vocabulary: SafeVocabulary | None,
) -> AnswerPlanV0:
    safe_degradations = [code for code in degradations if code in _SAFE_DEGRADATIONS]
    if result is None:
        return AnswerPlanV0(
            disposition="fallback",
            stop_reason=stop_reason,
            search_attempts=attempts,
            degradations=tuple(dict.fromkeys(safe_degradations)),
        )

    exact = [item for item in result.candidates if item.match_class == "exact"]
    alternatives = [item for item in result.candidates if item.match_class == "alternative"]
    selected = (exact or alternatives)[:3]
    if not selected:
        disposition: Disposition = "fallback"
    elif result.needs_refinement and not exact:
        disposition = "clarify"
    else:
        disposition = "answer"
    selected_ids = tuple(
        item.product_id for item in selected if not contains_pii(item.product_id)
    )
    if len(selected_ids) != len(selected):
        safe_degradations.append("report_identifier_redacted")
    evidence_ids = tuple(
        dict.fromkeys(
            evidence_id
            for item in selected
            for evidence_id in item.evidence_ids
            if not contains_pii(evidence_id)
        )
    )
    return AnswerPlanV0(
        disposition=disposition,
        selected_product_ids=selected_ids,
        evidence_ids=evidence_ids,
        missing_information=tuple(
            facet
            for facet in result.missing_information
            if vocabulary is not None and vocabulary.knows_facet(facet)
        ),
        stop_reason=stop_reason,
        search_attempts=attempts,
        degradations=tuple(dict.fromkeys(safe_degradations)),
    )


def validate_answer_plan_v0(
    plan: AnswerPlanV0, result: SearchEntitiesResult | None
) -> tuple[str, ...]:
    """Minimal deterministic validator for the disposable offline plan."""

    failures: list[str] = []
    if plan.disposition == "answer" and not plan.selected_product_ids:
        failures.append("answer_without_products")
    if result is None:
        if plan.selected_product_ids or plan.evidence_ids:
            failures.append("grounding_without_search_result")
        return tuple(failures)

    candidates = {item.product_id: item for item in result.candidates}
    for product_id in plan.selected_product_ids:
        candidate = candidates.get(product_id)
        if candidate is None:
            failures.append("unknown_product")
        elif candidate.match_class == "rejected":
            failures.append("rejected_product")
    allowed_evidence = {
        evidence_id
        for product_id in plan.selected_product_ids
        if product_id in candidates
        for evidence_id in candidates[product_id].evidence_ids
    }
    if any(evidence_id not in allowed_evidence for evidence_id in plan.evidence_ids):
        failures.append("unknown_evidence")
    return tuple(dict.fromkeys(failures))


def _span(stage: SpanStage, attempt: int, started: float) -> OfflineSpan:
    return OfflineSpan(
        stage=stage,
        attempt=attempt,
        latency_ms=round((perf_counter() - started) * 1_000, 3),
    )


def _budget_reason(
    started: float,
    budget: PrototypeBudget,
    cost: CostFn | None,
) -> StopReason | None:
    if (perf_counter() - started) * 1_000 > budget.max_elapsed_ms:
        return "time_budget"
    if cost is not None:
        try:
            if float(cost()) > budget.max_cost_usd:
                return "cost_budget"
        except Exception:  # noqa: BLE001 - telemetry failure cannot silence an offline run
            pass
    return None


async def run_offline_prototype(
    raw_message: str,
    *,
    history: Sequence[Mapping[str, str]],
    profile: Mapping[str, Any],
    agent: QuerySpecAgent,
    search: SearchFn,
    vocabulary: SafeVocabulary | None = None,
    budget: PrototypeBudget = PrototypeBudget(),
    cost: CostFn | None = None,
) -> PrototypeRun:
    """Run the NX-210 prototype without any production side effect or registration."""

    run_started = perf_counter()
    spans: list[OfflineSpan] = []
    degradations: list[str] = []
    attempts = 0
    result: SearchEntitiesResult | None = None
    initial: RuntimeQuerySpec | None = None
    current: RuntimeQuerySpec | None = None
    stop_reason: StopReason = "query_spec_unavailable"

    try:
        stage_started = perf_counter()
        current = await agent.initial_query_spec(
            raw_message,
            history=history,
            profile=profile,
        )
        spans.append(_span("query_spec", 0, stage_started))
        initial = current
    except Exception:  # noqa: BLE001 - offline prototype must still produce a fallback plan
        degradations.append("query_spec_failed")

    seen_queries: set[tuple[object, ...]] = set()
    seen_candidates: set[str] = set()
    while current is not None and attempts < budget.max_searches:
        if reason := _budget_reason(run_started, budget, cost):
            stop_reason = reason
            break
        fingerprint = _query_fingerprint(current)
        if fingerprint in seen_queries:
            stop_reason = "no_new_query"
            break
        seen_queries.add(fingerprint)

        attempts += 1
        try:
            stage_started = perf_counter()
            result = await search(current)
            spans.append(_span("search_entities", attempts, stage_started))
        except Exception:  # noqa: BLE001 - search outage must produce a visible fallback
            degradations.append("search_entities_failed")
            stop_reason = "search_unavailable"
            result = None
            break
        degradations.extend(result.degradations)

        if reason := _stop_after_result(
            result,
            min_grounded_candidates=budget.min_grounded_candidates,
        ):
            stop_reason = reason
            break

        candidate_ids = {item.product_id for item in result.candidates}
        if not (candidate_ids - seen_candidates):
            stop_reason = "no_new_candidates"
            break
        seen_candidates.update(candidate_ids)

        if reason := _budget_reason(run_started, budget, cost):
            stop_reason = reason
            break
        if attempts >= budget.max_searches:
            stop_reason = "max_searches"
            break

        try:
            stage_started = perf_counter()
            refined = await agent.refine_query_spec(
                raw_message,
                history=history,
                profile=profile,
                previous=current,
                result=result,
                attempt=attempts,
            )
            spans.append(_span("query_refinement", attempts, stage_started))
        except Exception:  # noqa: BLE001 - refinement failure becomes clarification/fallback
            degradations.append("query_refinement_failed")
            refined = None
        if refined is None:
            stop_reason = "clarification_required"
            break
        if initial is not None and not preserves_hard_constraints(initial, refined):
            stop_reason = "hard_constraint_relaxation"
            break
        current = refined

    safe_spec = (current or initial).to_safe(vocabulary) if (current or initial) else SafeQuerySpec()
    plan = _make_plan(
        result,
        stop_reason=stop_reason,
        attempts=attempts,
        degradations=degradations,
        vocabulary=vocabulary,
    )
    if validate_answer_plan_v0(plan, result):
        plan = _make_plan(
            None,
            stop_reason="validator_rejected",
            attempts=attempts,
            degradations=[*plan.degradations, "answer_plan_invalid"],
            vocabulary=vocabulary,
        )
    measured_cost: float | None = None
    if cost is not None:
        try:
            measured_cost = float(cost())
            if not math.isfinite(measured_cost) or measured_cost < 0:
                raise ValueError("cost meter returned an invalid value")
        except Exception:  # noqa: BLE001 - telemetry failure is a named degradation
            plan = plan.model_copy(
                update={
                    "degradations": tuple(
                        dict.fromkeys((*plan.degradations, "cost_meter_failed"))
                    )
                }
            )
    return PrototypeRun(
        plan=plan,
        safe_query_spec=safe_spec,
        spans=tuple(spans),
        final_result=result,
        cost_usd=measured_cost,
    )
