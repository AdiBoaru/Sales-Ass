"""NX-342 (342a) — sesiunea de căutare reține DECIZIILE paginii 1, nu doar intrările ei.

Trei scurgeri, fiecare cu testul ei:

- §2 un set ascuns de NX-306 lăsa sesiunea deschisă, deci „mai arată-mi" servea restul setului
  refuzat;
- §3 un raft respins ca ghicitură (NX-305/313) rămânea în filtrele sesiunii și revenea la căutarea
  următoare ca RAFT ROSTIT (NX-299 tratează moștenirea drept rostită);
- §4 o nevoie retrasă după crearea sesiunii se moștenea în continuare.

Recenzia independentă a găsit că prima versiune NU funcționa în producție: starea se reîncarcă la
fiecare tur prin `ConversationState.from_jsonb`, care păstra doar cinci chei ale sesiunii, iar
testele setau `ctx.state.active_search` direct. De aceea testele de aici trec prin REÎNCĂRCARE.
Cu `SEARCH_SESSION_CONTRACT_ENABLED` stins, totul e ca înainte (testat)."""

from __future__ import annotations

import pytest

from src.agent.finalize import render
from src.config import get_settings
from src.conversation.state_v2 import (
    MAX_ACTIVE_SEARCH_KEYS,
    ConversationStateV2,
    Revocation,
    bounded_map,
)
from src.domain.pack import DomainPack
from src.models import ConversationState
from src.tools.catalog_tools import inheritable_filters, search_products_tool, session_decisions
from src.worker.runner import PipelineDeps
from tests.test_nx319_conversation_1518 import _LLM as _Nx319LLM
from tests.test_nx319_conversation_1518 import _ctx as _nx319_ctx
from tests.test_nx319_conversation_1518 import lexical_calls  # noqa: F401 — fixture
from tests.test_refused_set_withheld import _LLM as _RenderLLM
from tests.test_refused_set_withheld import _ctx as _refused_ctx
from tests.test_refused_set_withheld import _plan
from tests.test_search_filter_inherit import _LLM as _SearchLLM
from tests.test_search_filter_inherit import _capture_lexical, _ctx_with_session  # noqa: F401

FILTERS = {
    "query": "crema de fata",
    "category": "fata",
    "concerns": ["dry"],
    "price_max": None,
    "sort_mode": "relevance",
}
PACK = DomainPack(vertical="ecommerce")


@pytest.fixture
def contract_off(monkeypatch):
    monkeypatch.setattr(get_settings(), "search_session_contract_enabled", False)


def _deps(llm):
    return PipelineDeps(conn=object(), redis=None, llm=llm)


def _session(**decided) -> dict:
    sess = {"filters": dict(FILTERS), "pool": ["b1", "b2", "b3", "b4", "b5", "b6", "b7"], "fp": "F"}
    sess.update(decided)
    return sess


def _reloaded(sess: dict) -> dict:
    """Ce vede turul URMĂTOR: starea trece prin `bounded_map` (v2) și `from_jsonb` (v1)."""
    return ConversationState.from_jsonb({"active_search": bounded_map(sess)}).active_search


def _v2(*revocations: Revocation, revision: int = 0) -> ConversationStateV2:
    return ConversationStateV2(revision=revision, revocations=revocations)


def _decided(rev: int = 3, turn: str = "t-session") -> dict:
    return {"inherit": {"category": "fata", "concerns": ["dry"], "turn": turn, "rev": rev}}


def _later(key: str = "skin_type", turn: str = "t-later", revision: int = 4) -> ConversationStateV2:
    return _v2(Revocation(key=key, source_turn_id=turn, revision=revision), revision=revision)


# --- blocantul: deciziile supraviețuiesc reîncărcării ------------------------------------------


def test_the_decisions_survive_the_reload_the_next_turn_goes_through():
    decided = session_decisions(
        FILTERS, _v2(revision=3), "t-1", category_dropped=True, facets_dropped=False
    )
    after = _reloaded({**_session(), "cursor": 6, "page": 0, **decided})
    assert after is not None and "inherit" in after  # prima versiune: cheia dispărea aici
    assert inheritable_filters(after, None, PACK)["category"] is None
    assert after["filters"]["category"] == "fata"  # amprenta cererii rămâne neatinsă


