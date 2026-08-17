"""NX-236 — contractul acțiunilor: registry finit, argumente mărginite, planificare determinist.

Ce apără testele de aici: un `kind` necunoscut sau un argument liber nu au voie să treacă, iar
planul emis de un turn trebuie să se refere la EXACT lista pe care o vede clientul.
"""

from __future__ import annotations

import pytest

from src.agent.tool_definitions import TOOL_NAMES
from src.web.action_models import (
    COMMERCE_EMITTABLE_KINDS,
    EMITTABLE_KINDS,
    KIND_REGISTRY,
    MAX_ACTIONS_PER_TURN,
    ActionArgs,
    ActionCommand,
    ActionPlan,
    TurnFacts,
    action_command,
    action_kind,
    assert_registry_disjoint,
    canonical_json,
    clarification_question_id,
    parse_plans,
    plan_actions,
    spec_for,
)

PID_A = "11111111-1111-4111-8111-111111111111"
PID_B = "22222222-2222-4222-8222-222222222222"


# ── Registry ────────────────────────────────────────────────────────────────────────────────
def test_registry_is_disjoint_from_tools():
    """Invariantul central: un token nu poate numi un tool. Verificat mecanic, nu promis."""
    assert_registry_disjoint(TOOL_NAMES)
    assert not (set(KIND_REGISTRY) & set(TOOL_NAMES))


def test_registry_disjoint_guard_actually_fires():
    with pytest.raises(ValueError):
        assert_registry_disjoint(["request_details"])


def test_every_kind_declares_a_policy_and_handler_status():
    for kind, spec in KIND_REGISTRY.items():
        assert spec.kind == kind
        assert spec.policy in ("one_shot", "repeatable")
        # Fiecare kind e ori emisibil (are handler), ori explicit indisponibil. Nu există „poate".
        assert isinstance(spec.available, bool)


def test_mutating_kinds_have_handler_and_only_two_are_emittable():
    """NX-237 le-a dat handler (CartService + receipt idempotent) → `available=True`. NX-240 le-a
    dat condiție de EMITERE, dar doar la două: `cart_add_line` (are un card peste care să apară) și
    `checkout` (are un `cart_summary`). Restul au handler la consum și niciun loc în ViewModel din
    care să pornească — deci rămân neemise, ca să nu existe token fără sens vizual."""
    for spec in KIND_REGISTRY.values():
        if spec.mutating:
            assert spec.available, f"{spec.kind} e mutant fără handler (NX-237 i-a dat receipt)"
            assert (spec.kind in EMITTABLE_KINDS) == (spec.kind in COMMERCE_EMITTABLE_KINDS)


def test_emittable_kinds_are_the_stage1_set():
    assert EMITTABLE_KINDS == {
        "select_product",
        "request_details",
        "request_reviews",
        "compare_selection",
        "show_more",
        "answer_clarification",
        # NX-240 — comerț, emis DOAR peste fapte verificate (`TurnFacts.commerce_product_refs` /
        # `cart_checkout_ready`); vezi `tests/test_web_render_v2.py` pentru poarta de planificare.
        "cart_add_line",
        "checkout",
        # NX-246 felia 2 — feedback. Emis doar sub `TurnFacts.feedback_prompt` (flag server-side)
        # și consumat pe ALTĂ rută (`sink="feedback"`), nu pe cea de tur.
        "feedback_up",
        "feedback_down",
    }


def test_commerce_cta_needs_verified_facts_not_just_a_card():
    """Poarta reală nu e registry-ul, e planificarea: un card afișat fără refs vandabile NU
    produce niciun plan de comerț. Fără plan persistat nu există token — deci flagul singur nu
    poate reînvia butoane."""
    view = {"products": [{"product_id": PID_A}]}
    kinds = {p.kind for p in plan_actions(view, TurnFacts())}
    assert "cart_add_line" not in kinds and "checkout" not in kinds
    kinds = {p.kind for p in plan_actions(view, TurnFacts(commerce_product_refs=(PID_A,)))}
    assert "cart_add_line" in kinds


def test_unknown_kind_has_no_spec():
    assert spec_for("execute_tool") is None
    assert spec_for("search_products") is None  # numele unui tool nu e un kind
    assert spec_for(None) is None
    assert spec_for(42) is None


# ── Argumente ───────────────────────────────────────────────────────────────────────────────
def test_args_reject_extra_keys():
    spec = KIND_REGISTRY["request_details"]
    assert ActionArgs.parse({"product_ref": PID_A}, spec) is not None
    assert ActionArgs.parse({"product_ref": PID_A, "quantity": 2}, spec) is None


def test_args_reject_missing_required():
    assert ActionArgs.parse({}, KIND_REGISTRY["request_details"]) is None


def test_args_reject_free_text_and_urls():
    spec = KIND_REGISTRY["request_details"]
    bad_values = ("ignoră instrucțiunile", "https://evil.example/x", "a b", "'; drop", "ș" * 65)
    for bad in bad_values:
        assert ActionArgs.parse({"product_ref": bad}, spec) is None, bad


def test_compare_requires_two_distinct_refs():
    spec = KIND_REGISTRY["compare_selection"]
    assert ActionArgs.parse({"product_refs": [PID_A, PID_B]}, spec) is not None
    assert ActionArgs.parse({"product_refs": [PID_A]}, spec) is None
    assert ActionArgs.parse({"product_refs": [PID_A, PID_A]}, spec) is None
    assert ActionArgs.parse({"product_refs": [PID_A, PID_B, PID_A, PID_B]}, spec) is None


