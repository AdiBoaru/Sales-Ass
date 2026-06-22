"""NX-122 — trace per-tur: turn_id pe orice event + tool_call îmbogățit (args
whitelisted, count, latență, eroare) + rich_downgraded vizibil. Fără DB/OpenAI:
emit() e pur, _safe_tool_args e pur, insert_events e testat cu o conexiune falsă,
iar calea agent rulează cu LLM scriptat + tool-uri monkeypatch-uite (ca test_agent).
"""

import pytest

from src.db.queries.analytics import insert_events
from src.models import (
    BusinessConfig,
    Contact,
    Event,
    InboundMessage,
    RichReply,
    Route,
    RouteDecision,
    TurnContext,
)
from src.tools import catalog_tools as ct
from src.worker.runner import PipelineDeps
from src.worker.stages import agent as agent_mod
from src.worker.stages.agent import _safe_tool_args, _trunc, agent_stage

# --------------------------------------------------------------------------- #
# 1. emit() injectează turn_id (P10) fără să suprascrie unul explicit (P3)
# --------------------------------------------------------------------------- #


def _bare_ctx(turn_id="turn-xyz") -> TurnContext:
    return TurnContext(
        turn_id=turn_id,
        business=BusinessConfig(id="b", slug="d", name="D"),
        contact=Contact(id="c", business_id="b"),
        message=InboundMessage(provider_msg_id="m", body="salut"),
        conversation_id="conv",
    )


def test_emit_injects_turn_id_on_every_event():
    ctx = _bare_ctx("turn-xyz")
    ctx.emit("cache_hit", layer="semantic")
    ctx.emit("route", route="sales")
    assert all(e.properties["turn_id"] == "turn-xyz" for e in ctx.events)


def test_emit_setdefault_does_not_overwrite_explicit_turn_id():
    ctx = _bare_ctx("turn-real")
    # un apelant care pasează turn_id EXPLICIT (caz rar) nu e rescris de setdefault (P3).
    ctx.emit("synthetic", turn_id="turn-override", k=1)
    assert ctx.events[0].properties["turn_id"] == "turn-override"


# --------------------------------------------------------------------------- #
# 2. _safe_tool_args — whitelist per tool, fără PII (P12)
# --------------------------------------------------------------------------- #


def test_safe_args_search_products_keeps_only_whitelist():
    raw = {
        "category": "creme",
        "brand": "BrandA",
        "concerns": ["hidratare"],
        "price_max": 100,
        "sort_mode": "price_asc",
        "in_stock_only": True,
        "limit": 6,
        "query": "textul de căutare (poate ecoua userul)",  # EXCLUS deliberat → dropat
        "secret": "drop me",
    }
    out = _safe_tool_args("search_products", raw)
    assert out == {
        "category": "creme",
        "brand": "BrandA",
        "concerns": ["hidratare"],
        "price_max": 100,
        "sort_mode": "price_asc",
        "in_stock_only": True,
        "limit": 6,
    }
    assert "query" not in out and "secret" not in out


def test_safe_args_check_order_never_leaks_order_or_contact():
    # numărul comenzii / contactul = PII → NICIODATĂ în properties, doar has_arg.
    out = _safe_tool_args("check_order", {"order_ref": "ORD-12345", "contact": "+40712345678"})
    assert out == {"has_arg": True}
    assert "ORD-12345" not in str(out) and "40712345678" not in str(out)
    assert _safe_tool_args("check_order", {}) == {"has_arg": False}


def test_safe_args_unknown_tool_and_none_values():
    assert _safe_tool_args("faq_lookup", {"query": "cât e livrarea"}) == {}  # PII-arg → omis
    assert _safe_tool_args("necunoscut", {"x": 1}) == {}
    # cheie whitelisted dar None → omisă (nu poluăm cu null-uri)
    assert _safe_tool_args("get_product_details", {"product_id": None}) == {}
    assert _safe_tool_args("get_product_details", {"product_id": "p1"}) == {"product_id": "p1"}


def test_trunc_caps_string_and_list_lengths():
    assert _trunc("x" * 200) == "x" * 64
    assert _trunc(list(range(20))) == list(range(8))
    assert _trunc(["a" * 100]) == ["a" * 64]
    assert _trunc(42) == 42 and _trunc(None) is None


