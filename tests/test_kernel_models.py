"""NX-328 felia 1a — modelele normative ale contractului `kernel.v1.0`, schema și traceul.

Invarianți: I1 pe schemă (modelul nu are niciun câmp în care să pună un id de produs), I18
(fiecare trace poartă versiunea contractului) și regula de schimbare a contractului (schema scrisă
de model nu se schimbă fără bump de versiune). Zero model, zero DB."""

from __future__ import annotations

import json
import re

import pytest
from pydantic import ValidationError

from scripts import kernel_schema_snapshot as snap
from src.conversation.interpretation import (
    KERNEL_CONTRACT_VERSION,
    UNIVERSAL_DIMENSIONS,
    AmbiguityDecision,
    AnswerPolicy,
    CheckedChange,
    ResolvedRef,
    StateChange,
    TurnInterpretation,
    TurnPlan,
    build_interpretation_schema,
)
from src.conversation.kernel_trace import LAYER_NAMES, KernelTrace, first_divergence, render
from src.tools.catalog_tools import SearchArgs

# Exemplul din designul §B, adus la contract: fără `goal`, fără `within` (runda 3, punctul 15).
DESIGN_EXAMPLE = {
    "thread": "continue",
    "acts": [{"kind": "compare", "targets": ["r1", "r2"], "query": None}],
    "changes": [],
    "references": [
        {
            "id": "r1",
            "text": "al doilea",
            "kind": "ordinal",
            "ordinal": 2,
            "name": None,
            "dimension": None,
            "value": None,
            "direction": None,
        },
        {
            "id": "r2",
            "text": "primul",
            "kind": "ordinal",
            "ordinal": 1,
            "name": None,
            "dimension": None,
            "value": None,
            "direction": None,
        },
    ],
    "ambiguities": [],
    "corrects_previous_turn": True,
}


def _objects(node):
    if isinstance(node, dict):
        if node.get("type") == "object" and "properties" in node:
            yield node
        for value in node.values():
            yield from _objects(value)
    elif isinstance(node, list):
        for item in node:
            yield from _objects(item)


def _property_names(schema) -> set[str]:
    return {name for obj in _objects(schema) for name in obj["properties"]}


# --- modelele scrise de model -------------------------------------------------------------------


def test_the_design_example_validates():
    parsed = TurnInterpretation.model_validate(DESIGN_EXAMPLE)
    assert parsed.acts[-1].kind == "compare"
    assert [r.id for r in parsed.references] == ["r1", "r2"]


def test_removed_fields_are_rejected():
    # Runda 1: `goal` și `thread=switch` au plecat; runda 3: `within`. Un model care le trimite
    # scrie contra unei versiuni vechi a contractului.
    with pytest.raises(ValidationError):
        TurnInterpretation.model_validate({**DESIGN_EXAMPLE, "goal": "compare"})
    with pytest.raises(ValidationError):
        TurnInterpretation.model_validate({**DESIGN_EXAMPLE, "thread": "switch"})
    ref = {**DESIGN_EXAMPLE["references"][0], "within": "shown_now"}
    with pytest.raises(ValidationError):
        TurnInterpretation.model_validate({**DESIGN_EXAMPLE, "references": [ref]})


def test_every_model_field_is_required_so_the_model_cannot_omit_one():
    with pytest.raises(ValidationError):
        StateChange.model_validate({"op": "add", "quote": "ceva"})


# --- schema ---------------------------------------------------------------------------------------


def test_schema_is_strict_compatible_on_every_object():
    schema = build_interpretation_schema(["skin_type"])
    objects = list(_objects(schema))
    assert objects
    for obj in objects:
        assert obj["additionalProperties"] is False
        assert sorted(obj["required"]) == sorted(obj["properties"])


def test_i1_the_model_has_no_field_for_a_product_or_variant_id():
    names = _property_names(build_interpretation_schema(["skin_type"]))
    id_like = {n for n in names if re.search(r"(^|_)ids?$|product|variant|sku|url", n)}
    # `id` e handle-ul local al unei referințe („r1"), nu un id de catalog.
    assert id_like == {"id"}
    # Câmpurile care numesc alte obiecte o fac prin handle-uri locale, nu prin id-uri.
    assert {"targets", "relative_to", "target"} <= names


def test_i1_code_owned_fields_never_reach_the_model_schema():
    names = _property_names(build_interpretation_schema(["skin_type"]))
    assert not names & {"provenance", "strength", "canonical_value", "outcome", "source"}


def test_the_dimension_enum_is_the_pack_plus_universals_sorted():
    schema = build_interpretation_schema(["skin_type", "concerns"])
    enum = schema["$defs"]["StateChange"]["properties"]["dimension"]["anyOf"][0]["enum"]
    assert enum == sorted({"skin_type", "concerns", *UNIVERSAL_DIMENSIONS})
    assert schema["$defs"]["Reference"]["properties"]["dimension"]["anyOf"][0]["enum"] == enum


def test_the_schema_does_not_depend_on_dimension_order():
    a = build_interpretation_schema(["b", "a", "c"])
    b = build_interpretation_schema(["c", "b", "a", "a"])
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def test_strictify_keeps_fields_named_like_schema_keywords():
    """Un câmp numit `title`/`description` e un NUME, nu metadate: nu are voie să dispară."""
    from pydantic import BaseModel, ConfigDict

    from src.conversation.interpretation import _strictify

    class Named(BaseModel):
        """docstring care nu trebuie să plece spre model"""

        model_config = ConfigDict(extra="forbid")
        title: str | None
        description: str

    schema = Named.model_json_schema()
    _strictify(schema)
    assert sorted(schema["properties"]) == ["description", "title"]
    assert schema["required"] == ["description", "title"]
    assert "description" not in {k for k in schema if k != "properties"}


