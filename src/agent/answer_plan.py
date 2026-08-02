"""NX-211 production AnswerPlan contract and deterministic validator.

The model proposes a plan, but server-owned evidence decides whether it may be rendered. The
contract deliberately stores no conversation text, profile, or raw tool payload.
"""

from __future__ import annotations

from collections import Counter
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.safety.external_data import contains_pii

Scalar = str | int | float | bool
ClaimType = Literal["fact", "recommendation"]
EvidenceKind = Literal["identity", "variant", "price", "stock", "url", "claim"]
ConstraintVerdict = Literal["MATCH", "MISMATCH", "UNKNOWN"]
ValidationCode = Literal[
    "action_not_successful",
    "ambiguous_product",
    "cross_tenant_evidence",
    "cross_tenant_product",
    "duplicate_selected_product",
    "fact_evidence_kind_mismatch",
    "fact_value_mismatch",
    "hard_constraint_mismatch",
    "missing_claim_evidence",
    "missing_product_evidence",
    "pii_detected",
    "stale_evidence",
    "tenant_mismatch",
    "unknown_evidence",
    "unknown_need",
    "unknown_product",
    "unknown_variant",
]


class SelectedProduct(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    product_id: str
    variant_id: str | None
    evidence_ids: tuple[str, ...]


class PlanClaim(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    claim_type: ClaimType = Field(alias="type")
    text: str
    evidence_ids: tuple[str, ...]
    need_ids: tuple[str, ...]


class FactAssertion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    product_id: str
    variant_id: str | None
    value: Scalar
    evidence_id: str


class PlanFacts(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    prices: tuple[FactAssertion, ...]
    stocks: tuple[FactAssertion, ...]
    urls: tuple[FactAssertion, ...]


class ConfirmedAction(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    action: Literal["cart_add", "checkout_link", "subscribe_back_in_stock"]
    action_id: str
    reference_id: str | None


class AnswerPlan(BaseModel):
    """Versioned production plan emitted before response rendering."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    business_id: str
    locale: str
    selected_products: tuple[SelectedProduct, ...]
    claims: tuple[PlanClaim, ...]
    facts: PlanFacts
    uncertainties: tuple[str, ...]
    unmet_constraints: tuple[str, ...]
    confirmed_actions: tuple[ConfirmedAction, ...]

    @model_validator(mode="after")
    def _contains_no_pii(self) -> AnswerPlan:
        text_values = [
            self.business_id,
            self.locale,
            *(item.product_id for item in self.selected_products),
            *(item.variant_id or "" for item in self.selected_products),
            *(evidence_id for item in self.selected_products for evidence_id in item.evidence_ids),
            *(claim.text for claim in self.claims),
            *(evidence_id for claim in self.claims for evidence_id in claim.evidence_ids),
            *(need_id for claim in self.claims for need_id in claim.need_ids),
            *self.uncertainties,
            *self.unmet_constraints,
            *(action.action_id for action in self.confirmed_actions),
            *(action.reference_id or "" for action in self.confirmed_actions),
        ]
        for collection in (self.facts.prices, self.facts.stocks, self.facts.urls):
            for fact in collection:
                text_values.extend(
                    [
                        fact.product_id,
                        fact.variant_id or "",
                        fact.evidence_id,
                        fact.value if isinstance(fact.value, str) else "",
                    ]
                )
        if any(value and contains_pii(value) for value in text_values):
            raise ValueError("AnswerPlan contains PII")
        return self


class GroundedProduct(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    product_id: str
    business_id: str
    resolution: Literal["exact", "ambiguous"]
    variant_ids: tuple[str, ...]


class EvidenceRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence_id: str
    business_id: str
    product_id: str
    variant_id: str | None
    kind: EvidenceKind
    value: Scalar
    source_version: str
    current: bool


class HardConstraintOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    product_id: str
    facet: str
    verdict: ConstraintVerdict
    unknown_is_violation: bool


class AnswerPlanContext(BaseModel):
    """Trusted server projection used to validate model output."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    business_id: str
    locale: str
    products: tuple[GroundedProduct, ...]
    evidence: tuple[EvidenceRecord, ...]
    hard_constraints: tuple[HardConstraintOutcome, ...]
    successful_action_ids: tuple[str, ...]
    known_need_ids: tuple[str, ...]


class AnswerPlanValidation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ok: bool
    failures: tuple[ValidationCode, ...]


def _same_value(left: Scalar, right: Scalar) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return left is right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return abs(float(left) - float(right)) <= 0.005
    return str(left).strip() == str(right).strip()


def validate_answer_plan(plan: AnswerPlan, context: AnswerPlanContext) -> AnswerPlanValidation:
    """Validate tenant, identity, evidence, constraints, needs, and confirmed mutations."""

    failures: list[ValidationCode] = []
    if plan.business_id != context.business_id or plan.locale != context.locale:
        failures.append("tenant_mismatch")

    products = {product.product_id: product for product in context.products}
    evidence = {item.evidence_id: item for item in context.evidence}
    selected_ids = [item.product_id for item in plan.selected_products]
    if len(selected_ids) != len(set(selected_ids)):
        failures.append("duplicate_selected_product")

    for selected in plan.selected_products:
        product = products.get(selected.product_id)
        if product is None:
            failures.append("unknown_product")
            continue
        if product.business_id != context.business_id:
            failures.append("cross_tenant_product")
        if product.resolution != "exact":
            failures.append("ambiguous_product")
        if selected.variant_id is not None and selected.variant_id not in product.variant_ids:
            failures.append("unknown_variant")
        has_identity = False
        has_variant = selected.variant_id is None
        if not selected.evidence_ids:
            failures.append("missing_product_evidence")
        for evidence_id in selected.evidence_ids:
            item = evidence.get(evidence_id)
            if item is None:
                failures.append("unknown_evidence")
                continue
            if item.business_id != context.business_id:
                failures.append("cross_tenant_evidence")
            if not item.current:
                failures.append("stale_evidence")
            if item.product_id != selected.product_id:
                failures.append("unknown_evidence")
            if item.kind == "identity" and item.variant_id is None:
                has_identity = True
            if item.kind == "variant" and item.variant_id == selected.variant_id:
                has_variant = True
        if not has_identity or not has_variant:
            failures.append("missing_product_evidence")

    known_needs = set(context.known_need_ids)
    for claim in plan.claims:
        if not claim.evidence_ids:
            failures.append("missing_claim_evidence")
        if any(need_id not in known_needs for need_id in claim.need_ids):
            failures.append("unknown_need")
        for evidence_id in claim.evidence_ids:
            item = evidence.get(evidence_id)
            if item is None:
                failures.append("unknown_evidence")
            elif item.business_id != context.business_id:
                failures.append("cross_tenant_evidence")
            elif not item.current:
                failures.append("stale_evidence")
            elif item.product_id not in selected_ids:
                failures.append("unknown_evidence")

    fact_groups = (
        ("price", plan.facts.prices),
        ("stock", plan.facts.stocks),
        ("url", plan.facts.urls),
    )
    for expected_kind, facts in fact_groups:
        for fact in facts:
            item = evidence.get(fact.evidence_id)
            if fact.product_id not in selected_ids:
                failures.append("unknown_product")
            if item is None:
                failures.append("unknown_evidence")
                continue
            if item.business_id != context.business_id:
                failures.append("cross_tenant_evidence")
            if not item.current:
                failures.append("stale_evidence")
            if (
                item.kind != expected_kind
                or item.product_id != fact.product_id
                or item.variant_id != fact.variant_id
            ):
                failures.append("fact_evidence_kind_mismatch")
            if not _same_value(item.value, fact.value):
                failures.append("fact_value_mismatch")

    selected = set(selected_ids)
    for outcome in context.hard_constraints:
        if outcome.product_id not in selected:
            continue
        if outcome.verdict == "MISMATCH" or (
            outcome.verdict == "UNKNOWN" and outcome.unknown_is_violation
        ):
            failures.append("hard_constraint_mismatch")

    successful = set(context.successful_action_ids)
    if any(
        action.action_id not in successful or not action.action_id.startswith(f"{action.action}:")
        for action in plan.confirmed_actions
    ):
        failures.append("action_not_successful")

    return AnswerPlanValidation(
        ok=not failures,
        failures=tuple(dict.fromkeys(failures)),
    )


def evidence_coverage(plan: AnswerPlan) -> float:
    if not plan.claims:
        return 1.0
    covered = sum(bool(claim.evidence_ids) for claim in plan.claims)
    return covered / len(plan.claims)


def failure_counts(validation: AnswerPlanValidation) -> dict[str, int]:
    """PII-safe telemetry projection with closed codes only."""

    return dict(Counter(validation.failures))