def test_trunc_recurses_into_dict_elements_of_a_list():
    # cart_items = listă de dict-uri (checkout_link) → string-urile din dict trebuie bornate,
    # nu doar cele top-level (altfel un dict în listă scăpa neplafonat).
    out = _trunc([{"product_id": "p1", "note": "z" * 200}])
    assert out == [{"product_id": "p1", "note": "z" * 64}]
    # dict cu >8 chei într-o listă → cap la 8 chei (recursiv)
    big = {f"k{i}": i for i in range(20)}
    assert len(_trunc([big])[0]) == 8


# --------------------------------------------------------------------------- #
# 3. insert_events — turn_id extras în coloană dedicată (NULL dacă lipsește)
# --------------------------------------------------------------------------- #


class _FakeConn:
    """Captează argumentele lui executemany fără DB reală."""

    def __init__(self):
        self.sql = None
        self.rows = None

    async def executemany(self, sql, rows):
        self.sql = sql
        self.rows = list(rows)


async def test_insert_events_extracts_turn_id_into_column():
    conn = _FakeConn()
    events = [
        Event("tool_call", {"name": "search_products", "turn_id": "turn-abc"}),
        Event("legacy_no_turn", {"foo": 1}),  # fără turn_id → coloana primește NULL
    ]
    n = await insert_events(conn, "biz1", events, conversation_id="c1", contact_id="ct1")
    assert n == 2
    assert "turn_id" in conn.sql  # coloana e în INSERT
    # tuplul de rând: (..., tokens_in, tokens_out, cost_usd, turn_id) → turn_id e ultimul
    assert conn.rows[0][-1] == "turn-abc"
    assert conn.rows[1][-1] is None


# --------------------------------------------------------------------------- #
# 4+5. Calea agent: tool_call îmbogățit + rich_downgraded + turn_id partajat
# --------------------------------------------------------------------------- #

_PRODUCTS = [
    {
        "id": "p1",
        "name": "Crema Hidratantă",
        "brand": "BrandA",
        "price": 82.99,
        "url": "https://shop/p1",
        "ai_summary": "hidratare",
        "availability": "in_stock",
        "rating": 4.6,
        "top_pros": ["hidratează bine"],
    },
    {
        "id": "p2",
        "name": "Ser Calmant",
        "brand": "BrandB",
        "price": 120.5,
        "url": "https://shop/p2",
        "ai_summary": "calmare",
        "availability": "in_stock",
        "rating": 4.3,
        "top_pros": ["calmează"],
    },
]


class _FakeLLM:
    def __init__(self, *, tool_calls=(), final=""):
        self._tool_calls = list(tool_calls)
        self._final = final

    async def embed(self, texts, *, model=None):
        return [[0.0] * 8 for _ in texts]

    async def complete(self, system, user, *, model=None):
        return self._final or "Îți recomand aceste produse potrivite."

    async def run_tool_loop(self, system, user, tools, execute, *, max_steps=3, model=None):
        for name, args in self._tool_calls:
            await execute(name, args)
        return self._final


@pytest.fixture(autouse=True)
def _stub_prompt_inputs(monkeypatch):
    async def _cats(conn, business_id):
        return ["Creme"]

    async def _aliases(conn, business_id, **k):
        return []

    monkeypatch.setattr(agent_mod, "list_category_names", _cats)
    monkeypatch.setattr(agent_mod, "list_routing_aliases", _aliases)


def _ctx(body="vreau o cremă") -> TurnContext:
    ctx = _bare_ctx("t")
    ctx.message = InboundMessage(provider_msg_id="m", body=body)
    ctx.route = RouteDecision(route=Route.SALES)
    return ctx


def _deps(llm) -> PipelineDeps:
    return PipelineDeps(conn=object(), redis=None, llm=llm)


def _patch_search(monkeypatch, products):
    async def fake_search(conn, business_id, vec, **k):
        return products

    async def fake_lexical(conn, business_id, **k):
        return []

    async def has_emb(conn, business_id):
        return True

    monkeypatch.setattr(ct, "has_embeddings", has_emb)
    monkeypatch.setattr(ct, "search_products_semantic", fake_search)
    monkeypatch.setattr(ct, "search_products_lexical", fake_lexical)