def test_code_docstrings_do_not_leak_into_the_schema():
    text = json.dumps(build_interpretation_schema([]), ensure_ascii=False)
    assert '"description"' not in text and '"title"' not in text


# --- snapshotul și regula de versiune ------------------------------------------------------------


def test_the_stored_snapshot_matches_the_generator():
    stored = snap.SNAPSHOT.read_text(encoding="utf-8").replace("\r\n", "\n")
    assert stored == snap.dump(snap.current()), (
        "schema TurnInterpretation a divergat de snapshot: dacă schimbarea e intenționată, "
        "schimbă KERNEL_CONTRACT_VERSION și rulează "
        "`python scripts/kernel_schema_snapshot.py --write`"
    )
    assert json.loads(stored)["contract_version"] == KERNEL_CONTRACT_VERSION


def test_a_schema_change_without_a_version_bump_is_a_violation():
    head = snap.current()
    base = json.loads(json.dumps(head))
    base["schema"]["$defs"]["Act"]["properties"].pop("query")
    assert snap.bump_violation(base, head) is not None

    bumped = {**head, "contract_version": "kernel.v1.1"}
    assert snap.bump_violation(base, bumped) is None
    assert snap.bump_violation(head, head) is None


# --- traceul -------------------------------------------------------------------------------------


def _trace(**overrides) -> KernelTrace:
    doc = {
        "contract_version": KERNEL_CONTRACT_VERSION,
        "vocabulary_snapshot": "pack:test@1",
        "interpretation": TurnInterpretation.model_validate(DESIGN_EXAMPLE),
        "checked_changes": [],
        "resolved_refs": [
            ResolvedRef(
                ref_id="r1",
                kind="ordinal",
                outcome="exact",
                product_ids=["p2"],
                source="shown_now",
                reason=None,
            ),
            ResolvedRef(
                ref_id="r2",
                kind="ordinal",
                outcome="exact",
                product_ids=["p1"],
                source="shown_now",
                reason=None,
            ),
        ],
        "state_before": {"topic": "canapele"},
        "proposals": [],
        "rejected": [],
        "state_after": {"topic": "canapele"},
        "ambiguity": AmbiguityDecision(verdict="act", reason="all_exact", question=None),
        "plan": TurnPlan(
            executor="compare", product_ids=["p2", "p1"], search_args=None, depends_on=None
        ),
        "executor": "serve_comparison",
        "answer_policy": AnswerPolicy(verdict_allowed=True, missing=[]),
    }
    doc.update(overrides)
    return KernelTrace(**doc)


def test_i18_the_trace_requires_a_contract_version():
    with pytest.raises(ValidationError):
        _trace(contract_version="")
    with pytest.raises(ValidationError):
        _trace(contract_version="v1")
    assert _trace().contract_version == KERNEL_CONTRACT_VERSION


def test_render_has_one_line_per_layer_in_contract_order():
    text = render(_trace(), user_text="Nu, mă refeream la al doilea. Compară-l cu primul.")
    labels = [line.split("  ")[0].strip() for line in text.splitlines()]
    assert labels == [
        "USER",
        "CONTRACT",
        "INTERPRETATION",
        "CHECKED",
        "REFERENCES",
        "STATE BEFORE",
        "PROPOSALS",
        "REJECTED",
        "STATE AFTER",
        "AMBIGUITY",
        "PLAN",
        "EXECUTOR",
        "ANSWER POLICY",
    ]
    assert "r1 ordinal → exact [p2] from shown_now" in text
    assert "compare  ids=[p2,p1]" in text


def test_render_shows_search_args_without_defaults():
    plan = TurnPlan(
        executor="search",
        product_ids=[],
        search_args=SearchArgs(query="cremă de față", price_max=100, limit=6),
        depends_on=None,
    )
    line = next(line for line in render(_trace(plan=plan)).splitlines() if line.startswith("PLAN"))
    assert "SearchArgs(" in line and "price_max=100" in line and "query='cremă de față'" in line


def test_first_divergence_blames_only_the_first_wrong_layer():
    trace = _trace()
    expected = {
        "interpretation": DESIGN_EXAMPLE,
        "resolver": [r.model_dump() for r in trace.resolved_refs[::-1]],  # greșit: ordine inversă
        "plan": {"executor": "search"},  # și el greșit, dar DUPĂ resolver
    }
    divergence = first_divergence(expected, trace)
    assert divergence is not None
    assert divergence.layer == "resolver"
    assert divergence.passed == ("interpretation",)
    assert divergence.report().startswith("interpretation ✓ → resolver ✗")


def test_first_divergence_is_none_when_every_labeled_layer_matches():
    trace = _trace()
    assert first_divergence({"executor": "serve_comparison"}, trace) is None
    assert first_divergence({}, trace) is None


def test_first_divergence_refuses_an_unknown_layer_label():
    with pytest.raises(ValueError):
        first_divergence({"resolvr": []}, _trace())


def test_layer_order_is_the_contract_order():
    assert LAYER_NAMES == (
        "interpretation",
        "checked",
        "resolver",
        "reducer",
        "ambiguity",
        "plan",
        "executor",
        "answer_policy",
    )


def test_checked_change_carries_code_owned_provenance():
    change = StateChange(
        op="set",
        target=None,
        dimension="price",
        relation="lte",
        value=None,
        number=100.0,
        unit="lei",
        relative_to=None,
        quote="sub 100 lei",
    )
    checked = CheckedChange(
        change=change,
        dimension="price",
        canonical_value=100.0,
        provenance="explicit",
        strength="hard",
        rejected=None,
    )
    assert checked.provenance == "explicit"
    with pytest.raises(ValidationError):
        CheckedChange(**{**checked.model_dump(), "provenance": "user_said_so"})
