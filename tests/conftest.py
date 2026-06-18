"""Fixture-uri partajate de teste.

NX-78: `agent_stage` citește categorii + aliase din DB pt promptul generat din `categories`
(principiul 9). Testele care exersează pipeline-ul/agentul folosesc un `conn` fals
(`object()`), deci stubbim cele două query-uri global → prompt generic, fără DB reală.
Testele care vor să verifice CONȚINUTUL promptului (ex. test_agent vertical-capture) îl
suprascriu cu o fixtură locală mai specifică.
"""

import pytest

from src.worker.stages import agent as agent_mod


@pytest.fixture(autouse=True)
def _stub_agent_prompt_inputs(monkeypatch):
    async def _no_categories(conn, business_id):
        return []

    async def _no_aliases(conn, business_id, **kwargs):
        return []

    monkeypatch.setattr(agent_mod, "list_category_names", _no_categories, raising=False)
    monkeypatch.setattr(agent_mod, "list_routing_aliases", _no_aliases, raising=False)
