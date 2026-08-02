from copy import deepcopy

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from src.agent.answer_plan import (
    AnswerPlan,
    AnswerPlanContext,
    EvidenceRecord,
    GroundedProduct,
    HardConstraintOutcome,
    validate_answer_plan,
)
from src.agent.answer_plan_runtime import (
    build_answer_plan_context,
    critic_triggers,
    prepare_answer_plan,
    run_semantic_critic,
    validate_revised_draft,
)


def _context(
    *,
    tenant: str = "business-1",
    resolution: str = "exact",
    constraint: str = "MATCH",
    unknown_is_violation: bool = False,
) -> AnswerPlanContext:
    return AnswerPlanContext(
        business_id="business-1",
        locale="ro",
        products=(
            GroundedProduct(
                product_id="product-1",
                business_id=tenant,
                resolution=resolution,
                variant_ids=("variant-1",),
            ),
        ),
        evidence=(
            EvidenceRecord(
                evidence_id="identity-1",
                business_id=tenant,
                product_id="product-1",
                variant_id=None,
                kind="identity",
                value="product-1",
                source_version="v1",
                current=True,
            ),
            EvidenceRecord(
                evidence_id="price-1",
                business_id=tenant,
                product_id="product-1",
                variant_id=None,
                kind="price",
                value=49.9,
                source_version="v1",
                current=True,
            ),
            EvidenceRecord(
                evidence_id="stock-1",
                business_id=tenant,
                product_id="product-1",
                variant_id=None,
                kind="stock",
                value="in_stock",
                source_version="v1",
                current=True,
            ),
            EvidenceRecord(
                evidence_id="url-1",
                business_id=tenant,
                product_id="product-1",
                variant_id=None,
                kind="url",
                value="https://shop.test/product-1",
                source_version="v1",
                current=True,
            ),
            EvidenceRecord(
                evidence_id="claim-1",
                business_id=tenant,
                product_id="product-1",
                variant_id=None,
                kind="claim",
                value="potrivit pentru ten sensibil",
                source_version="v1",
                current=True,
            ),
        ),
        hard_constraints=(
            HardConstraintOutcome(
                product_id="product-1",
                facet="skin_type",
                verdict=constraint,
                unknown_is_violation=unknown_is_violation,
            ),
        ),
        successful_action_ids=("checkout-1",),
        known_need_ids=("need-1",),
    )


def _raw_plan() -> dict:
    return {
        "schema_version": 1,
        "business_id": "business-1",
        "locale": "ro",
        "selected_products": [
            {
                "product_id": "product-1",
                "variant_id": None,
                "evidence_ids": ["identity-1"],
            }
        ],
        "claims": [
            {
                "type": "recommendation",
                "text": "Este potrivit pentru nevoia ta.",
                "evidence_ids": ["claim-1"],
                "need_ids": ["need-1"],
            }
        ],
        "facts": {
            "prices": [
                {
                    "product_id": "product-1",
                    "variant_id": None,
                    "value": 49.9,
                    "evidence_id": "price-1",
                }
            ],
            "stocks": [
                {
                    "product_id": "product-1",
                    "variant_id": None,
                    "value": "in_stock",
                    "evidence_id": "stock-1",
                }
            ],
            "urls": [
                {
                    "product_id": "product-1",
                    "variant_id": None,
                    "value": "https://shop.test/product-1",
                    "evidence_id": "url-1",
                }
            ],
        },
        "uncertainties": [],
        "unmet_constraints": [],
        "confirmed_actions": [
            {"action": "checkout_link", "action_id": "checkout-1", "reference_id": "ref-1"}
        ],
    }


def _plan(raw: dict | None = None) -> AnswerPlan:
    return AnswerPlan.model_validate(raw or _raw_plan())


def test_valid_plan_passes_all_deterministic_gates():
    assert validate_answer_plan(_plan(), _context()).ok is True


@given(st.floats(min_value=0, max_value=100_000, allow_nan=False, allow_infinity=False))
def test_property_price_outside_evidence_never_passes(value):
    if abs(value - 49.9) <= 0.005:
        return
    raw = _raw_plan()
    raw["facts"]["prices"][0]["value"] = value

    report = validate_answer_plan(_plan(raw), _context())

    assert report.ok is False
    assert "fact_value_mismatch" in report.failures


@given(st.from_regex(r"https://invalid\.test/[a-z]{1,12}", fullmatch=True))
def test_property_url_outside_evidence_never_passes(url):
    raw = _raw_plan()
    raw["facts"]["urls"][0]["value"] = url

    report = validate_answer_plan(_plan(raw), _context())

    assert report.ok is False
    assert "fact_value_mismatch" in report.failures


def test_cross_tenant_product_and_evidence_are_rejected():
    report = validate_answer_plan(_plan(), _context(tenant="business-2"))

    assert report.ok is False
    assert "cross_tenant_product" in report.failures
    assert "cross_tenant_evidence" in report.failures


def test_ambiguous_identity_is_never_resolved_automatically():
    report = validate_answer_plan(_plan(), _context(resolution="ambiguous"))

    assert report.ok is False
    assert "ambiguous_product" in report.failures


