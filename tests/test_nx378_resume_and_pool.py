"""NX-378 — după o reluare, «mai arată-mi altele» arată ALTELE din subiectul reluat.

Rularea pe producție din 2026-10-01 (`KERNEL-LIVE-2026-10-01.md`, k1 T4): «ok, înapoi la seruri,
mai arată-mi altele» a primit zero carduri. Trei cauze, măsurate prin replay pe turul real:
1. căutarea planificată excludea ecranul din vederea v1 (șampoanele), nu ecranul subiectului
   reluat (serurile, puse înapoi de reducer), deci aducea aceleași seruri, iar compunerea le refuza;
2. pool-ul paginării era tăiat la pagină: rândurile completării NX-298 care nu încăpeau se
   aruncau;
3. după cele câteva seruri pentru ten gras, completarea NX-298 punea produse din setul filtrelor
   fără text (cushion, creme cu SPF), deși existau seruri pentru pete cu tip de ten necunoscut
   (NX-377, acum ÎNAINTEA lui NX-298).
Pe codul ramurii, turul real aduce cinci seruri noi pentru pete. Zero DB în teste.
"""

from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from src.agent import kernel_executors as kx
from src.agent.tool_executor import ToolRun
from src.config import get_settings
from src.conversation.state_v2 import ConversationStateV2, DisplayedRef, References
from src.tools import catalog_tools as ct
from src.tools.base import run_tool
from src.worker.runner import PipelineDeps
from tests.test_nx377_unknown_facet_fill import (
    MATCHES,
    UNKNOWN,
    _ctx,
    _row,
    _sole_pack,
)


def _refs(shown: list[str], earlier: list[list[str]]) -> References:
    return References(
        displayed_products=tuple(DisplayedRef(product_id=i, name=i, price=1.0) for i in shown),
        recent_sets=tuple(
            tuple(DisplayedRef(product_id=i, name=i, price=1.0) for i in s) for s in earlier
        ),
    )


# --- 1. ce a văzut clientul, după starea porții ---------------------------------------------------


def test_the_seen_set_is_the_gate_screen_plus_the_earlier_sets():
    gate = ConversationStateV2(references=_refs(["s1", "s2"], [["a", "s1"], ["b"]]))
    ctx = NS(kernel_view=NS(gate_state=gate))
    assert kx._seen_by_subject(ctx) == ("s1", "s2", "a", "b")


def test_without_a_kernel_view_nothing_is_added():
    assert kx._seen_by_subject(NS(kernel_view=None)) == ()


async def test_the_seen_set_reaches_the_planned_search(monkeypatch):
    seen: dict = {}

    async def fake(ctx, deps, args, **kw):
        seen.update(kw)
        return NS(products=[], llm_view="", ok=True, state_patch={}, error=None)

    monkeypatch.setattr(ct, "run_planned_search", fake)
    run = ToolRun(NS(), NS())
    monkeypatch.setattr(run, "_absorb_planned", lambda *a, **k: None)
    await run.execute_planned(object(), exclude_shown=True, seen_extra=("s1", "s2"))
    assert seen == {"exclude_shown": True, "seen_extra": ("s1", "s2")}
    seen.clear()
    await run.execute_planned(object())
    assert seen == {}  # fără cerere, apelul de azi, byte-identic


# --- 2 + 3. pool-ul paginării și precedența completărilor (unealta reală, căutare falsă) ----------


OFF_TEXT = [_row(f"f{i}", f"Cushion {i}") for i in range(20)]


@pytest.fixture
def lexical(monkeypatch):
    calls: list[dict] = []
    state = {"matches": list(MATCHES), "unknown": list(UNKNOWN)}

    async def fake_lex(conn, business_id, **k):
        calls.append(k)
        if k.get("missing_facets"):
            return [dict(r) for r in state["unknown"]]
        if k.get("only_filters_step"):
            return [{**r, "lexical_step": "filters_only"} for r in OFF_TEXT]
        return [dict(r) for r in state["matches"]]

    async def no_emb(conn, business_id):
        return False

    async def vocab(deps, business_id):
        from tests.test_nx377_unknown_facet_fill import VOCAB

        return VOCAB

    monkeypatch.setattr(ct, "has_embeddings", no_emb)
    monkeypatch.setattr(ct, "search_products_lexical", fake_lex)
    monkeypatch.setattr(ct, "get_vocabulary", vocab)
    return {"calls": calls, "state": state}


async def _search(ctx, **args):
    deps = PipelineDeps(conn=object(), redis=None, llm=None)
    base = {"query": "ser", "concerns": ["oily"], "features": ["hyperpigmentation"]}
    return await run_tool(ctx, deps, "search_products", {**base, **args})


