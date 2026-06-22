"""NX-119a — sesiuni de căutare: pool stocat + paginare deterministă + unseen-dedup.

Fără DB/LLM real: retrieverul lexical + `get_products_by_ids` sunt monkeypatch-uite. Testăm
forma `active_search` (pool/cursor/fp), paginarea fără suprapunere, epuizarea (P6), rafinarea
(fp nou → sesiune nouă), cap-ul de pool și hidratarea defensivă din `from_jsonb`.
"""

import json

from src.models import (
    MAX_SEARCH_POOL,
    BusinessConfig,
    Contact,
    ConversationState,
    InboundMessage,
    ProductRef,
    TurnContext,
)
from src.tools import catalog_tools as ct
from src.tools.base import run_tool
from src.worker.runner import PipelineDeps


def _prod(i: int) -> dict:
    return {
        "id": f"p{i}",
        "name": f"P{i}",
        "brand": "B",
        "price": 10.0 + i,
        "url": f"u{i}",
        "ai_summary": "",
        "availability": "in_stock",
        "rating": 4.0,
    }


def _ctx() -> TurnContext:
    return TurnContext(
        turn_id="t",
        business=BusinessConfig(id="biz-1", slug="s", name="n"),
        contact=Contact(id="c", business_id="biz-1"),
        message=InboundMessage(provider_msg_id="m", body="x"),
        conversation_id="conv",
    )


def _deps() -> PipelineDeps:
    return PipelineDeps(conn=object(), redis=None, llm=None)  # llm=None → cale lexical-only


def _patch(monkeypatch, products: list[dict]) -> None:
    by_id = {p["id"]: p for p in products}

    async def fake_lex(conn, business_id, **k):
        return list(products)

    async def fake_by_ids(conn, business_id, ids, **k):
        return [by_id[i] for i in ids if i in by_id]  # păstrează ordinea cerută

    monkeypatch.setattr(ct, "search_products_lexical", fake_lex)
    monkeypatch.setattr(ct, "get_products_by_ids", fake_by_ids)


def _session_event(ctx):
    return next(e for e in reversed(ctx.events) if e.type == "search_session")


def _continue_ctx(prev_ctx, prev_products) -> TurnContext:
    """Simulează processor-ul între tururi: persistă `active_search` + `displayed_products`."""
    ctx = _ctx()
    ctx.state.active_search = prev_ctx.state_patch.get("active_search")
    ctx.state.displayed_products = [
        ProductRef(p["id"], p["name"], p["price"]) for p in prev_products
    ]
    return ctx


# --- Happy ----------------------------------------------------------------------


async def test_new_session_seeds_pool_first_page(monkeypatch):
    _patch(monkeypatch, [_prod(i) for i in range(10)])
    ctx = _ctx()
    res = await run_tool(ctx, _deps(), "search_products", {"query": "creme"})
    assert [p["id"] for p in res.products] == [f"p{i}" for i in range(6)]
    sess = ctx.state_patch["active_search"]
    assert sess["pool"] == [f"p{i}" for i in range(10)]  # ≤ MAX_SEARCH_POOL
    assert sess["cursor"] == 6 and sess["fp"]
    ev = _session_event(ctx)
    assert ev.properties["action"] == "new" and ev.properties["pool_size"] == 10


async def test_show_more_next_page_no_overlap(monkeypatch):
    _patch(monkeypatch, [_prod(i) for i in range(14)])
    ctx1 = _ctx()
    res1 = await run_tool(ctx1, _deps(), "search_products", {"query": "creme"})
    ctx2 = _continue_ctx(ctx1, res1.products)
    res2 = await run_tool(ctx2, _deps(), "search_products", {"query": "creme"})
    page1 = [p["id"] for p in res1.products]
    page2 = [p["id"] for p in res2.products]
    assert page1 == [f"p{i}" for i in range(6)]
    assert page2 == [f"p{i}" for i in range(6, 12)]
    assert not set(page1) & set(page2)  # ZERO suprapunere
    assert ctx2.state_patch["active_search"]["cursor"] == 12
    assert _session_event(ctx2).properties["action"] == "page"


async def test_three_pages_all_unique(monkeypatch):
    _patch(monkeypatch, [_prod(i) for i in range(14)])
    seen_ids: list[str] = []
    ctx = _ctx()
    prev = None
    for _ in range(3):
        if prev is not None:
            ctx = _continue_ctx(prev[0], prev[1])
        res = await run_tool(ctx, _deps(), "search_products", {"query": "creme"})
        seen_ids += [p["id"] for p in res.products]
        prev = (ctx, res.products)
    assert len(seen_ids) == 14 and len(set(seen_ids)) == 14  # 6+6+2, toate unice


