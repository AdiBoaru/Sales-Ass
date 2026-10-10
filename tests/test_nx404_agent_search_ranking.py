"""NX-404 — ce produse vede agentul și în ce ordine: nevoia spusă ca prioritate (necunoscutul nu
mai iese), rankingul producției, o familie pe rând cu variantele accesibile, starea nevoii pe fișă,
locul fiecărui card în listă. Zero apeluri de model și zero DB."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from src.assistant import search as asearch
from src.assistant.search import group_families, need_status, rank_candidates, split_needs
from tests.test_nx396_assistant import (  # noqa: F401 — `_catalog` e fixture autouse
    ScriptedLLM,
    _ans,
    _call,
    _catalog,
    _run,
    _search,
)

OILY = {"skin_type": ["oily"]}


class _NoConn:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc):
        return False


def _no_db():
    return _NoConn()


def _p(pid, name, *, skin=None, rating=4.5, reviews=10, availability="in_stock", price=100.0):
    attrs = {"product_type": "crema de fata"}
    if skin is not None:
        attrs["skin_type"] = skin
    return {
        "id": pid,
        "name": name,
        "price": price,
        "url": f"https://shop.test/{pid}",
        "availability": availability,
        "rating": rating,
        "review_count": reviews,
        "attributes": attrs,
    }


# --- starea nevoii -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("attrs", "needs", "status"),
    [
        ({"skin_type": "oily"}, OILY, "match"),
        ({"skin_type": ["oily", "combination"]}, OILY, "match"),
        ({}, OILY, "unknown"),
        ({"skin_type": "dry"}, OILY, "mismatch"),
        ({"skin_type": "oily"}, {}, "n/a"),
        # o dimensiune lipsă, cealaltă potrivită ⇒ necunoscut; una contrazisă ⇒ nepotrivit
        ({"skin_type": "oily"}, {"skin_type": ["oily"], "concerns": ["acne"]}, "unknown"),
        ({"concerns": ["redness"]}, {"skin_type": ["oily"], "concerns": ["acne"]}, "mismatch"),
    ],
)
def test_need_status(attrs, needs, status):
    assert need_status({"attributes": attrs}, needs) == status


def test_a_boolean_compares_like_sql():
    """Recenzia NX-404: `str(True)` e „True”, SQL-ul dă „true”: produsul ieșea nepotrivit."""
    assert need_status({"attributes": {"fragrance_free": True}}, {"fragrance_free": ["true"]}) == (
        "match"
    )
    assert need_status({"attributes": {"spf": 50.0}}, {"spf": ["50"]}) == "match"


def test_split_needs():
    assert split_needs(["skin_type:oily", "concerns:acne", "concerns:pores", "x"]) == {
        "skin_type": ["oily"],
        "concerns": ["acne", "pores"],
    }


# --- ordinea -------------------------------------------------------------------------------------


def test_a_match_comes_before_an_unknown_and_a_mismatch_leaves():
    """Pe replay, o pondere a nevoii în scor punea necunoscutele peste potriviri (182 → 147)."""
    rows = [
        _p("u", "Crema U", rating=5.0, reviews=500),  # primul la text, cel mai bine cotat
        _p("x", "Crema X", skin="dry", rating=5.0, reviews=500),
        _p("m", "Crema M", skin="oily", rating=4.0, reviews=3),
    ]
    ranked = rank_candidates(rows, OILY, weights={})
    assert [r["id"] for r in ranked] == ["m", "u"]


def test_within_a_group_the_production_ranking_decides():
    """Același scor de text (poziții vecine): produsul epuizat coboară, cel bine cotat urcă."""
    rows = [
        _p("a", "Crema A", skin="oily", availability="out_of_stock", rating=5.0, reviews=200),
        _p("b", "Crema B", skin="oily", rating=4.9, reviews=150),
    ]
    assert [r["id"] for r in rank_candidates(rows, OILY, weights={})] == ["b", "a"]
    assert [r["id"] for r in rank_candidates(rows, OILY, weights=None)] == ["b", "a"]


def test_every_in_stock_product_comes_before_an_out_of_stock_one():
    """Turul 1 din `2789a469`: epuizatul venea primul la text, cel în stoc din completare, iar
    penalizarea din ranking (cinci locuri) nu ajungea; agentul l-a pomenit nechemat."""
    rows = [_p("oos", "Crema Epuizata", skin="oily", availability="out_of_stock")]
    rows += [_p(f"x{i}", f"Crema Umplutura {i}") for i in range(20)]
    rows.append(_p("ok", "Crema In Stoc", skin="oily"))
    ranked = rank_candidates(rows, OILY, weights={})
    assert ranked[0]["id"] == "ok" and ranked[-1]["id"] == "oos"


def test_an_explicit_sort_keeps_the_sql_order_inside_each_group():
    rows = [
        _p("u1", "Crema U1", price=10),
        _p("m1", "Crema M1", skin="oily", price=20),
        _p("m2", "Crema M2", skin="oily", price=30),
    ]
    ranked = rank_candidates(rows, OILY, sort="price_asc", weights={})
    assert [r["id"] for r in ranked] == ["m1", "m2", "u1"]


def test_an_explicit_sort_orders_inside_each_group_across_queries():
    """Candidații vin din interogări diferite: concatenarea lor nu e în ordinea prețului."""
    rows = [
        _p("t1", "Text 1", skin="oily", price=80),
        _p("t2", "Text 2", price=90),
        _p("f1", "Raft 1", skin="oily", price=4),
        _p("f2", "Raft 2", price=5),
    ]
    ranked = rank_candidates(rows, OILY, sort="price_asc", weights={})
    assert [r["id"] for r in ranked] == ["f1", "t1", "f2", "t2"]


def test_families_keep_their_versions_and_the_limit_counts_families():
    rows = [
        _p("a1", "Cushion Glow - nuanta 10C, 15 g"),
        _p("b", "Crema B"),
        _p("a2", "Cushion Glow - nuanta 13C, 15 g"),
        _p("c", "Crema C"),
        _p("a3", "Cushion Glow - nuanta 17C, 15 g"),
    ]
    groups = group_families(rows, limit=2)
    assert [(g[0]["id"], [v["id"] for v in g[1]]) for g in groups] == [
        ("a1", ["a2", "a3"]),
        ("b", []),
    ]


# --- cele trei interogări ------------------------------------------------------------------------


def _recording(monkeypatch):
    calls = []

    async def fake(conn, business_id, query, **kw):
        calls.append(kw)
        n = len(calls)
        return [_p(f"r{n}", f"Crema {n}"), _p("shared", "Crema S")]

    import src.db.queries.catalog as cat

    monkeypatch.setattr(cat, "search_products_lexical", fake)
    return calls


ARGS = {"query": "crema", "category": "ten", "product_types": ["crema de fata"]}


async def test_matches_and_shelf_run_the_text_ladder_then_the_shelf_completes(monkeypatch):
    """Raftul fără nevoi rulează scara de text OBIȘNUITĂ (eticheta de treaptă rămâne adevărată și
    nu depinde de flagul `filters_only`); completarea din raft vine doar sub 20 de candidați."""
    calls = _recording(monkeypatch)
    rows = await asearch.fetch_candidates(
        _no_db, "b", {**ARGS, "needs": ["skin_type:oily"]}, locale="ro"
    )
    assert [r["id"] for r in rows] == ["r1", "shared", "r2", "r3"]
    assert calls[0]["facet_filters"] == {"product_type": ["crema de fata"], "skin_type": ["oily"]}
    assert calls[1]["facet_filters"] == {"product_type": ["crema de fata"]}
    assert [c.get("only_filters_step", False) for c in calls] == [False, False, True]
    assert all(c["allow_filters_only"] for c in calls)


async def test_enough_candidates_need_no_completion(monkeypatch):
    calls = []

    async def fake(conn, business_id, query, **kw):
        calls.append(kw)
        return [_p(f"{len(calls)}-{i}", f"Crema {len(calls)}-{i}") for i in range(20)]

    import src.db.queries.catalog as cat

    monkeypatch.setattr(cat, "search_products_lexical", fake)
    await asearch.fetch_candidates(_no_db, "b", ARGS, locale="ro")
    assert len(calls) == 1


async def test_a_single_connection_provider_runs_the_queries_one_by_one(monkeypatch):
    active = []

    async def fake(conn, business_id, query, **kw):
        import asyncio

        active.append(1)
        assert len(active) == 1, "două operații simultane pe aceeași conexiune"
        await asyncio.sleep(0)
        active.pop()
        return []

    import src.db.queries.catalog as cat

    monkeypatch.setattr(cat, "search_products_lexical", fake)
    await asearch.fetch_candidates(
        _no_db, "b", {**ARGS, "needs": ["skin_type:oily"]}, locale="ro", parallel=False
    )


async def test_on_an_explicit_sort_a_short_list_is_completed_from_the_shelf(monkeypatch):
    calls = _recording(monkeypatch)
    rows = await asearch.fetch_candidates(
        _no_db, "b", {**ARGS, "needs": ["skin_type:oily"], "sort": "price_asc"}, locale="ro"
    )
    assert [r["id"] for r in rows] == ["r1", "shared", "r2", "r3"]
    assert [c.get("only_filters_step", False) for c in calls] == [False, False, True]


async def test_without_needs_or_a_subject_there_is_no_extra_query(monkeypatch):
    calls = []

    async def fake(conn, business_id, query, **kw):
        calls.append(kw)
        return []

    import src.db.queries.catalog as cat

    monkeypatch.setattr(cat, "search_products_lexical", fake)
    await asearch.fetch_candidates(_no_db, "b", {"query": "ceva"}, locale="ro")
    assert len(calls) == 1


# --- prin agent ----------------------------------------------------------------------------------


def _tool_output(llm, call_id):
    out = [i for i in llm.inputs[-1] if isinstance(i, dict) and i.get("call_id") == call_id][-1]
    return json.loads(out["output"])


async def test_the_agent_sees_matches_first_unknowns_marked_and_no_mismatch(_catalog):  # noqa: F811
    _catalog["search"] = [
        _p("11111111-1111-1111-1111-111111111111", "Crema Necunoscuta"),
        _p("22222222-2222-2222-2222-222222222222", "Crema Uscata", skin="dry"),
        _p("33333333-3333-3333-3333-333333333333", "Crema Grasa", skin="oily"),
    ]
    llm = ScriptedLLM(
        [_call("search_catalog", _search(needs=["skin_type:dry"]), "c1")],
        [_call("answer", _ans("Uite."), "c2")],
    )
    await _run(llm)
    products = _tool_output(llm, "c1")["products"]
    assert [(p["name"], p.get("need")) for p in products] == [
        ("Crema Uscata", "match"),
        ("Crema Necunoscuta", "unknown"),
    ]


async def test_versions_get_handles_and_can_be_compared(_catalog):  # noqa: F811
    from tests.test_nx396_assistant import SHADES

    _catalog["search"] = list(SHADES)
    llm = ScriptedLLM(
        [_call("search_catalog", _search(query="cushion"), "c1")],
        [_call("answer", _ans("Uite."), "c2")],
    )
    await _run(llm)
    [row] = _tool_output(llm, "c1")["products"]
    assert row["handle"] == "P1"
    assert row["versions"] == [{"handle": "P2", "name": "Cushion Glow (13C)"}]


async def test_the_sheet_carries_the_need_and_the_place_in_the_list(_catalog, monkeypatch):  # noqa: F811
    _catalog["search"] = [_p("44444444-4444-4444-4444-444444444444", "Crema Fara Tip")]
    import tests.test_nx396_assistant as base

    monkeypatch.setitem(base.CATALOG, _catalog["search"][0]["id"], _catalog["search"][0])
    llm = ScriptedLLM(
        [_call("search_catalog", _search(needs=["skin_type:dry"]), "c1")],
        [_call("product_details", {"handles": ["P1"]}, "c2")],
        [_call("answer", _ans("Uite."), "c3")],
    )
    await _run(llm)
    sheet = _tool_output(llm, "c2")["sheets"]["P1"]
    assert "need: unknown" in sheet and "position in this turn's list: 1 of 1" in sheet


async def test_each_card_reports_its_place_in_the_list(_catalog):  # noqa: F811
    _catalog["search"] = [
        _p("55555555-5555-5555-5555-555555555555", "Crema Unu", skin="dry"),
        _p("66666666-6666-6666-6666-666666666666", "Crema Doi"),
    ]
    llm = ScriptedLLM(
        [_call("search_catalog", _search(needs=["skin_type:dry"]), "c1")],
        [_call("answer", _ans("Uite.", ["P2", "P1"]), "c2")],
    )
    _served, ctx = await _run(llm)
    [ev] = [e.properties for e in ctx.events if e.type == "assistant_turn"]
    assert ev["card_positions"] == [2, 1]
    assert ev["card_needs"] == ["unknown", "match"]
    assert ev["candidates"] == 2


# --- pâlnia e mecanică ---------------------------------------------------------------------------


def _returns_products(fn: ast.AST) -> bool:
    return any(
        isinstance(node, ast.Dict)
        and any(isinstance(k, ast.Constant) and k.value == "products" for k in node.keys)
        for node in ast.walk(fn)
    )


def _calls_present(fn: ast.AST) -> bool:
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "present"
        for node in ast.walk(fn)
    )


def test_every_tool_that_returns_products_goes_through_the_funnel():
    """O unealtă nouă care întoarce o listă `products` fără `present` ar ocoli rankingul, starea
    nevoii și siguranța: pică aici, nu la client."""
    tree = ast.parse(Path("src/assistant/tools.py").read_text(encoding="utf-8"))
    [tools] = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Tools"]
    methods = [n for n in tools.body if isinstance(n, ast.AsyncFunctionDef | ast.FunctionDef)]
    offenders = [m.name for m in methods if _returns_products(m) and not _calls_present(m)]
    assert offenders == []
    # exemplul care TREBUIE să pice
    bad = ast.parse("async def _x(self):\n    return {'products': []}\n").body[0]
    assert _returns_products(bad) and not _calls_present(bad)


# --- recenzia adversarială: pâlnia ---------------------------------------------------------------


def _shades(family, n, start):
    return [
        _p(f"{start + i:08d}-0000-0000-0000-000000000000", f"{family} - nuanta {i}, 10 ml")
        for i in range(n)
    ]


async def test_shade_heavy_families_do_not_starve_the_list(_catalog):  # noqa: F811
    """Siguranța tăia la primele 32 de rânduri, înaintea familiilor: trei familii cu câte 15
    nuanțe umpleau tăietura și agentul vedea 3 produse."""
    rows = _shades("Ruj A", 15, 1000) + _shades("Ruj B", 15, 2000) + _shades("Ruj C", 15, 3000)
    rows += [_p(f"{9000 + i:08d}-0000-0000-0000-000000000000", f"Crema {i}") for i in range(6)]
    _catalog["search"] = rows
    llm = ScriptedLLM(
        [_call("search_catalog", _search(), "c1")], [_call("answer", _ans("Uite."), "c2")]
    )
    await _run(llm)
    assert len(_tool_output(llm, "c1")["products"]) == 8


async def test_versions_never_push_the_best_families_out_of_memory(_catalog):  # noqa: F811
    rows = []
    for f in range(8):
        rows += _shades(f"Familia {f}", 7, 1000 * (f + 1))
    _catalog["search"] = rows
    llm = ScriptedLLM(
        [_call("search_catalog", _search(), "c1")], [_call("answer", _ans("Uite."), "c2")]
    )
    _served, ctx = await _run(llm)
    reps = {p["handle"] for p in _tool_output(llm, "c1")["products"]}
    assert len(reps) == 8 and reps <= set(ctx.state_patch["assistant"]["h"])


async def test_a_second_list_replaces_the_places_of_the_first(_catalog):  # noqa: F811
    _catalog["search"] = [
        _p("77777777-7777-7777-7777-777777777777", "Crema Sapte", skin="dry"),
        _p("88888888-8888-8888-8888-888888888888", "Crema Opt"),
    ]
    llm = ScriptedLLM(
        [_call("search_catalog", _search(needs=["skin_type:dry"]), "c1")],
        [_call("search_catalog", _search(), "c2")],
        [_call("answer", _ans("Uite.", ["P1"]), "c3")],
    )
    _served, ctx = await _run(llm)
    [ev] = [e.properties for e in ctx.events if e.type == "assistant_turn"]
    assert ev["card_needs"] == ["n/a"] and ev["candidates"] == 2


async def test_a_card_from_an_earlier_search_keeps_its_place(_catalog, monkeypatch):  # noqa: F811
    """NX-406: pe smoke-ul de după deploy agentul a căutat de 3 ori, iar cardurile alese din
    căutările anterioare ieșeau fără loc (`card_positions` [None, None, None])."""
    import src.db.queries.catalog as cat
    import tests.test_nx396_assistant as base

    first = [
        _p("aaaaaaaa-0000-0000-0000-000000000001", "Crema Intai", skin="dry"),
        _p("aaaaaaaa-0000-0000-0000-000000000002", "Crema Doi"),
    ]
    second = [_p("bbbbbbbb-0000-0000-0000-000000000001", "Ser Altul")]
    for r in first + second:
        monkeypatch.setitem(base.CATALOG, r["id"], r)

    async def by_search(conn, business_id, query, **kw):
        return [dict(r) for r in (first if query == "crema" else second)]

    monkeypatch.setattr(cat, "search_products_lexical", by_search)
    llm = ScriptedLLM(
        [_call("search_catalog", _search(needs=["skin_type:dry"]), "c1")],
        [_call("search_catalog", _search(query="ser"), "c2")],
        [_call("product_details", {"handles": ["P2"]}, "c3")],
        [_call("answer", _ans("Uite.", ["P2", "P3"]), "c4")],
    )
    _served, ctx = await _run(llm)
    [ev] = [e.properties for e in ctx.events if e.type == "assistant_turn"]
    assert ev["card_positions"] == [2, 1]
    assert ev["card_needs"] == ["unknown", "n/a"]
    sheet = _tool_output(llm, "c3")["sheets"]["P2"]
    assert "need: unknown" in sheet and "position in this turn's list: 2 of 2" in sheet
