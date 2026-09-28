"""NX-342 (342a) — sesiunea de căutare reține DECIZIILE paginii 1, nu doar intrările ei.

Trei scurgeri, fiecare cu testul ei, toate verificate că pică pe `main`:

- §2 un set ascuns de NX-306 lăsa sesiunea deschisă, deci „mai arată-mi" servea restul setului
  refuzat;
- §3 un raft respins ca ghicitură (NX-305/313) rămânea în filtrele sesiunii și revenea la căutarea
  următoare ca RAFT ROSTIT (NX-299 tratează moștenirea drept rostită);
- §4 o nevoie retrasă după crearea sesiunii se moștenea în continuare.

Cu `SEARCH_SESSION_CONTRACT_ENABLED` stins, totul e ca înainte (testat)."""

from __future__ import annotations

import pytest

from src.agent.finalize import render
from src.config import get_settings
from src.conversation.state_v2 import ConversationStateV2, Revocation
from src.tools import catalog_tools as ct
from src.tools.catalog_tools import (
    inheritable_filters,
    search_products_tool,
    session_decisions,
)
from src.worker.runner import PipelineDeps
from tests.test_refused_set_withheld import _LLM as _RenderLLM
from tests.test_refused_set_withheld import _ctx as _refused_ctx
from tests.test_refused_set_withheld import _plan
from tests.test_search_filter_inherit import _LLM as _SearchLLM
from tests.test_search_filter_inherit import _capture_lexical, _ctx_with_session  # noqa: F401

FILTERS = {
    "query": "crema de fata",
    "category": "fata",
    "concerns": ["dry"],
    "sort_mode": "relevance",
}


@pytest.fixture
def contract_off(monkeypatch):
    monkeypatch.setattr(get_settings(), "search_session_contract_enabled", False)


def _deps(llm):
    return PipelineDeps(conn=object(), redis=None, llm=llm)


# --- §2: setul ascuns închide sesiunea ------------------------------------------------------------


def _session() -> dict:
    return {"filters": dict(FILTERS), "pool": ["b1", "b2", "b3", "b4", "b5", "b6", "b7"], "fp": "F"}


async def test_a_withheld_set_closes_the_session_it_came_from():
    ctx = _refused_ctx(relaxed=True)
    ctx.state_patch["active_search"] = _session()  # scrisă de unealtă pe același tur
    await render(ctx, _deps(_RenderLLM()), _plan())
    assert ctx.reply is not None and ctx.reply.products == []
    assert ctx.state_patch["active_search"] is None  # pe `main`: sesiunea rămânea paginabilă
    closed = [e for e in ctx.events if e.type == "search_session"]
    assert closed and closed[-1].properties["closed_reason"] == "withheld"


async def test_an_exact_refused_set_keeps_its_session():
    """NX-306 ascunde doar setul RELAXAT; unul exact rămâne pe ecran, deci și sesiunea lui."""
    ctx = _refused_ctx(relaxed=False)
    ctx.state_patch["active_search"] = _session()
    await render(ctx, _deps(_RenderLLM()), _plan())
    assert ctx.state_patch["active_search"] == _session()


async def test_withheld_with_the_contract_off_is_as_before(contract_off):
    ctx = _refused_ctx(relaxed=True)
    ctx.state_patch["active_search"] = _session()
    await render(ctx, _deps(_RenderLLM()), _plan())
    assert ctx.state_patch["active_search"] == _session()


# --- §3: filtrele EFECTIVE ale paginii 1 ----------------------------------------------------------


def test_a_dropped_guessed_shelf_is_not_inheritable():
    decided = session_decisions(FILTERS, None, category_dropped=True, facets_dropped=False)
    assert decided["inherit"] == {"category": None, "concerns": ["dry"]}
    sess = {**_session(), **decided}
    assert inheritable_filters(sess, None)["category"] is None
    assert sess["filters"]["category"] == "fata"  # amprenta cererii rămâne neatinsă


def test_dropped_guessed_facets_are_not_inheritable():
    decided = session_decisions(FILTERS, None, category_dropped=False, facets_dropped=True)
    assert decided["inherit"] == {"category": "fata", "concerns": None}


