"""NX-239 — extractorul determinist de obligații (`brain_models.extract_obligations`).

Semnalele sunt COD, nu model: semne de întrebare, clauze mixte, salut, acțiune opacă, clarificare
în așteptare, context de siguranță. Fixture-urile de characterization stau în
`tests/fixtures/single_brain/turn_types.json` (formulări proprii, zero texte iZi/eMAG)."""

from __future__ import annotations

import json
from pathlib import Path

from src.agent.answer_plan import (
    AnswerPlanContext,
    AnswerPlanV2,
    PlanClarification,
    PlanFacts,
    PlanNoResults,
    PlanObligation,
    StyleSignals,
    validate_answer_plan_v2,
)
from src.agent.brain_models import extract_obligations, split_clauses

_FIXTURES = json.loads(
    (Path(__file__).parent / "fixtures" / "single_brain" / "turn_types.json").read_text(
        encoding="utf-8"
    )
)


def _extract(case: dict) -> tuple:
    return extract_obligations(
        case.get("message", ""),
        has_action=case.get("has_action", False),
        safety_active=case.get("safety_active", False),
    )


def test_fixture_expected_obligations_exact():
    for case in _FIXTURES["cases"]:
        if "expected_obligations" not in case:
            continue
        got = [[o.kind, o.key] for o in _extract(case)]
        assert got == case["expected_obligations"], case["name"]


def test_fixture_expected_kinds():
    for case in _FIXTURES["cases"]:
        if "expected_kinds" not in case:
            continue
        got = [o.kind for o in _extract(case) if o.kind != "safety"]
        assert got == case["expected_kinds"], (case["name"], got)


def test_fixture_expected_kinds_include():
    for case in _FIXTURES["cases"]:
        for kind in case.get("expected_kinds_include", []):
            assert kind in {o.kind for o in _extract(case)}, case["name"]


def test_empty_message_without_action_has_no_obligations():
    assert extract_obligations("") == ()


def test_opaque_action_is_single_obligation():
    obs = extract_obligations("", has_action=True)
    assert [(o.kind, o.key) for o in obs] == [("action", "opaque_action")]


def test_pending_clarification_becomes_obligation():
    obs = extract_obligations("sub 70 de lei", pending_clarification=True)
    assert ("answer", "pending_clarification") in {(o.kind, o.key) for o in obs}


def test_mixed_clause_sources_marked():
    obs = extract_obligations("cât costă livrarea? și vreau un ser cu vitamina C")
    sources = {o.source for o in obs if o.kind in ("answer", "recommend")}
    assert sources == {"mixed_clause"}


def test_split_clauses_keeps_question_ownership():
    clauses = split_clauses("Salut! Aveți livrare? Vreau o cremă.")
    assert clauses[0] == "salut"
    assert len(clauses) == 3


def test_safety_context_always_appends_safety():
    obs = extract_obligations("vreau o cremă antirid", safety_active=True)
    assert any(o.kind == "safety" for o in obs)


# --- Acoperirea obligațiilor în planul V2 (validator, nu prompt) ---------------


def _min_plan(**overrides) -> AnswerPlanV2:
    base = dict(
        schema_version=2,
        business_id="b1",
        locale="ro",
        intent_summary="întrebare livrare + recomandare",
        obligations=(
            PlanObligation(kind="answer", key="question_0"),
            PlanObligation(kind="recommend", key="recommend_1"),
        ),
        direct_answer="Livrarea costă conform politicii de livrare a magazinului.",
        selected_products=(),
        claims=(),
        facts=PlanFacts(prices=(), stocks=(), urls=()),
        recommendations=(),
        comparison=None,
        constraints_applied=(),
        unknowns=(),
        relaxations=(),
        clarification=None,
        no_results=PlanNoResults(reason_class="no_match", criteria=("budget",), alternatives=()),
        state_update_proposals=(),
        action_intents=(),
        disclosures=(),
        confirmed_actions=(),
        style_signals=StyleSignals(tone="neutral", verbosity="short"),
    )
    base.update(overrides)
    return AnswerPlanV2(**base)


def _context() -> AnswerPlanContext:
    return AnswerPlanContext(
        business_id="b1",
        locale="ro",
        products=(),
        evidence=(),
        hard_constraints=(),
        successful_action_ids=(),
        known_need_ids=("budget_max",),
    )


def test_mixed_obligations_covered_passes():
    plan = _min_plan()
    validation = validate_answer_plan_v2(
        plan,
        _context(),
        required_obligations=(("answer", "question_0"), ("recommend", "recommend_1")),
    )
    assert validation.ok, validation.failures


def test_uncovered_obligation_fails():
    # obligație compare cerută determinist, planul nu are nici comparison, nici no_results→compare
    plan = _min_plan(no_results=None)
    validation = validate_answer_plan_v2(
        plan, _context(), required_obligations=(("compare", "compare"),)
    )
    assert "obligation_uncovered" in validation.failures


def test_first_reply_wins_is_impossible_structurally():
    """FAQ + recomandare: planul care răspunde DOAR la FAQ (fără recomandări/no_results/clarify)
    pică validarea — obligația de recomandare rămâne neacoperită."""
    plan = _min_plan(
        obligations=(PlanObligation(kind="answer", key="question_0"),),
        no_results=None,
    )
    validation = validate_answer_plan_v2(
        plan,
        _context(),
        required_obligations=(("answer", "question_0"), ("recommend", "recommend_1")),
    )
    assert "obligation_uncovered" in validation.failures


def test_clarification_covers_recommend_obligation():
    plan = _min_plan(
        no_results=None,
        clarification=PlanClarification(
            question="Pentru ce tip de ten cauți crema?",
            target_need="concerns",
            reason="îngustează setul",
            options=(),
        ),
    )
    validation = validate_answer_plan_v2(
        plan, _context(), required_obligations=(("recommend", "recommend_1"),)
    )
    assert validation.ok, validation.failures
