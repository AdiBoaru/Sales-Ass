"""NX-409 — agentul nu mai caută de 3-4 ori produse care nu există.

Pe 2026-10-10, «ce creme de fata ai pentru ten gras»: trei căutări la rând (23,4 s), deși prima le
avea deja pe toate cele 4 creme de față în stoc care spun „ten gras”; «Recomandă-mi un SPF»: patru
runde de unelte (25,3 s). Cauza: promptul cerea „sub 5 potrivite ⇒ caută din nou”, iar agentul nu
avea de unde ști câte potriviri există. Acum căutarea aduce tot setul nevoilor (a treia interogare,
în paralel) și spune câte sunt (`need_matches`), promptul cere căutările deodată, iar o rundă de
căutare peste plafon e refuzată. Zero apeluri de model și zero DB.
"""

from __future__ import annotations

import json

from src.assistant import prompt as aprompt
from src.assistant import search as asearch
from src.config import get_settings
from tests.test_nx396_assistant import (  # noqa: F401 — `_catalog` e fixture autouse
    ScriptedLLM,
    _ans,
    _call,
    _catalog,
    _run,
    _search,
)
from tests.test_nx404_agent_search_ranking import _no_db, _p

OILY = ["skin_type:dry"]  # singura nevoie din meniul fals; numele nu contează aici
SHELF = {"product_types": ["crema de fata"]}
NEED_ARGS = {"query": "crema", **SHELF, "needs": ["skin_type:oily"]}


def _tool_output(llm, call_id):
    for items in reversed(llm.inputs):
        for i in items:
            if (
                isinstance(i, dict)
                and i.get("type") == "function_call_output"
                and i.get("call_id") == call_id
            ):
                return json.loads(i["output"])
    raise AssertionError(call_id)


# --- căutarea aduce tot setul nevoilor ---------------------------------------------------------


def _recording(monkeypatch, sizes=None):
    calls = []

    async def fake(conn, business_id, query, **kw):
        calls.append(kw)
        n = len(calls)
        size = (sizes or {}).get(n, 1)
        return [_p(f"r{n}-{i}", f"Crema {n}-{i}") for i in range(size)]

    import src.db.queries.catalog as cat

    monkeypatch.setattr(cat, "search_products_lexical", fake)
    return calls


async def test_with_needs_and_a_subject_the_whole_need_set_is_fetched_and_counted(monkeypatch):
    calls = _recording(monkeypatch)
    stats: dict = {}
    await asearch.fetch_candidates(_no_db, "b", NEED_ARGS, locale="ro", stats=stats)
    needs_only = [c for c in calls[:3] if c.get("only_filters_step")]
    assert [c["facet_filters"] for c in needs_only] == [
        {"product_type": ["crema de fata"], "skin_type": ["oily"]}
    ]
    assert stats == {"need_counted": True, "need_pool_full": False}


async def test_a_full_pool_is_reported_as_at_least(monkeypatch):
    _recording(monkeypatch, sizes={3: asearch.POOL})
    stats: dict = {}
    await asearch.fetch_candidates(_no_db, "b", NEED_ARGS, locale="ro", stats=stats)
    assert stats["need_pool_full"] is True


async def test_a_need_without_a_subject_is_not_fetched_whole(monkeypatch):
    """Setul unei nevoi singure e tot catalogul care o poartă (pe SOLE `oily` e și pe luciuri)."""
    calls = _recording(monkeypatch)
    stats: dict = {}
    await asearch.fetch_candidates(
        _no_db, "b", {"query": "crema", "needs": ["skin_type:oily"]}, locale="ro", stats=stats
    )
    assert not any(c.get("only_filters_step") for c in calls)
    assert stats == {"need_counted": False, "need_pool_full": False}


# --- agentul află câte potriviri există ----------------------------------------------------------


def _ids(n):
    return f"{n:08d}-0000-0000-0000-000000000000"


async def test_the_search_says_when_every_match_is_in_the_list(_catalog):  # noqa: F811
    _catalog["search"] = [
        _p(_ids(1), "Crema Unu", skin="dry"),
        _p(_ids(2), "Crema Doi"),
        _p(_ids(3), "Crema Trei", skin="dry"),
        _p(_ids(4), "Crema Patru"),
    ]
    llm = ScriptedLLM(
        [_call("search_catalog", _search(needs=OILY, **SHELF), "c1")],
        [_call("answer", _ans("Uite."), "c2")],
    )
    await _run(llm)
    out = _tool_output(llm, "c1")
    assert out["need_matches"] == {"in_store": 2, "in_this_list": 2}