def test_the_session_stays_within_the_v2_key_budget():
    decided = session_decisions(
        FILTERS, _v2(revision=2), "t", category_dropped=True, facets_dropped=False
    )
    sess = {**_session(), "cursor": 6, "page": 0, **decided}
    assert len(sess) <= MAX_ACTIVE_SEARCH_KEYS
    kept = bounded_map(sess)
    assert kept is not None and kept["inherit"]["rev"] == 2 and kept["inherit"]["turn"] == "t"


# --- §2: setul ascuns închide sesiunea ---------------------------------------------------------


async def test_a_withheld_set_closes_the_session_it_came_from():
    ctx = _refused_ctx(relaxed=True)
    ctx.state_patch["active_search"] = _session()  # scrisă de unealtă pe același tur
    await render(ctx, _deps(_RenderLLM()), _plan())
    assert ctx.reply is not None and ctx.reply.products == []
    assert ctx.state_patch["active_search"] is None  # pe `main`: sesiunea rămânea paginabilă
    closed = [e for e in ctx.events if e.type == "search_session_closed"]
    assert closed and closed[0].properties["reason"] == "withheld"
    assert not [e for e in ctx.events if e.type == "search_session"]  # forma NX-303 neatinsă


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


async def test_withheld_without_sessions_writes_nothing(monkeypatch):
    monkeypatch.setattr(get_settings(), "search_sessions_enabled", False)
    ctx = _refused_ctx(relaxed=True)
    await render(ctx, _deps(_RenderLLM()), _plan())
    assert "active_search" not in ctx.state_patch
    assert not [e for e in ctx.events if e.type == "search_session_closed"]


def test_an_explicitly_closed_session_offers_no_paging_button():
    """`_commit_facts` trata `None` ca „nicio veste" și cădea pe sesiunea turului trecut."""
    from src.worker.processor import _commit_facts

    ctx = _refused_ctx(relaxed=True)
    ctx.state.active_search = _session()
    ctx.state_patch["active_search"] = None
    assert _commit_facts(ctx).active_search_ref is None


# --- §3: filtrele EFECTIVE ale paginii 1 --------------------------------------------------------


def test_dropped_guessed_facets_are_not_inheritable():
    decided = session_decisions(FILTERS, None, "t", category_dropped=False, facets_dropped=True)
    assert inheritable_filters(_reloaded({**_session(), **decided}), None, PACK) == {
        "category": "fata",
        "concerns": None,
    }


async def test_the_rescue_records_the_dropped_shelf_end_to_end(lexical_calls):  # noqa: F811
    """Turul `707fde73` cap-coadă (harnessul NX-319): NX-313 scoate raftul „Fata", iar sesiunea
    scrisă de CĂUTARE îl marchează nemoștenibil, păstrând cererea în `filters`."""
    ctx = _nx319_ctx("vreau o crema de fata")
    await search_products_tool(
        ctx, _deps(_Nx319LLM()), {"query": "crema de fata", "category": "fata"}
    )
    assert [e for e in ctx.events if e.type == "guessed_filter_rescued"]
    sess = ctx.state_patch["active_search"]
    assert sess["filters"]["category"] == "fata"
    assert sess["inherit"]["category"] is None
    assert inheritable_filters(_reloaded(sess), None, PACK)["category"] is None


async def test_the_next_search_does_not_inherit_the_rejected_shelf(_capture_lexical):  # noqa: F811
    ctx = _ctx_with_session(category="creme-hidratante", concerns=["dry"])
    ctx.state.active_search = _reloaded(
        {**ctx.state.active_search, "inherit": {"category": None, "concerns": ["dry"]}}
    )
    await search_products_tool(
        ctx, _deps(_SearchLLM()), {"query": "ceva mai ieftin", "sort_mode": "price_asc"}
    )
    assert _capture_lexical["category"] is None  # pe `main`: ["creme-hidratante"], ca rostit
    assert _capture_lexical["concerns"] == ["dry"]
    ev = [e for e in ctx.events if e.type == "search_filter_inherited"]
    assert ev and ev[0].properties["fields"] == ["concerns"]


