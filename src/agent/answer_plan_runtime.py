"""NX-211 runtime orchestration around the production AnswerPlan contract."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from src.agent.answer_plan import (
    AnswerPlan,
    AnswerPlanContext,
    AnswerPlanValidation,
    EvidenceRecord,
    GroundedProduct,
    HardConstraintOutcome,
    evidence_coverage,
    failure_counts,
    validate_answer_plan,
)
from src.agent.validator import ValidationResult, validate_prose
from src.safety.external_data import contains_pii
from src.worker.text_scrub import has_medical_claim

log = logging.getLogger(__name__)

CriticTrigger = Literal[
    "comparison",
    "coverage_below_threshold",
    "initial_validator_failure",
    "medical_or_legal",
    "recommendation",
    "tier_max_quality",
]
CriticFailure = Literal[
    "claim_not_supported",
    "important_limitation_missing",
    "need_not_addressed",
    "overclaim",
    "tone_problem",
]

_LEGAL_RE = re.compile(r"\b(?:legal|juridic|garantat|conform legii|avocat)\b", re.IGNORECASE)

_ACTION_CONFIRMATION_RE = {
    "cart_add": re.compile(
        r"\b(?:am|a fost)\s+ad.ugat.{0,30}\bcos\b|\badded to (?:your )?cart\b",
        re.IGNORECASE,
    ),
    "checkout_link": re.compile(
        r"\b(?:am creat|ti-am pregatit).{0,30}\b(?:link|plata)\b|\bcheckout link is ready\b",
        re.IGNORECASE,
    ),
    "subscribe_back_in_stock": re.compile(
        r"\bte (?:anunt|notific).{0,30}\bcand revine\b|\bsubscribed.{0,20}back in stock\b",
        re.IGNORECASE,
    ),
}
ANSWER_PLAN_SCHEMA: dict[str, Any] = {
    "name": "answer_plan_v1",
    "strict": True,
    "schema": AnswerPlan.model_json_schema(by_alias=True),
}

CRITIC_SCHEMA: dict[str, Any] = {
    "name": "answer_plan_critic_v1",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["passed", "failures"],
        "properties": {
            "passed": {"type": "boolean"},
            "failures": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": [
                        "claim_not_supported",
                        "important_limitation_missing",
                        "need_not_addressed",
                        "overclaim",
                        "tone_problem",
                    ],
                },
            },
        },
    },
}


class CriticModelVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    passed: bool
    failures: tuple[CriticFailure, ...]


class CriticResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["skipped", "passed", "rejected", "unavailable"]
    triggers: tuple[CriticTrigger, ...]
    failures: tuple[CriticFailure, ...]


@dataclass(frozen=True, slots=True)
class PreparedAnswerPlan:
    plan: AnswerPlan | None
    context: AnswerPlanContext
    validation: AnswerPlanValidation
    revisions: int


def _product_id(product: dict[str, Any]) -> str:
    return str(product.get("id") or product.get("product_id") or "")


def _variants(product: dict[str, Any]) -> tuple[str, ...]:
    return tuple(
        str(variant.get("id") or variant.get("variant_id"))
        for variant in (product.get("variants") or [])
        if variant.get("id") or variant.get("variant_id")
    )


def build_answer_plan_context(
    *,
    business_id: str,
    locale: str,
    products: list[dict[str, Any]],
    successful_action_ids: set[str] | None = None,
    known_need_ids: tuple[str, ...] = (),
) -> AnswerPlanContext:
    """Project live product rows into compact, current evidence owned by the server."""

    grounded_products: list[GroundedProduct] = []
    evidence: list[EvidenceRecord] = []
    constraints: list[HardConstraintOutcome] = []
    for product in products:
        product_id = _product_id(product)
        if not product_id:
            continue
        tenant = str(product.get("business_id") or business_id)
        resolution = "ambiguous" if product.get("identity_status") == "ambiguous" else "exact"
        grounded_products.append(
            GroundedProduct(
                product_id=product_id,
                business_id=tenant,
                resolution=resolution,
                variant_ids=_variants(product),
            )
        )
        version = str(product.get("source_version") or product.get("updated_at") or "live")

        def add(kind: str, value: Any, *, variant_id: str | None = None) -> None:
            if value is None:
                return
            suffix = f":{variant_id}" if variant_id else ""
            evidence.append(
                EvidenceRecord(
                    evidence_id=f"product:{product_id}:{kind}{suffix}",
                    business_id=tenant,
                    product_id=product_id,
                    variant_id=variant_id,
                    kind=kind,
                    value=value,
                    source_version=version,
                    current=not bool(product.get("evidence_stale")),
                )
            )

        add("identity", product_id)
        add("price", product.get("price"))
        add("stock", product.get("availability"))
        add("url", product.get("url") or product.get("product_url"))
        for variant in product.get("variants") or []:
            variant_id = str(variant.get("id") or variant.get("variant_id") or "")
            if not variant_id:
                continue
            add("variant", variant_id, variant_id=variant_id)
            add("price", variant.get("price"), variant_id=variant_id)
            add("stock", variant.get("availability", variant.get("stock")), variant_id=variant_id)
        for raw in product.get("answer_evidence") or []:
            try:
                evidence.append(EvidenceRecord.model_validate(raw))
            except ValidationError:
                continue
        for raw in product.get("hard_constraint_results") or []:
            try:
                payload = {key: value for key, value in raw.items() if key != "product_id"}
                constraints.append(HardConstraintOutcome(product_id=product_id, **payload))
            except (AttributeError, TypeError, ValidationError):
                continue
    return AnswerPlanContext(
        business_id=business_id,
        locale=locale,
        products=tuple(grounded_products),
        evidence=tuple(evidence),
        hard_constraints=tuple(constraints),
        successful_action_ids=tuple(sorted(successful_action_ids or set())),
        known_need_ids=known_need_ids,
    )


def _plan_prompt(query: str, context: AnswerPlanContext, failures: tuple[str, ...] = ()) -> str:
    safe_query = "[PII REDACTED]" if contains_pii(query) else query
    payload = {
        "business_id": context.business_id,
        "locale": context.locale,
        "products": [item.model_dump(mode="json") for item in context.products],
        "evidence": [item.model_dump(mode="json") for item in context.evidence],
        "hard_constraints": [item.model_dump(mode="json") for item in context.hard_constraints],
        "successful_action_ids": list(context.successful_action_ids),
        "known_need_ids": list(context.known_need_ids),
    }
    feedback = f"\nFIX ONLY THESE VALIDATION CODES: {','.join(failures)}" if failures else ""
    return (
        f"CURRENT USER NEED:\n{safe_query}\n\nSERVER EVIDENCE:\n"
        f"{json.dumps(payload, ensure_ascii=False, sort_keys=True)}{feedback}"
    )


_PLAN_SYSTEM = """Produce AnswerPlan schema_version 1 before any response text.
Use only server evidence IDs and exact product/variant IDs supplied by the server.
UNKNOWN is not MISMATCH. Never confirm an action unless its action_id is in successful_action_ids.
Every factual or recommendation claim needs evidence. Do not include personal data."""


async def prepare_answer_plan(
    llm,
    *,
    query: str,
    context: AnswerPlanContext,
) -> PreparedAnswerPlan:
    """Generate and validate a plan, allowing exactly one plan revision."""

    last_plan: AnswerPlan | None = None
    validation = AnswerPlanValidation(ok=False, failures=("unknown_product",))
    for revision in range(2):
        try:
            raw = await llm.complete_schema(
                _PLAN_SYSTEM,
                _plan_prompt(query, context, validation.failures if revision else ()),
                ANSWER_PLAN_SCHEMA,
            )
            if contains_pii(json.dumps(raw, ensure_ascii=False)):
                validation = AnswerPlanValidation(ok=False, failures=("pii_detected",))
                continue
            last_plan = AnswerPlan.model_validate(raw)
            validation = validate_answer_plan(last_plan, context)
        except (ValidationError, ValueError, TypeError, KeyError):
            last_plan = None
            validation = AnswerPlanValidation(ok=False, failures=("unknown_evidence",))
        except Exception:  # noqa: BLE001 - provider outage must become a visible fallback
            log.warning("answer plan generation unavailable", exc_info=True)
            last_plan = None
            validation = AnswerPlanValidation(ok=False, failures=("unknown_evidence",))
        if validation.ok:
            return PreparedAnswerPlan(last_plan, context, validation, revision)
    return PreparedAnswerPlan(None, context, validation, 1)


def critic_triggers(
    plan: AnswerPlan,
    draft: str,
    *,
    comparison: bool,
    initial_validation_failed: bool,
    coverage_threshold: float,
    max_quality: bool,
) -> tuple[CriticTrigger, ...]:
    triggers: list[CriticTrigger] = []
    if any(claim.claim_type == "recommendation" for claim in plan.claims):
        triggers.append("recommendation")
    if comparison:
        triggers.append("comparison")
    combined = " ".join([draft, *(claim.text for claim in plan.claims)])
    if has_medical_claim(combined) or _LEGAL_RE.search(combined):
        triggers.append("medical_or_legal")
    if evidence_coverage(plan) < coverage_threshold:
        triggers.append("coverage_below_threshold")
    if initial_validation_failed:
        triggers.append("initial_validator_failure")
    if max_quality:
        triggers.append("tier_max_quality")
    return tuple(dict.fromkeys(triggers))


async def run_semantic_critic(
    llm,
    *,
    plan: AnswerPlan,
    context: AnswerPlanContext,
    draft: str,
    enabled: bool,
    comparison: bool = False,
    initial_validation_failed: bool = False,
    coverage_threshold: float = 0.99,
    max_quality: bool = False,
) -> CriticResult:
    triggers = critic_triggers(
        plan,
        draft,
        comparison=comparison,
        initial_validation_failed=initial_validation_failed,
        coverage_threshold=coverage_threshold,
        max_quality=max_quality,
    )
    if not enabled or not triggers:
        return CriticResult(status="skipped", triggers=triggers, failures=())
    if contains_pii(draft):
        return CriticResult(
            status="rejected",
            triggers=triggers,
            failures=("overclaim",),
        )
    prompt = {
        "plan": plan.model_dump(mode="json", by_alias=True),
        "evidence": [item.model_dump(mode="json") for item in context.evidence],
        "draft": draft,
        "checks": [
            "evidence supports each claim",
            "recommendation addresses need_ids",
            "tone is appropriate",
            "no exaggeration",
            "important limitations are present",
        ],
    }
    try:
        raw = await llm.complete_schema(
            "You are a strict semantic critic. Return codes only, never rewrite the answer.",
            json.dumps(prompt, ensure_ascii=False, sort_keys=True),
            CRITIC_SCHEMA,
        )
        verdict = CriticModelVerdict.model_validate(raw)
    except Exception:  # noqa: BLE001 - critic outage degrades to deterministic validation
        log.warning("answer plan critic unavailable", exc_info=True)
        return CriticResult(status="unavailable", triggers=triggers, failures=())
    failures = tuple(dict.fromkeys(verdict.failures))
    status = "passed" if verdict.passed and not failures else "rejected"
    return CriticResult(status=status, triggers=triggers, failures=failures)


def safe_fallback(locale: str) -> str:
    messages = {
        "ro": (
            "Nu pot confirma recomandarea in siguranta acum. "
            "Pot restrange optiunile dupa buget sau nevoie."
        ),
        "hu": (
            "Most nem tudom biztonsagosan megerositeni az ajanlast. "
            "Szukithetem a lehetosegeket dupa nevoie sau buget."
        ),
        "en": (
            "I cannot safely confirm that recommendation right now. "
            "I can narrow the options by need or budget."
        ),
    }
    return messages.get(locale, messages["en"])


def validate_revised_draft(
    draft: str,
    *,
    products: list[dict[str, Any]],
    generated_links: set[str],
    grounded_prices: set[float],
    plan: AnswerPlan | None = None,
) -> ValidationResult:
    if contains_pii(draft):
        return ValidationResult(ok=False, reasons=["pii_detected"])
    confirmed = {action.action for action in plan.confirmed_actions} if plan else set()
    for action, pattern in _ACTION_CONFIRMATION_RE.items():
        if pattern.search(draft) and action not in confirmed:
            return ValidationResult(ok=False, reasons=["false_action_confirmation"])

    return validate_prose(
        draft,
        products=products,
        generated_links=generated_links,
        grounded_prices=grounded_prices,
    )


def plan_event(prepared: PreparedAnswerPlan) -> dict[str, Any]:
    """Persist counts and closed failure codes, never plan text or IDs."""

    return {
        "schema_version": 1,
        "ok": prepared.validation.ok,
        "revisions": prepared.revisions,
        "n_products": len(prepared.plan.selected_products) if prepared.plan else 0,
        "n_claims": len(prepared.plan.claims) if prepared.plan else 0,
        "failures": failure_counts(prepared.validation),
    }