async def test_more_matches_than_rows_are_reported(_catalog):  # noqa: F811
    _catalog["search"] = [_p(_ids(i), f"Crema {i}", skin="dry") for i in range(1, 11)]
    llm = ScriptedLLM(
        [_call("search_catalog", _search(needs=OILY, **SHELF), "c1")],
        [_call("answer", _ans("Uite."), "c2")],
    )
    await _run(llm)
    assert _tool_output(llm, "c1")["need_matches"] == {"in_store": 10, "in_this_list": 8}


async def test_excluded_products_are_not_counted_as_remaining(_catalog):  # noqa: F811
    _catalog["search"] = [_p(_ids(i), f"Crema {i}", skin="dry") for i in range(1, 4)]
    llm = ScriptedLLM(
        [_call("search_catalog", _search(needs=OILY, **SHELF), "c1")],
        [_call("search_catalog", _search(needs=OILY, exclude=["P1", "P2"], **SHELF), "c2")],
        [_call("answer", _ans("Uite."), "c3")],
    )
    await _run(llm)
    assert _tool_output(llm, "c2")["need_matches"] == {"in_store": 1, "in_this_list": 1}


async def test_no_count_without_needs_or_subject(_catalog):  # noqa: F811
    llm = ScriptedLLM(
        [_call("search_catalog", _search(needs=OILY), "c1")],
        [_call("answer", _ans("Uite."), "c2")],
    )
    await _run(llm)
    assert "need_matches" not in _tool_output(llm, "c1")


async def test_the_count_is_not_a_fact_for_the_gate(_catalog):  # noqa: F811
    """Ca `found`: un contor al nostru nu întemeiază o cifră din răspuns. Cele 12 potriviri au
    nume fără cifre, deci „12” există doar în `need_matches`."""
    letters = "ABCDEFGHIJKL"
    _catalog["search"] = [
        _p(_ids(i + 1), f"Crema {letters[i]}{letters[i]}", skin="dry") for i in range(12)
    ]
    llm = ScriptedLLM(
        [_call("search_catalog", _search(needs=OILY, **SHELF), "c1")],
        [_call("answer", _ans("Avem 12 creme pentru tine."), "c2")],
        [_call("answer", _ans("Avem mai multe creme pentru tine."), "c3")],
    )
    served, ctx = await _run(llm)
    assert served
    assert _tool_output(llm, "c1")["need_matches"]["in_store"] == 12
    [ev] = [e.properties for e in ctx.events if e.type == "assistant_turn"]
    assert "ungrounded_number" in ev["gate_first"]


# --- plafonul rundelor de căutare ----------------------------------------------------------------


async def test_a_third_search_round_is_refused_but_a_sheet_is_still_read(_catalog, monkeypatch):  # noqa: F811
    monkeypatch.setattr(get_settings(), "assistant_max_search_rounds", 2)
    llm = ScriptedLLM(
        [_call("search_catalog", _search(), "c1")],
        [_call("search_catalog", _search(query="alta"), "c2")],
        [_call("search_catalog", _search(query="a treia"), "c3")],
        [_call("product_details", {"handles": ["P1"]}, "c4")],
        [_call("answer", _ans("Uite."), "c5")],
    )
    served, ctx = await _run(llm)
    assert served
    refused = _tool_output(llm, "c3")
    assert refused["ok"] is False and "search limit" in refused["error"]
    assert "sheets" in _tool_output(llm, "c4")
    [ev] = [e.properties for e in ctx.events if e.type == "assistant_turn"]
    assert ev["search_rounds"] == 3 and ev["search_refused"] == 1
    assert ev["tools"].count("search_catalog") == 2


async def test_several_searches_in_one_round_count_once(_catalog, monkeypatch):  # noqa: F811
    monkeypatch.setattr(get_settings(), "assistant_max_search_rounds", 2)
    llm = ScriptedLLM(
        [
            _call("search_catalog", _search(), "c1"),
            _call("search_catalog", _search(query="alta"), "c2"),
        ],
        [_call("search_catalog", _search(query="a treia"), "c3")],
        [_call("answer", _ans("Uite."), "c4")],
    )
    _served, ctx = await _run(llm)
    assert "products" in _tool_output(llm, "c3")
    [ev] = [e.properties for e in ctx.events if e.type == "assistant_turn"]
    assert ev["search_rounds"] == 2 and ev["search_refused"] == 0


# --- promptul ------------------------------------------------------------------------------------


def test_prompt_v8_searches_again_only_when_more_matches_exist():
    assert aprompt.PROMPT_VERSION >= "assistant.v8"
    text = " ".join(
        aprompt.instructions(
            store="X", locale="ro", families=(), max_shown=6, chip_count=5, search_rounds=2
        ).split()
    )
    assert "if in_this_list equals in_store" in text and "do not search again" in text
    assert "make all the calls in the same round" in text
    assert "A turn has at most 2 rounds of searching" in text
    assert "search again (other words, a wider filter" not in text, "regula veche a plecat"