# --- Edge -----------------------------------------------------------------------


async def test_pool_exactly_six_then_exhausted(monkeypatch):
    _patch(monkeypatch, [_prod(i) for i in range(6)])
    ctx1 = _ctx()
    res1 = await run_tool(ctx1, _deps(), "search_products", {"query": "creme"})
    assert len(res1.products) == 6
    ctx2 = _continue_ctx(ctx1, res1.products)
    res2 = await run_tool(ctx2, _deps(), "search_products", {"query": "creme"})
    assert res2.products == [] and "epuizat" in res2.llm_view.lower()
    assert _session_event(ctx2).properties["action"] == "exhausted"


async def test_refined_filters_start_new_session(monkeypatch):
    _patch(monkeypatch, [_prod(i) for i in range(10)])
    ctx1 = _ctx()
    res1 = await run_tool(ctx1, _deps(), "search_products", {"query": "creme"})
    fp1 = ctx1.state_patch["active_search"]["fp"]
    # filtru rafinat (price_max nou) → fp DIFERIT → SESIUNE NOUĂ (action=new), NU paginare din pool
    ctx2 = _continue_ctx(ctx1, res1.products)
    res2 = await run_tool(ctx2, _deps(), "search_products", {"query": "creme", "price_max": 50})
    sess2 = ctx2.state_patch["active_search"]
    assert sess2["fp"] != fp1  # filtru rafinat → fp nou
    assert _session_event(ctx2).properties["action"] == "new"  # sesiune nouă, nu „page"
    # pool-ul rafinat e COMPLET (review #1): produsele deja afișate rămân în pool (nu starved),
    # pot resurfa la continuare; doar prima pagină le sare prin unseen-dedup.
    assert sess2["pool"] == [f"p{i}" for i in range(10)]
    assert "p0" not in [p["id"] for p in res2.products]  # prima pagină sare ce s-a arătat deja


async def test_continuation_skips_seen_from_displayed(monkeypatch):
    _patch(monkeypatch, [_prod(i) for i in range(8)])
    ctx1 = _ctx()
    res1 = await run_tool(ctx1, _deps(), "search_products", {"query": "creme"})  # p0..p5, cursor 6
    ctx2 = _continue_ctx(ctx1, res1.products)
    # p6 a fost între timp afișat (alt drum) → trebuie sărit pe pagina 2
    ctx2.state.displayed_products += [ProductRef("p6", "P6", 16.0)]
    res2 = await run_tool(ctx2, _deps(), "search_products", {"query": "creme"})
    assert [p["id"] for p in res2.products] == ["p7"]  # p6 (văzut) sărit, doar p7 rămâne


async def test_pool_capped_at_max(monkeypatch):
    _patch(monkeypatch, [_prod(i) for i in range(40)])  # > MAX_SEARCH_POOL
    ctx = _ctx()
    await run_tool(ctx, _deps(), "search_products", {"query": "creme"})
    assert len(ctx.state_patch["active_search"]["pool"]) == MAX_SEARCH_POOL


async def test_no_session_when_zero_results(monkeypatch):
    _patch(monkeypatch, [])  # niciun produs → nicio sesiune de paginat
    ctx = _ctx()
    res = await run_tool(ctx, _deps(), "search_products", {"query": "zzz"})
    assert res.products == [] and "active_search" not in ctx.state_patch


# --- regresii din review-ul adversarial NX-119a ---------------------------------


async def test_exhausted_advances_cursor_when_remaining_all_seen(monkeypatch):
    """Review #3: pagina iese goală fiindcă restul pool-ului e deja văzut → cursorul TOT avansează
    la len(pool), ca un tur viitor să NU re-servească coada."""
    _patch(monkeypatch, [_prod(i) for i in range(8)])
    ctx1 = _ctx()
    res1 = await run_tool(ctx1, _deps(), "search_products", {"query": "creme"})  # p0-5, cursor 6
    ctx2 = _continue_ctx(ctx1, res1.products)
    # restul pool-ului (p6,p7) marcat ca văzut → pagina goală, dar cursorul trece la 8
    ctx2.state.displayed_products += [ProductRef("p6", "P6", 16.0), ProductRef("p7", "P7", 17.0)]
    res2 = await run_tool(ctx2, _deps(), "search_products", {"query": "creme"})
    assert res2.products == []
    assert ctx2.state_patch["active_search"]["cursor"] == 8  # avansat, nu rămas la 6