async def test_text_matches_with_an_unknown_facet_come_before_off_text_rows(lexical):
    res = await _search(_ctx(_sole_pack()))
    ids = [p["id"] for p in res.products]
    assert ids == ["m0", "m1", "m2", "u0", "u1", "u2"]
    assert not any(c.get("only_filters_step") for c in lexical["calls"])  # pagina e plină


async def test_the_rest_of_the_filter_fill_feeds_the_paging_pool(lexical):
    """Fără fațetă care califică (`concerns` e aditivă): NX-298 umple pagina, iar restul rândurilor
    deja aduse intră în pool, nu se mai aruncă."""
    ctx = _ctx(_sole_pack())
    res = await _search(ctx, concerns=None)
    page = [p["id"] for p in res.products]
    assert page == ["m0", "m1", "m2", "f0", "f1", "f2"]
    pool = ctx.state_patch["active_search"]["pool"]
    assert len(pool) > len(page) and {"f3", "f4", "f5"} <= set(pool)


async def test_the_pool_kill_switch_restores_the_page_sized_pool(lexical, monkeypatch):
    monkeypatch.setattr(get_settings(), "search_pool_from_filter_fill_enabled", False)
    ctx = _ctx(_sole_pack())
    await _search(ctx, concerns=None)
    assert ctx.state_patch["active_search"]["pool"] == ["m0", "m1", "m2", "f0", "f1", "f2"]


async def test_the_planned_first_page_skips_what_the_gate_says_was_seen(lexical):
    """k1 T4: ecranul v1 e al șampoanelor; starea porții știe serurile văzute."""
    ctx = _ctx(_sole_pack())
    args = ct.SearchArgs(query="ser", concerns=["oily"], features=["hyperpigmentation"])
    res = await ct.run_planned_search(
        ctx,
        PipelineDeps(conn=object(), redis=None, llm=None),
        args,
        exclude_shown=True,
        seen_extra=("m0", "m1"),
    )
    ids = [p["id"] for p in res.products]
    assert "m0" not in ids and "m1" not in ids and "m2" in ids


# --- recenzia adversarială ----------------------------------------------------------------------


async def test_overlapping_tails_do_not_duplicate_pool_ids(lexical, monkeypatch):
    """Coada NX-303 și restul completării NX-298 vin din aceeași interogare: niciun id dublat."""

    async def same_tail(conn, ctx, a, step, *, searchable_facets):
        return [{**r, "lexical_step": "filters_only"} for r in OFF_TEXT]

    monkeypatch.setattr(ct, "_subject_filter_tail", same_tail)
    monkeypatch.setattr(ct, "_text_gate_is_redundant", lambda *a, **k: True)
    ctx = _ctx(_sole_pack())
    await _search(ctx, concerns=None)
    pool = ctx.state_patch["active_search"]["pool"]
    assert len(pool) == len(set(pool))


async def test_the_tail_sources_are_counted_apart(lexical):
    ctx = _ctx(_sole_pack())
    await _search(ctx, concerns=None)
    nx303 = next(e for e in ctx.events if e.type == "pool_extended_from_filter")
    assert nx303.properties["added"] == 0  # metrica NX-303 nu primește alte surse
    other = next(e for e in ctx.events if e.type == "pool_extended")
    assert other.properties["filter_fill"] > 0


async def test_earlier_screens_leave_the_session_pool_too(lexical):
    """Paginarea compară doar cu ecranul de acum; ce s-a văzut pe subiect nu mai revine."""
    ctx = _ctx(_sole_pack())
    args = ct.SearchArgs(query="ser", concerns=["oily"], features=["hyperpigmentation"])
    await ct.run_planned_search(
        ctx,
        PipelineDeps(conn=object(), redis=None, llm=None),
        args,
        exclude_shown=True,
        seen_extra=("m0", "m1", "u3"),
    )
    pool = ctx.state_patch["active_search"]["pool"]
    assert not {"m0", "m1", "u3"} & set(pool)


async def test_everything_already_seen_is_nothing_new_not_nothing_found(lexical):
    lexical["state"]["matches"] = [MATCHES[0]]
    lexical["state"]["unknown"] = []
    ctx = _ctx(_sole_pack())
    args = ct.SearchArgs(query="ser", concerns=["oily"], features=["hyperpigmentation"])
    res = await ct.run_planned_search(
        ctx,
        PipelineDeps(conn=object(), redis=None, llm=None),
        args,
        exclude_shown=True,
        seen_extra=["m0", *(f"f{i}" for i in range(20))],
    )
    assert not res.products and res.llm_view == ct._NO_MORE_VIEW
