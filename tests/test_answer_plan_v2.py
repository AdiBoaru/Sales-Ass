"""NX-239 — contractul `AnswerPlanV2`: schema/caps, PII, proiecția `to_v1`, validatorul V2.

Validatorul V2 REFOLOSEȘTE `validate_answer_plan` (nu există un al doilea validator de evidence):
aici testăm regulile NOI (obligation coverage, hard relaxation, revoked needs, action intents,
comparație sourced) plus faptul că regulile V1 (evidence/tenant/fapte) se aplică prin proiecție."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.agent.answer_plan import (
    AnswerPlanContext,
    AnswerPlanV2,
    ComparisonCell,
    EvidenceRecord,
    GroundedProduct,
    PlanClaim,
    PlanComparison,
    PlanFacts,
    PlanNoResults,
    PlanObligation,
    PlanRecommendation,
    SelectedProduct,
    StyleSignals,
    validate_answer_plan_v2,
)
from src.agent.answer_plan_runtime import ANSWER_PLAN_V2_SCHEMA


def _facts() -> PlanFacts:
    return PlanFacts(prices=(), stocks=(), urls=())


def _plan(**overrides) -> AnswerPlanV2:
    base = dict(
        schema_version=2,
        business_id="b1",
        locale="ro",
        intent_summary="recomandare ser",
        obligations=(PlanObligation(kind="recommend", key="recommend_0"),),
        direct_answer="Pentru ten uscat, serul LumaDerm e alegerea potrivită din catalog.",
        selected_products=(
            SelectedProduct(
                product_id="p1", variant_id=None, evidence_ids=("product:p1:identity",)
            ),
        ),
        claims=(),
        facts=_facts(),
        recommendations=(
            PlanRecommendation(
                product_id="p1",
                variant_id=None,
                reason="are acid hialuronic, exact pentru ten uscat",
                evidence_ids=("product:p1:identity",),
                need_ids=("concerns",),
            ),
        ),
        comparison=None,
        constraints_applied=("budget_max",),
        unknowns=(),
        relaxations=(),
        clarification=None,
        no_results=None,
        state_update_proposals=(),
        action_intents=(),
        disclosures=(),
        handoff=False,
        confirmed_actions=(),
        style_signals=StyleSignals(tone="neutral", verbosity="short"),
    )
    base.update(overrides)
    return AnswerPlanV2(**base)


def _context(**overrides) -> AnswerPlanContext:
    base = dict(
        business_id="b1",
        locale="ro",
        products=(
            GroundedProduct(product_id="p1", business_id="b1", resolution="exact", variant_ids=()),
        ),
        evidence=(
            EvidenceRecord(
                evidence_id="product:p1:identity",
                business_id="b1",
                product_id="p1",
                variant_id=None,
                kind="identity",
                value="p1",
                source_version="live",
                current=True,
            ),
        ),
        hard_constraints=(),
        successful_action_ids=(),
        known_need_ids=("concerns", "budget_max"),
    )
    base.update(overrides)
    return AnswerPlanContext(**base)


# --- Schema & caps -------------------------------------------------------------


def test_schema_is_strict_and_all_required():
    schema = ANSWER_PLAN_V2_SCHEMA["schema"]
    assert ANSWER_PLAN_V2_SCHEMA["strict"] is True
    assert set(schema["required"]) == set(schema["properties"])


def test_extra_fields_forbidden():
    with pytest.raises(ValidationError):
        AnswerPlanV2(**{**_plan().model_dump(), "chain_of_thought": "gând ascuns"})


def test_caps_enforced_on_obligations():
    with pytest.raises(ValidationError):
        _plan(obligations=tuple(PlanObligation(kind="answer", key=f"q{i}") for i in range(9)))


def test_direct_answer_length_cap():
    with pytest.raises(ValidationError):
        _plan(direct_answer="x" * 901)


def test_pii_rejected_in_plan():
    with pytest.raises(ValidationError):
        _plan(direct_answer="Sună-mă la 0722123456 să discutăm")


def test_no_results_class_is_closed_vocabulary():
    with pytest.raises(ValidationError):
        PlanNoResults(reason_class="maybe_later", criteria=(), alternatives=())


def test_clarification_is_structurally_single():
    """Max o clarificare per plan: câmp UNIC, nu listă — nu există unde să pui a doua."""
    assert AnswerPlanV2.model_fields["clarification"].annotation is not None
    schema = ANSWER_PLAN_V2_SCHEMA["schema"]["properties"]["clarification"]
    assert "anyOf" in schema or schema.get("type") != "array"


# --- Proiecția V1 (reuse validator) --------------------------------------------


def test_to_v1_maps_recommendations_to_claims():
    v1 = _plan().to_v1()
    assert v1.schema_version == 1
    assert any(c.claim_type == "recommendation" for c in v1.claims)
    assert v1.claims[-1].evidence_ids == ("product:p1:identity",)


def test_v1_rules_apply_through_projection():
    """Evidence inexistent pe o recomandare → validatorul V1 (reuse) îl prinde."""
    plan = _plan(
        recommendations=(
            PlanRecommendation(
                product_id="p1",
                variant_id=None,
                reason="motiv legat de nevoie",
                evidence_ids=("product:p1:nonexistent",),
                need_ids=("concerns",),
            ),
        )
    )
    validation = validate_answer_plan_v2(plan, _context())
    assert "unknown_evidence" in validation.failures


def test_unknown_need_flagged_through_projection():
    plan = _plan(
        recommendations=(
            PlanRecommendation(
                product_id="p1",
                variant_id=None,
                reason="motiv",
                evidence_ids=("product:p1:identity",),
                need_ids=("nevoie_inventata",),
            ),
        )
    )
    validation = validate_answer_plan_v2(plan, _context())
    assert "unknown_need" in validation.failures


# --- Regulile NOI V2 -----------------------------------------------------------


def test_valid_plan_passes():
    validation = validate_answer_plan_v2(
        _plan(), _context(), required_obligations=(("recommend", "recommend_0"),)
    )
    assert validation.ok, validation.failures


def test_hard_relaxation_forbidden():
    plan = _plan(relaxations=("budget_max",))
    validation = validate_answer_plan_v2(plan, _context(), hard_constraint_keys=("budget_max",))
    assert "hard_relaxation" in validation.failures


def test_soft_relaxation_allowed():
    plan = _plan(relaxations=("concerns",))
    validation = validate_answer_plan_v2(plan, _context(), hard_constraint_keys=("budget_max",))
    assert "hard_relaxation" not in validation.failures


def test_revoked_need_cannot_be_revived():
    plan = _plan()
    validation = validate_answer_plan_v2(plan, _context(), revoked_need_keys=("concerns",))
    assert "revoked_need_used" in validation.failures


def test_unknown_action_intent_rejected():
    plan = _plan(action_intents=("launch_rocket",))
    validation = validate_answer_plan_v2(
        plan, _context(), allowed_action_intents=("cart_add_action",)
    )
    assert "unknown_action_intent" in validation.failures


def test_empty_plan_without_answer_or_honesty_fails():
    plan = _plan(direct_answer="", recommendations=(), selected_products=(), no_results=None)
    validation = validate_answer_plan_v2(plan, _context())
    assert "missing_direct_answer" in validation.failures


def test_no_results_covers_recommend_obligation():
    plan = _plan(
        recommendations=(),
        selected_products=(),
        no_results=PlanNoResults(
            reason_class="no_match", criteria=("budget_max",), alternatives=()
        ),
    )
    validation = validate_answer_plan_v2(
        plan, _context(), required_obligations=(("recommend", "recommend_0"),)
    )
    assert validation.ok, validation.failures


def test_comparison_requires_grounded_refs_and_evidence():
    plan = _plan(
        comparison=PlanComparison(
            product_ids=("p1", "fantoma"),
            axes=("preț",),
            cells=(
                ComparisonCell(product_id="p1", axis="preț", value=100.0, evidence_id="inexistent"),
            ),
        )
    )
    validation = validate_answer_plan_v2(plan, _context())
    assert "unknown_product" in validation.failures
    assert "unknown_evidence" in validation.failures


def test_hard_constraint_mismatch_still_vetoes():
    """Regula V1 de hard constraints (context server) rămâne activă prin proiecție."""
    from src.agent.answer_plan import HardConstraintOutcome

    plan = _plan()
    context = _context(
        hard_constraints=(
            HardConstraintOutcome(
                product_id="p1", facet="price", verdict="MISMATCH", unknown_is_violation=False
            ),
        )
    )
    validation = validate_answer_plan_v2(plan, context)
    assert "hard_constraint_mismatch" in validation.failures


def test_claims_and_recommendations_share_claim_rules():
    plan = _plan(
        claims=(
            PlanClaim(
                claim_type="fact",
                text="are 30 ml",
                evidence_ids=(),
                need_ids=(),
            ),
        )
    )
    validation = validate_answer_plan_v2(plan, _context())
    assert "missing_claim_evidence" in validation.failures