async def test_tool_call_event_enriched_on_success(monkeypatch):
    _patch_search(monkeypatch, _PRODUCTS)
    # _finalize_rich → None (fără complete_schema) ar emite rich_downgraded; îl forțăm None curat.
    monkeypatch.setattr(agent_mod, "_finalize_rich", _none_rich)
    llm = _FakeLLM(
        tool_calls=[
            (
                "search_products",
                {
                    "query": "cremă hidratantă ten uscat",  # text de căutare → dropat (P12)
                    "category": "creme",
                    "price_max": 100,
                    "brand": "BrandA",
                },
            )
        ],
        final="Îți recomand aceste produse.",
    )
    ctx = _ctx()
    await agent_stage(ctx, _deps(llm))
    tc = next(e for e in ctx.events if e.type == "tool_call")
    assert tc.properties["name"] == "search_products"
    assert tc.properties["ok"] is True
    assert tc.properties["n_results"] == 2
    assert tc.properties["latency_ms"] >= 0
    assert tc.properties["error"] is None
    # args whitelisted: filtrele structurate păstrate, query (text de căutare) dropat (P12)
    assert tc.properties["args"] == {"category": "creme", "price_max": 100, "brand": "BrandA"}
    assert "cremă hidratantă" not in str(tc.properties)


async def test_tool_call_event_on_failure_has_error_no_pii(monkeypatch):
    monkeypatch.setattr(agent_mod, "_finalize_rich", _none_rich)
    # tool inexistent → run_tool întoarce ok=False; args necunoscute → {} (fără PII).
    llm = _FakeLLM(tool_calls=[("necunoscut", {"order_ref": "ORD-9"})], final="ok")
    ctx = _ctx()
    await agent_stage(ctx, _deps(llm))
    tc = next(e for e in ctx.events if e.type == "tool_call")
    assert tc.properties["ok"] is False
    assert tc.properties["error"] is not None
    assert tc.properties["n_results"] == 0
    assert tc.properties["args"] == {}
    assert "ORD-9" not in str(tc.properties)


async def _none_rich(*a, **k):
    return None


async def _empty_rich(*a, **k):
    return RichReply(intro=None, items=[], pick=None, education=None, chips=[], disclaimer="")


async def test_rich_downgraded_structured_call_failed(monkeypatch):
    _patch_search(monkeypatch, _PRODUCTS)
    monkeypatch.setattr(agent_mod, "_finalize_rich", _none_rich)
    llm = _FakeLLM(
        tool_calls=[("search_products", {"query": "cremă", "category": "creme"})],
        final="Îți recomand aceste produse.",
    )
    ctx = _ctx()
    await agent_stage(ctx, _deps(llm))
    ev = next(e for e in ctx.events if e.type == "rich_downgraded")
    assert ev.properties["reason"] == "structured-call-failed"


async def test_rich_downgraded_all_items_dropped(monkeypatch):
    _patch_search(monkeypatch, _PRODUCTS)
    monkeypatch.setattr(agent_mod, "_finalize_rich", _empty_rich)
    llm = _FakeLLM(
        tool_calls=[("search_products", {"query": "cremă", "category": "creme"})],
        final="Îți recomand aceste produse.",
    )
    ctx = _ctx()
    await agent_stage(ctx, _deps(llm))
    ev = next(e for e in ctx.events if e.type == "rich_downgraded")
    assert ev.properties["reason"] == "all-items-dropped-by-membership"


async def test_all_events_of_a_turn_share_turn_id(monkeypatch):
    _patch_search(monkeypatch, _PRODUCTS)
    monkeypatch.setattr(agent_mod, "_finalize_rich", _none_rich)
    llm = _FakeLLM(
        tool_calls=[("search_products", {"query": "cremă", "category": "creme"})],
        final="Îți recomand aceste produse.",
    )
    ctx = _ctx()
    await agent_stage(ctx, _deps(llm))
    assert ctx.events  # s-au emis event-uri (tool_call, rich_downgraded, agent_recommended...)
    assert all(e.properties.get("turn_id") == "t" for e in ctx.events)