async def test_the_next_search_does_not_inherit_the_rejected_shelf(_capture_lexical):  # noqa: F811
    ctx = _ctx_with_session(category="creme-hidratante", concerns=["dry"])
    ctx.state.active_search = {
        **ctx.state.active_search,
        "inherit": {"category": None, "concerns": ["dry"]},
    }
    await search_products_tool(
        ctx, _deps(_SearchLLM()), {"query": "ceva mai ieftin", "sort_mode": "price_asc"}
    )
    assert _capture_lexical["category"] is None  # pe `main`: ["creme-hidratante"], ca rostit
    assert _capture_lexical["concerns"] == ["dry"]
    ev = [e for e in ctx.events if e.type == "search_filter_inherited"]
    assert ev and ev[0].properties["fields"] == ["concerns"]


async def test_a_new_session_records_its_decisions(_capture_lexical):  # noqa: F811
    ctx = _ctx_with_session(category="creme-hidratante", concerns=["dry"])
    ctx.state.active_search = None
    ctx.state_v2 = ConversationStateV2(revision=5)
    await search_products_tool(
        ctx, _deps(_SearchLLM()), {"query": "crema", "sort_mode": "relevance"}
    )
    sess = ctx.state_patch["active_search"]
    assert set(sess) >= {"filters", "pool", "fp", "inherit", "rev"}
    assert sess["rev"] == 5


# --- §4: o nevoie retrasă după crearea sesiunii ---------------------------------------------------


def _v2(*revisions: int) -> ConversationStateV2:
    return ConversationStateV2(
        revision=max(revisions, default=0),
        revocations=tuple(Revocation(key="skin_type", revision=r) for r in revisions),
    )


def test_a_need_retracted_after_the_session_is_not_inherited():
    sess = {**_session(), "inherit": {"category": "fata", "concerns": ["dry"]}, "rev": 3}
    assert inheritable_filters(sess, _v2(4))["concerns"] is None
    assert inheritable_filters(sess, _v2(4))["category"] == "fata"


def test_a_retraction_older_than_the_session_changes_nothing():
    sess = {**_session(), "inherit": {"category": "fata", "concerns": ["dry"]}, "rev": 3}
    assert inheritable_filters(sess, _v2(2))["concerns"] == ["dry"]
    assert inheritable_filters(sess, None)["concerns"] == ["dry"]  # fără stare v2: nimic de știut


async def test_the_retracted_need_does_not_reach_the_next_search(_capture_lexical):  # noqa: F811
    ctx = _ctx_with_session(category="creme-hidratante", concerns=["dry"])
    ctx.state.active_search = {
        **ctx.state.active_search,
        "inherit": {"category": "creme-hidratante", "concerns": ["dry"]},
        "rev": 3,
    }
    ctx.state_v2 = _v2(4)
    await search_products_tool(
        ctx, _deps(_SearchLLM()), {"query": "crema", "sort_mode": "price_asc"}
    )
    assert _capture_lexical["concerns"] is None  # pe `main`: ["dry"], deși clientul a retras-o
    assert _capture_lexical["category"] == ["creme-hidratante"]


# --- flag stins și sesiunile vechi ----------------------------------------------------------------


def test_contract_off_writes_no_decisions_and_reads_the_old_filters(contract_off):
    assert session_decisions(FILTERS, _v2(1), category_dropped=True, facets_dropped=True) == {}
    sess = {**_session(), "inherit": {"category": None, "concerns": None}, "rev": 0}
    assert inheritable_filters(sess, _v2(9)) == FILTERS


def test_a_session_written_before_nx342_inherits_as_before():
    assert inheritable_filters(_session(), _v2(9)) == FILTERS


def test_the_session_stays_within_the_v2_key_budget():
    from src.conversation.state_v2 import MAX_ACTIVE_SEARCH_KEYS, bounded_map

    sess = {
        **_session(),
        "cursor": 6,
        "page": 0,
        **session_decisions(FILTERS, _v2(2), category_dropped=True, facets_dropped=False),
    }
    kept = bounded_map(sess)
    assert len(sess) <= MAX_ACTIVE_SEARCH_KEYS
    assert kept is not None and "inherit" in kept and kept["rev"] == 2


def test_the_search_module_exposes_both_helpers():
    assert (
        ct.session_decisions is session_decisions and ct.inheritable_filters is inheritable_filters
    )
