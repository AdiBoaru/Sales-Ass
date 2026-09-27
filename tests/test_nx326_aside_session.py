"""NX-326 (kernel v1.0, pasul 0) — o paranteză nu golește sesiunea de căutare.

Clientul e la pagina 1 a unei căutări, întreabă «cât durează livrarea?», apoi «mai arată-mi».
Înainte, ORICE răspuns fără produse scria `active_search: None`, deci paginarea nu mai avea ce
relua. Acum turul care n-a citit catalogul și nu e o clarificare păstrează sesiunea, pe v1 și pe
ramura de propuneri v2. Stub-uri DB/LLM, zero apeluri reale."""

import pytest

from src.config import get_settings
from src.models import (
    BusinessConfig,
    Contact,
    InboundMessage,
    RetrievalResult,
    Route,
    RouteDecision,
    TurnContext,
)
from src.worker.processor import _build_new_state, _turn_proposals
from src.worker.runner import PipelineDeps
from src.worker.stages import agent as agent_mod
from src.worker.stages.agent import agent_stage

SESSION = {"filters": {"category": "ten"}, "pool": ["p1", "p2", "p3"], "cursor": 6, "fp": "abc"}


@pytest.fixture
def flag(monkeypatch):
    def _set(value: bool) -> None:
        monkeypatch.setattr(get_settings(), "aside_keeps_search_session_enabled", value)

    _set(True)
    return _set


@pytest.fixture(autouse=True)
def _stub_prompt_inputs(monkeypatch):
    async def _cats(conn, business_id):
        return ["Creme"]

    async def _aliases(conn, business_id, **k):
        return []

    monkeypatch.setattr(agent_mod, "list_category_names", _cats)
    monkeypatch.setattr(agent_mod, "list_category_menu", _cats)
    monkeypatch.setattr(agent_mod, "list_routing_aliases", _aliases)


def _ctx(body: str = "cât durează livrarea?") -> TurnContext:
    ctx = TurnContext(
        turn_id="t",
        business=BusinessConfig(id="b", slug="d", name="D"),
        contact=Contact(id="c", business_id="b"),
        message=InboundMessage(provider_msg_id="m", body=body),
        conversation_id="conv",
    )
    ctx.route = RouteDecision(route=Route.SALES)
    return ctx


def _after(ctx: TurnContext) -> dict:
    return _build_new_state(
        {"active_search": dict(SESSION)}, ctx, is_rich=False, has_products=False
    )


# --- regula, pe _build_new_state ------------------------------------------------------------


def test_a_turn_that_never_touched_the_catalog_keeps_the_session(flag):
    ctx = _ctx()
    ctx.set_reply("Livrarea durează câteva zile lucrătoare.")
    assert _after(ctx)["active_search"] == SESSION


def test_a_tool_loop_turn_without_catalog_tools_keeps_the_session(flag):
    ctx = _ctx()
    ctx.retrieval = RetrievalResult(products=[], source="tools", catalog_read=False)
    ctx.set_reply("Livrarea durează câteva zile lucrătoare.")
    assert _after(ctx)["active_search"] == SESSION


def test_a_search_with_zero_results_still_closes_the_session(flag):
    ctx = _ctx("vreau un parfum")
    ctx.retrieval = RetrievalResult(products=[], source="tools", catalog_read=True)
    ctx.set_reply("N-am găsit parfumuri.")
    assert _after(ctx)["active_search"] is None


def test_a_clarification_still_closes_the_session(flag):
    ctx = _ctx("vreau ceva pentru păr")
    ctx.set_clarify("Pentru ce tip de păr?", field="hair_type", resume_route="sales")
    assert _after(ctx)["active_search"] is None


def test_flag_off_closes_the_session_as_before(flag):
    flag(False)
    ctx = _ctx()
    ctx.set_reply("Livrarea durează câteva zile lucrătoare.")
    assert _after(ctx)["active_search"] is None


def test_the_v2_proposals_follow_the_same_rule(flag):
    ctx = _ctx()
    ctx.set_reply("Livrarea durează câteva zile lucrătoare.")
    ops = [p.op for p in _turn_proposals(ctx, is_rich=False, has_products=False)]
    assert "set_active_search" not in ops

    ctx = _ctx("vreau un parfum")
    ctx.retrieval = RetrievalResult(products=[], source="tools", catalog_read=True)
    ctx.set_reply("N-am găsit parfumuri.")
    ops = [p.op for p in _turn_proposals(ctx, is_rich=False, has_products=False)]
    assert "set_active_search" in ops


# --- cap-coadă: bucla de tool-uri decide `catalog_read` -------------------------------------


class _ScriptedLoop:
    def __init__(self, tool_calls, final):
        self._tool_calls = tool_calls
        self._final = final

    async def embed(self, texts, *, model=None):
        return [[0.0] * 8 for _ in texts]

    async def complete(self, *a, **k):
        return self._final

    async def run_tool_loop(self, system, user, tools, execute, **kw):
        for name, args in self._tool_calls:
            await execute(name, args)
        return self._final


async def test_a_faq_only_turn_is_an_aside_end_to_end(flag, monkeypatch):
    async def fake_faqs(conn, business_id, locale, *, limit):
        return [{"question": "Livrare", "answer": "Livrarea durează câteva zile lucrătoare."}]

    monkeypatch.setattr("src.tools.faq_tools.list_active", fake_faqs)
    llm = _ScriptedLoop([("faq_lookup", {"query": "livrare"})], "Livrarea durează câteva zile.")
    ctx = _ctx()
    await agent_stage(ctx, PipelineDeps(conn=object(), redis=None, llm=llm))

    assert ctx.retrieval is not None and ctx.retrieval.catalog_read is False
    assert _after(ctx)["active_search"] == SESSION


async def test_a_search_turn_reads_the_catalog_end_to_end(flag, monkeypatch):
    from src.tools import catalog_tools as ct

    async def nothing(*a, **k):
        return []

    monkeypatch.setattr(ct, "search_products_lexical", nothing)
    monkeypatch.setattr(ct, "search_products_semantic", nothing)
    llm = _ScriptedLoop([("search_products", {"query": "parfum"})], "N-am găsit parfumuri.")
    ctx = _ctx("vreau un parfum")
    await agent_stage(ctx, PipelineDeps(conn=object(), redis=None, llm=llm))

    assert ctx.retrieval is not None and ctx.retrieval.catalog_read is True
    assert _after(ctx)["active_search"] is None