def test_unknown_is_not_mismatch_unless_policy_says_it_is_unsafe():
    allowed = validate_answer_plan(_plan(), _context(constraint="UNKNOWN"))
    blocked = validate_answer_plan(
        _plan(),
        _context(constraint="UNKNOWN", unknown_is_violation=True),
    )

    assert "hard_constraint_mismatch" not in allowed.failures
    assert "hard_constraint_mismatch" in blocked.failures


def test_action_confirmation_requires_recorded_success():
    context = _context().model_copy(update={"successful_action_ids": ()})

    report = validate_answer_plan(_plan(), context)

    assert "action_not_successful" in report.failures


@pytest.mark.parametrize(
    "field,value",
    [
        ("claim", "Scrie-mi la test@example.com"),
        ("uncertainty", "telefon 0712345678"),
    ],
)
def test_answer_plan_rejects_pii(field, value):
    raw = _raw_plan()
    if field == "claim":
        raw["claims"][0]["text"] = value
    else:
        raw["uncertainties"] = [value]

    with pytest.raises(ValidationError):
        AnswerPlan.model_validate(raw)


class _PlanLLM:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = 0

    async def complete_schema(self, system, user, schema, *, model=None):
        output = self.outputs[self.calls]
        self.calls += 1
        if isinstance(output, Exception):
            raise output
        return deepcopy(output)


async def test_plan_generation_revises_at_most_once():
    bad = _raw_plan()
    bad["facts"]["prices"][0]["value"] = 999
    llm = _PlanLLM([bad, _raw_plan()])

    prepared = await prepare_answer_plan(llm, query="vreau un ser", context=_context())

    assert prepared.validation.ok is True
    assert prepared.revisions == 1
    assert llm.calls == 2


async def test_plan_generation_failure_returns_no_plan_for_p6_fallback():
    llm = _PlanLLM([RuntimeError("down"), RuntimeError("down")])

    prepared = await prepare_answer_plan(llm, query="vreau un ser", context=_context())

    assert prepared.plan is None
    assert prepared.validation.ok is False
    assert llm.calls == 2


def test_runtime_context_uses_server_tenant_and_live_facts():
    context = build_answer_plan_context(
        business_id="business-1",
        locale="ro",
        products=[
            {
                "id": "product-1",
                "price": 49.9,
                "availability": "in_stock",
                "url": "https://shop.test/product-1",
            }
        ],
    )

    assert context.business_id == "business-1"
    assert {item.kind for item in context.evidence} == {"identity", "price", "stock", "url"}


def test_critic_triggers_are_deterministic_code_conditions():
    triggers = critic_triggers(
        _plan(),
        "Compar doua produse.",
        comparison=True,
        initial_validation_failed=True,
        coverage_threshold=0.99,
        max_quality=True,
    )

    assert triggers == (
        "recommendation",
        "comparison",
        "initial_validator_failure",
        "tier_max_quality",
    )


async def test_critic_kill_switch_off_never_calls_model():
    llm = _PlanLLM([RuntimeError("must not run")])

    result = await run_semantic_critic(
        llm,
        plan=_plan(),
        context=_context(),
        draft="Recomand produsul.",
        enabled=False,
    )

    assert result.status == "skipped"
    assert llm.calls == 0


async def test_critic_unavailable_degrades_without_silence():
    llm = _PlanLLM([RuntimeError("critic down")])

    result = await run_semantic_critic(
        llm,
        plan=_plan(),
        context=_context(),
        draft="Recomand produsul.",
        enabled=True,
    )

    assert result.status == "unavailable"


def test_final_draft_rejects_pii_and_ungrounded_values(monkeypatch):
    monkeypatch.setattr(
        "src.agent.validator.get_settings",
        lambda: type(
            "S",
            (),
            {
                "validator_bare_numbers_enabled": True,
                "validator_claims_enabled": True,
                "validator_stock_claims_enabled": True,
                "safety_medical_guardrail_enabled": True,
            },
        )(),
    )
    pii = validate_revised_draft(
        "Scrie la test@example.com",
        products=[],
        generated_links=set(),
        grounded_prices=set(),
    )
    invented = validate_revised_draft(
        "Costa 999 lei: https://fake.test/pay",
        products=[],
        generated_links=set(),
        grounded_prices=set(),
    )

    assert pii.ok is False and pii.reasons == ["pii_detected"]
    assert invented.ok is False
    assert {"ungrounded_price", "invented_link"} <= set(invented.reasons)


def test_successful_action_id_cannot_be_relabelled_as_another_action():
    raw = _raw_plan()
    raw["confirmed_actions"][0]["action"] = "cart_add"

    report = validate_answer_plan(_plan(raw), _context())

    assert "action_not_successful" in report.failures


def test_draft_cannot_report_an_unconfirmed_action():
    result = validate_revised_draft(
        "Am adaugat produsul in cos.",
        products=[],
        generated_links=set(),
        grounded_prices=set(),
        plan=_plan(),
    )

    assert result.ok is False
    assert result.reasons == ["false_action_confirmation"]