async def test_the_session_price_bound_is_still_the_requests(lexical_calls):  # noqa: F811
    """Recenzia: sursa NX-319 „aceeași margine ca sesiunea" citea filtrele MOȘTENIBILE, care n-au
    preț, deci „mai arată-mi" cu aceeași margine pierdea marginea."""
    ctx = _nx319_ctx("mai arata-mi")
    ctx.state.active_search = _reloaded(
        {
            "filters": {**FILTERS, "query": "crema anti aging", "price_max": 29.99},
            "pool": ["x1"],
            "fp": "OLD",
            "inherit": {"category": None, "concerns": None, "turn": "t0", "rev": 1},
        }
    )
    await search_products_tool(
        ctx, _deps(_Nx319LLM()), {"query": "crema anti aging", "price_max": 29.99}
    )
    ev = [e for e in ctx.events if e.type == "price_bound_provenance"]
    assert ev and ev[0].properties["source"] == "session" and ev[0].properties["kept"] is True


# --- §4: o nevoie retrasă după crearea sesiunii -------------------------------------------------


def test_a_need_retracted_on_a_later_turn_is_not_inherited():
    sess = _reloaded({**_session(), **_decided()})
    assert inheritable_filters(sess, _later(), PACK) == {"category": "fata", "concerns": None}


def test_a_retraction_from_the_session_turn_itself_changes_nothing():
    """«de fapt am tenul gras» retrage `dry` și caută `oily` în ACELAȘI tur: nevoia corectată e deja
    în argumentele căutării, deci nu se șterge la turul următor (recenzia, scenariul A)."""
    sess = _reloaded({**_session(), **_decided()})
    assert inheritable_filters(sess, _later(turn="t-session"), PACK)["concerns"] == ["dry"]


def test_a_budget_retraction_does_not_touch_the_inherited_needs():
    """Recenzia, scenariul B: o corecție de buget suprima nevoile moștenite."""
    sess = _reloaded({**_session(), **_decided()})
    assert inheritable_filters(sess, _later(key="budget_max"), PACK)["concerns"] == ["dry"]


def test_a_retraction_older_than_the_session_changes_nothing():
    sess = _reloaded({**_session(), **_decided(rev=3)})
    assert inheritable_filters(sess, _later(revision=3), PACK)["concerns"] == ["dry"]
    assert inheritable_filters(sess, None, PACK)["concerns"] == ["dry"]  # fără stare v2


async def test_the_retracted_need_does_not_reach_the_next_search(_capture_lexical):  # noqa: F811
    ctx = _ctx_with_session(category="creme-hidratante", concerns=["dry"])
    ctx.state.active_search = _reloaded(
        {
            **ctx.state.active_search,
            "inherit": {
                "category": "creme-hidratante",
                "concerns": ["dry"],
                "turn": "t0",
                "rev": 3,
            },
        }
    )
    ctx.state_v2 = _later(turn="t1")
    await search_products_tool(
        ctx, _deps(_SearchLLM()), {"query": "crema", "sort_mode": "price_asc"}
    )
    assert _capture_lexical["concerns"] is None  # pe `main`: ["dry"], deși clientul a retras-o
    assert _capture_lexical["category"] == ["creme-hidratante"]


# --- flag stins și sesiunile vechi --------------------------------------------------------------


def test_contract_off_writes_no_decisions_and_reads_the_old_filters(contract_off):
    assert session_decisions(FILTERS, _v2(), "t", category_dropped=True, facets_dropped=True) == {}
    sess = _reloaded({**_session(), **_decided()})
    assert inheritable_filters(sess, _later(revision=9), PACK) == sess["filters"]


def test_a_session_written_before_nx342_inherits_as_before():
    old = _reloaded(_session())
    assert "inherit" not in old
    assert inheritable_filters(old, _later(revision=9), PACK) == old["filters"]
