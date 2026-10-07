"""NX-335 felia 5b — apelul de interpretare: ce a întors furnizorul, efortul și temperatura.

Două goluri ale clientului, găsite scriind cardul (Architecture Review, punctele 1-2):

- `complete_schema` întoarce doar JSON-ul parsat, cu `{}` în loc de conținut lipsă, deci un refuz,
  un conținut gol și un răspuns tăiat la plafon arată la fel. `complete_schema_raw` le expune;
- efortul și temperatura se moștenesc de la agent (`low`, 0,7), deci interpretarea ar fi raționat
  din prima zi, contra designului (§A: prima versiune rulează la `none`).

Invariantul care ține calea vie neatinsă: fără parametrii noi, CEREREA (kwargs pe sârmă) și
rezultatul `complete_schema` sunt identice cu `main`. ZERO OpenAI."""

from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from src.agent import llm as llm_mod
from src.agent import usage
from src.agent.llm import LLMClient
from src.config import get_settings
from src.observability import turn_latency


class _Completions:
    def __init__(self, content='{"a": 1}', *, refusal=None, finish_reason="stop"):
        self.kwargs: list[dict] = []
        self._reply = (content, refusal, finish_reason)

    async def create(self, **kwargs):
        self.kwargs.append(kwargs)
        content, refusal, finish = self._reply
        return NS(
            choices=[NS(message=NS(content=content, refusal=refusal), finish_reason=finish)],
            usage=NS(
                prompt_tokens=10,
                completion_tokens=5,
                prompt_tokens_details=None,
                completion_tokens_details=NS(reasoning_tokens=0),
            ),
        )


def _client(completions: _Completions, model: str = "gpt-6-luna") -> LLMClient:
    return LLMClient(NS(chat=NS(completions=completions)), model_agent=model)


SCHEMA = {"name": "sales_recommendation", "strict": True, "schema": {}}

#: Cererea pe care `main@2858908` o trimitea pentru `complete_schema` pe `gpt-6-luna`, cu setările
#: suitei (`LLM_REASONING_EFFORT_AGENT=low` ⇒ raționament pornit ⇒ fără temperatură, ceasul NX-311
#: de 75 s). Capturată pe `main` înainte de schimbare, nu dedusă din codul nou.
MAIN_KWARGS = {
    "model": "gpt-6-luna",
    "messages": [{"role": "system", "content": "S"}, {"role": "user", "content": "U"}],
    "response_format": {"type": "json_schema", "json_schema": SCHEMA},
    "reasoning_effort": "low",
    "timeout": 75.0,
}


@pytest.fixture(autouse=True)
def _suite_settings():
    s = get_settings()
    assert s.llm_reasoning_effort_agent == "low", "testul fixează cererea pe setările suitei"
    yield


async def test_complete_schema_request_and_result_are_byte_identical_to_main():
    completions = _Completions()
    out = await _client(completions).complete_schema("S", "U", SCHEMA)
    assert out == {"a": 1}
    assert completions.kwargs == [MAIN_KWARGS]


async def test_complete_schema_still_returns_an_empty_dict_on_empty_content():
    out = await _client(_Completions(content=None)).complete_schema("S", "U", SCHEMA)
    assert out == {}


async def test_complete_schema_raw_exposes_what_the_provider_returned():
    completions = _Completions(content=None, refusal="nu pot", finish_reason="content_filter")
    reply = await _client(completions).complete_schema_raw("S", "U", SCHEMA)
    assert reply.content is None
    assert reply.refusal == "nu pot"
    assert reply.finish_reason == "content_filter"
    # fără efort/temperatură cerute, aceeași cerere ca `complete_schema`
    assert completions.kwargs == [MAIN_KWARGS]


async def test_interpret_effort_none_sends_none_and_the_interpret_temperature():
    """`none` pe `gpt-6-luna` ⇒ `reasoning_effort="none"` și `temperature=0.2` pe sârmă, iar
    ceasul e al unui apel care NU raționează (30 s, NX-311), ca runda 1 de azi."""
    s = get_settings()
    completions = _Completions()
    llm = _client(completions)
    await llm.complete_schema_raw(
        "S",
        "U",
        {"name": "turn_interpretation", "strict": True, "schema": {}},
        reasoning_effort="none",
        temperature=s.llm_temperature_interpret,
    )
    sent = completions.kwargs[0]
    assert s.llm_temperature_interpret == 0.2
    assert sent["reasoning_effort"] == "none"
    assert sent["temperature"] == 0.2
    assert sent["timeout"] == 30.0