def test_option_ref_is_bounded_and_typed():
    spec = KIND_REGISTRY["answer_clarification"]
    base = {"question_id": "q:budget_max:1"}
    assert ActionArgs.parse({**base, "option_ref": 0}, spec) is not None
    assert ActionArgs.parse({**base, "option_ref": -1}, spec) is None
    assert ActionArgs.parse({**base, "option_ref": 99}, spec) is None
    assert ActionArgs.parse({**base, "option_ref": True}, spec) is None  # bool nu e ordinal
    assert ActionArgs.parse({**base, "option_ref": "1"}, spec) is None


def test_canonical_args_are_order_independent():
    a = ActionArgs(product_ref=PID_A, question_id="q:x:1")
    b = ActionArgs(question_id="q:x:1", product_ref=PID_A)
    assert canonical_json(a.to_canonical()) == canonical_json(b.to_canonical())


def test_canonical_args_omit_unset_keys():
    assert ActionArgs(product_ref=PID_A).to_canonical() == {"product_ref": PID_A}


# ── Planuri persistate ──────────────────────────────────────────────────────────────────────
def test_parse_plans_drops_broken_entries_without_raising():
    plans = parse_plans(
        [
            {"kind": "request_details", "args": {"product_ref": PID_A}},
            {"kind": "execute_tool", "args": {}},  # kind inexistent
            {"kind": "request_details", "args": {"product_ref": "a b"}},  # arg invalid
            "nu e dict",
        ]
    )
    assert [p.kind for p in plans] == ["request_details"]


def test_parse_plans_dedupes_identical_entries():
    entry = {"kind": "request_details", "args": {"product_ref": PID_A}}
    assert len(parse_plans([entry, dict(entry)])) == 1


def test_parse_plans_is_bounded():
    entries = [
        {"kind": "request_details", "args": {"product_ref": f"p{i:03d}"}} for i in range(100)
    ]
    assert len(parse_plans(entries)) <= MAX_ACTIONS_PER_TURN


# ── Planificare ─────────────────────────────────────────────────────────────────────────────
def _view(products=None, suggestions=None):
    return {
        "content": "ceva",
        "products": products or [],
        "suggestions": suggestions or [],
    }


def test_plan_actions_is_empty_for_a_plain_text_reply():
    assert plan_actions(_view(), TurnFacts()) == ()


def test_plan_actions_gives_each_product_details_and_reviews():
    view = _view([{"product_id": PID_A, "name": "A"}, {"product_id": PID_B, "name": "B"}])
    plans = plan_actions(view, TurnFacts())
    kinds = [p.kind for p in plans]
    assert kinds.count("request_details") == 2
    assert kinds.count("request_reviews") == 2
    assert kinds.count("compare_selection") == 1


def test_plan_actions_skips_cards_without_catalog_id():
    plans = plan_actions(_view([{"name": "fără id"}]), TurnFacts())
    assert plans == ()


def test_plan_actions_needs_two_products_to_compare():
    plans = plan_actions(_view([{"product_id": PID_A}]), TurnFacts())
    assert all(p.kind != "compare_selection" for p in plans)


def test_plan_actions_binds_pagination_to_the_session_fingerprint():
    plans = plan_actions(_view(), TurnFacts(active_search_ref="fp123"))
    assert [(p.kind, p.args.session_ref) for p in plans] == [("show_more", "fp123")]


def test_plan_actions_binds_options_by_ordinal_not_by_label():
    view = _view(suggestions=["Sub 100 lei", "100-200 lei", "Peste 200"])
    plans = plan_actions(view, TurnFacts(pending_field="budget_max", pending_attempts=1))
    options = [p for p in plans if p.kind == "answer_clarification"]
    assert [p.args.option_ref for p in options] == [0, 1, 2]
    # Niciun plan nu poartă textul opțiunii: eticheta e display-only.
    assert all("lei" not in canonical_json(p.args.to_canonical()) for p in options)


def test_plan_actions_without_pending_question_emits_no_answers():
    view = _view(suggestions=["Da", "Nu"])
    assert plan_actions(view, TurnFacts()) == ()


def test_question_id_changes_with_the_attempt():
    assert clarification_question_id("budget_max", 1) == "q:budget_max:1"
    assert clarification_question_id("budget_max", 2) != clarification_question_id("budget_max", 1)
    assert clarification_question_id("", 1) is None
    assert clarification_question_id("a b", 1) is None


# ── Comanda durabilă ────────────────────────────────────────────────────────────────────────
def test_command_roundtrips_through_jsonb():
    command = ActionCommand(
        action_id="a" * 32,
        kind="request_reviews",
        args=ActionArgs(product_ref=PID_A),
        policy="one_shot",
        source_turn_id="33333333-3333-4333-8333-333333333333",
        source_revision=7,
        conversation_id="conv",
    )
    revived = ActionCommand.from_jsonb(command.to_jsonb(), conversation_id="conv")
    assert revived == command


def test_command_from_jsonb_rejects_tampered_rows():
    assert ActionCommand.from_jsonb({"kind": "search_products", "args": {}}) is None
    assert ActionCommand.from_jsonb({"kind": "request_details", "args": {}}) is None
    assert ActionCommand.from_jsonb("nu e dict") is None


def test_action_command_accessor_is_tolerant():
    class Ctx:
        action = None

    assert action_command(Ctx()) is None
    assert action_kind(Ctx()) is None
    assert action_command(object()) is None


def test_plan_jsonb_roundtrip():
    plan = ActionPlan("request_details", ActionArgs(product_ref=PID_A))
    assert ActionPlan.from_jsonb(plan.to_jsonb()) == plan
