"""NX-335 felia 5b — eticheta apelului în `llm_usage.per_call` (`purpose`), derivată din CERERE.

Interpretarea și compunerea bogată au amândouă forma `schema`, deci verificarea I13 a pasului 6
(„exact un apel `interpret` pe tur interpretat") n-ar avea pe ce număra. Eticheta vine din numele
schemei (`response_format.json_schema.name`), ca `request_shape` (P10: apelantul nu-și declară
scopul). Un nume necunoscut ⇒ cheia lipsește, deci rândurile de azi rămân identice."""

from __future__ import annotations

import pytest

from src.agent import usage

TODAY_SCHEMAS = (
    "answer_plan_v1",
    "answer_plan_v2",
    "answer_plan_critic_v1",
    "comparison_narrative",
    "sales_recommendation",
    "faq_set",
)


def _schema_call(name: str) -> dict:
    return {
        "model": "m",
        "messages": [],
        "response_format": {"type": "json_schema", "json_schema": {"name": name, "schema": {}}},
    }


def test_the_interpretation_schema_maps_to_interpret():
    assert usage.CALL_PURPOSES == {"turn_interpretation": "interpret"}
    assert usage.request_purpose(_schema_call("turn_interpretation")) == "interpret"


@pytest.mark.parametrize("name", TODAY_SCHEMAS)
def test_todays_schema_names_have_no_purpose(name):
    assert usage.request_purpose(_schema_call(name)) is None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"model": "m", "messages": []},
        {"model": "m", "messages": [], "tools": [{"type": "function"}]},
        {"model": "m", "messages": [], "response_format": {"type": "json_object"}},
        {"model": "m", "messages": [], "response_format": "garbage"},
        {"model": "m", "messages": [], "response_format": {"type": "json_schema"}},
    ],
)
def test_calls_without_a_named_schema_have_no_purpose(kwargs):
    assert usage.request_purpose(kwargs) is None


def test_record_call_writes_purpose_only_when_given():
    acc, token = usage.push()
    try:
        usage.record_call(None, shape="schema", reasoning=False, ms=1.0, ok=False)
        usage.record_call(
            None, shape="schema", reasoning=False, ms=1.0, ok=False, purpose="interpret"
        )
        usage.record_call(None, shape="schema", reasoning=False, ms=1.0, ok=False, purpose="x")
    finally:
        usage.pop(token)
    first, second, third = acc.call_rows
    assert "purpose" not in first
    assert second["purpose"] == "interpret"
    # vocabular închis: un scop necunoscut nu intră pe rând
    assert "purpose" not in third