async def test_interpret_effort_default_is_low_and_drops_the_temperature():
    """2026-10-07: interpretarea raționează la `low` (măsurat pe `fresh-2026-10-07`). Cu
    raționamentul pornit furnizorul refuză o temperatură ≠ 1, deci ea nu pleacă, iar ceasul e
    al unui apel care raționează (NX-311), nu cei 30 s ai rundei fără raționament."""
    s = get_settings()
    completions = _Completions()
    llm = _client(completions)
    await llm.complete_schema_raw(
        "S",
        "U",
        {"name": "turn_interpretation", "strict": True, "schema": {}},
        reasoning_effort=s.llm_reasoning_effort_interpret,
        temperature=s.llm_temperature_interpret,
    )
    sent = completions.kwargs[0]
    assert s.llm_reasoning_effort_interpret == "low"
    assert sent["reasoning_effort"] == "low"
    assert "temperature" not in sent
    assert sent["timeout"] > 30.0


def test_sampling_without_overrides_is_the_agent_sampling():
    llm = _client(_Completions())
    base = llm._sampling(agent=True, model="gpt-6-luna")
    same = llm._sampling(agent=True, model="gpt-6-luna", effort=None, temperature=None)
    assert base == same
    assert base.params == {"reasoning_effort": "low"}
    assert base.reasoning_on is True


def test_sampling_override_beats_the_agent_setting():
    llm = _client(_Completions())
    s = llm._sampling(agent=True, model="gpt-6-luna", effort="none", temperature=0.2)
    assert s.params == {"reasoning_effort": "none", "temperature": 0.2}
    assert s.reasoning_on is False
    # raționament pornit ⇒ temperatura nu pleacă, ca azi
    on = llm._sampling(agent=True, model="gpt-6-luna", effort="low", temperature=0.2)
    assert on.params == {"reasoning_effort": "low"}
    assert on.reasoning_on is True


def test_a_profile_without_reasoning_effort_does_not_send_it_and_counts(monkeypatch):
    """Profil monkeypatched fără `reasoning_effort` (niciun profil declarat azi nu e așa) ⇒
    parametrul nu pleacă, `llm_param_unsupported_reasoning_effort` numărat, ca azi."""
    profile = llm_mod.ModelProfile(frozenset({"temperature"}), False)
    monkeypatch.setattr(llm_mod, "model_profile", lambda model: profile)
    llm = _client(_Completions())
    acc, token = turn_latency.push()
    try:
        s = llm._sampling(agent=True, model="x-model", effort="none", temperature=0.2)
    finally:
        turn_latency.pop(token)
    assert "reasoning_effort" not in s.params
    assert acc.degradations.get("llm_param_unsupported_reasoning_effort") == 1


async def test_per_call_rows_of_todays_calls_gain_no_purpose_key():
    acc, token = usage.push()
    try:
        await _client(_Completions()).complete_schema("S", "U", SCHEMA)
    finally:
        usage.pop(token)
    assert acc.call_rows and "purpose" not in acc.call_rows[0]


async def test_the_interpretation_call_is_labelled_interpret_in_per_call():
    acc, token = usage.push()
    try:
        await _client(_Completions()).complete_schema_raw(
            "S", "U", {"name": "turn_interpretation", "strict": True, "schema": {}}
        )
    finally:
        usage.pop(token)
    assert acc.call_rows[0]["purpose"] == "interpret"


def test_scripted_llm_covers_complete_schema_raw():
    from src.evals.scripted_llm import ScriptedLLM

    assert hasattr(ScriptedLLM, "complete_schema_raw")


async def test_scripted_llm_complete_schema_raw_returns_a_schema_reply():
    """NX-335, recenzia (punctul 11): conținutul e o `TurnInterpretation` validă, nu planul
    creierului (pasul 6 l-ar fi citit ca `schema_violation` pe fiecare tur)."""
    from src.conversation.interpretation import TurnInterpretation
    from src.evals.scripted_llm import ScriptedLLM

    reply = await ScriptedLLM({"plan_repair": {"x": 1}}).complete_schema_raw("S", "U", {})
    TurnInterpretation.model_validate_json(reply.content)
    assert reply.refusal is None
    assert reply.finish_reason == "stop"