async def test_continuation_all_dead_returns_exhaustion(monkeypatch):
    """Review #7: id-urile paginii au devenit inactive între tururi (hidratare → []) → semnal de
    epuizare determinist, NU bare „Niciun produs găsit"."""
    _patch(monkeypatch, [_prod(i) for i in range(12)])
    ctx1 = _ctx()
    res1 = await run_tool(ctx1, _deps(), "search_products", {"query": "creme"})
    ctx2 = _continue_ctx(ctx1, res1.products)

    async def dead(conn, business_id, ids, **k):  # produse dezactivate → hidratare goală
        return []

    monkeypatch.setattr(ct, "get_products_by_ids", dead)
    res2 = await run_tool(ctx2, _deps(), "search_products", {"query": "creme"})
    assert res2.products == [] and "epuizat" in res2.llm_view.lower()  # _NO_MORE_VIEW
    assert _session_event(ctx2).properties["action"] == "exhausted"


async def test_page_index_monotonic_when_limit_varies(monkeypatch):
    """Review #5/6: page_index e contor REAL din state (nu cursor//limit) → monoton chiar dacă
    `limit` variază între tururi."""
    _patch(monkeypatch, [_prod(i) for i in range(20)])
    ctx1 = _ctx()
    res1 = await run_tool(ctx1, _deps(), "search_products", {"query": "creme", "limit": 6})
    assert _session_event(ctx1).properties["page_index"] == 0
    ctx2 = _continue_ctx(ctx1, res1.products)
    res2 = await run_tool(ctx2, _deps(), "search_products", {"query": "creme", "limit": 3})
    assert _session_event(ctx2).properties["page_index"] == 1  # contor real, nu 6//3=2
    ctx3 = _continue_ctx(ctx2, res2.products)
    await run_tool(ctx3, _deps(), "search_products", {"query": "creme", "limit": 6})
    assert _session_event(ctx3).properties["page_index"] == 2  # monoton


# --- from_jsonb defensiv + buget state ------------------------------------------


def test_from_jsonb_active_search_defensive():
    # parțial (fără pool) → None (tratat ca sesiune nouă, nu crapă)
    assert ConversationState.from_jsonb({"active_search": {"fp": "x"}}).active_search is None
    # pool ne-listă → None
    assert ConversationState.from_jsonb({"active_search": {"pool": "nope"}}).active_search is None
    # lipsă cheie → None
    assert ConversationState.from_jsonb({}).active_search is None
    # valid → hidratat, pool capat, cursor int
    s = ConversationState.from_jsonb(
        {
            "active_search": {
                "pool": [f"p{i}" for i in range(40)],
                "cursor": "6",
                "fp": "abc",
                "filters": {"query": "x"},
            }
        }
    )
    assert len(s.active_search["pool"]) == MAX_SEARCH_POOL
    assert s.active_search["cursor"] == 6 and s.active_search["fp"] == "abc"


def test_from_jsonb_non_int_cursor_does_not_crash():
    # Review #2: cursor/page ne-numeric (drift/edit) → 0, NU ValueError (P6: nu blochează turul)
    s = ConversationState.from_jsonb(
        {"active_search": {"pool": ["p1"], "cursor": "NaN", "fp": "z"}}
    )
    assert s.active_search["cursor"] == 0
    s2 = ConversationState.from_jsonb({"active_search": {"pool": ["p1"], "cursor": [1, 2]}})
    assert s2.active_search["cursor"] == 0 and s2.active_search["page"] == 0


def test_active_search_under_8kb_budget():
    pool = ["123e4567-e89b-12d3-a456-426614174000"] * MAX_SEARCH_POOL
    sess = {
        "filters": {
            "query": "x" * 120,
            "category": "creme-fata",
            "concerns": ["oily", "sensitive", "dry"],
            "price_max": 80,
            "brand": None,
            "sort_mode": "relevance",
            "in_stock_only": False,
        },
        "pool": pool,
        "cursor": 6,
        "fp": "abcdef0123456789",
    }
    assert len(json.dumps({"active_search": sess}).encode()) < 2000  # mult sub CHECK-ul de 8KB
