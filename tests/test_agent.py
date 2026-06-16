"""G7-1 — stagiul Agent cu tool-calling (LLM + tool-uri mockuite, fără DB/apeluri reale).

FakeLLM scriptează tool_calls (prin `execute`) + textul final; query-urile de catalog din
`catalog_tools` sunt monkeypatch-uite. Testăm: recomandare grounded, retrieval acumulat,
fallback la preț inventat, răspuns fără produse, helperele de validare."""

from src.models import BusinessConfig, Contact, InboundMessage, Route, RouteDecision, TurnContext
from src.tools import catalog_tools as ct
from src.worker.runner import PipelineDeps
from src.worker.stages.agent import _budget, _links_ok, _prices_ok, agent_stage

PRODUCTS = [
    {
        "id": "p1",
        "name": "Crema Hidratantă",
        "brand": "BrandA",
        "price": 82.99,
        "url": "https://shop/p1",
        "ai_summary": "hidratare profundă",
        "availability": "in_stock",
        "rating": 4.6,
        "top_pros": ["hidratează bine"],
    },
    {
        "id": "p2",
        "name": "Ser Calmant",
        "brand": "BrandB",
        "price": 120.50,
        "url": "https://shop/p2",
        "ai_summary": "calmare",
        "availability": "in_stock",
        "rating": 4.3,
        "top_pros": ["calmează"],
    },
]


class FakeLLM:
    """Scriptează bucla: `tool_calls` (rulate prin execute) + `final`; `retry` = textul
    de la al doilea `complete` (validator retry)."""

    def __init__(self, *, tool_calls=(), final="", retry=None):
        self._tool_calls = list(tool_calls)
        self._final = final
        self._retry = retry
        self.complete_calls = 0
        self.embed_calls = 0

    async def embed(self, texts, *, model=None):
        self.embed_calls += 1
        return [[0.0] * 8 for _ in texts]

    async def complete(self, system, user, *, model=None):
        self.complete_calls += 1
        return self._retry if self._retry is not None else "fallback"

    async def run_tool_loop(self, system, user, tools, execute, *, max_steps=3, model=None):
        for name, args in self._tool_calls:
            await execute(name, args)
        return self._final


def _ctx(body="vreau o cremă", route=Route.SALES) -> TurnContext:
    ctx = TurnContext(
        turn_id="t",
        business=BusinessConfig(id="b", slug="d", name="D"),
        contact=Contact(id="c", business_id="b"),
        message=InboundMessage(provider_msg_id="m", body=body),
        conversation_id="conv",
    )
    if route is not None:
        ctx.route = RouteDecision(route=route)
    return ctx


def _deps(llm) -> PipelineDeps:
    return PipelineDeps(conn=object(), redis=None, llm=llm)


def _patch_search(monkeypatch, products):
    async def fake_search(conn, business_id, vec, **k):
        return products

    monkeypatch.setattr(ct, "search_products_semantic", fake_search)


# --- helpere de validare (pure) ----------------------------------------------


def test_budget_extraction():
    assert _budget("o cremă sub 150 lei") == 150
    assert _budget("vreau o cremă") is None


def test_prices_ok():
    assert _prices_ok("Crema la 82.99 lei", PRODUCTS) is True
    assert _prices_ok("Crema la 999 lei", PRODUCTS) is False
    assert _prices_ok("fără niciun preț", PRODUCTS) is True


def test_links_ok():
    assert _links_ok("vezi https://shop/p1 aici", PRODUCTS) is True
    assert _links_ok("link inventat https://evil/x", PRODUCTS) is False
    assert _links_ok("fără link", PRODUCTS) is True


# --- agent_stage -------------------------------------------------------------


async def test_non_sales_is_noop():
    ctx = _ctx(route=Route.SIMPLE)
    llm = FakeLLM(final="x")
    await agent_stage(ctx, _deps(llm))
    assert ctx.reply is None
    assert llm.embed_calls == 0


async def test_no_llm_is_noop():
    ctx = _ctx()
    await agent_stage(ctx, PipelineDeps(conn=object(), redis=None, llm=None))
    assert ctx.reply is None


async def test_sales_recommends_via_tool(monkeypatch):
    _patch_search(monkeypatch, PRODUCTS)
    ctx = _ctx()
    llm = FakeLLM(
        tool_calls=[("search_products", {"query": "cremă", "price_max": None, "limit": 6})],
        final="Îți recomand Crema Hidratantă la 82.99 lei — bună pentru hidratare.",
    )
    await agent_stage(ctx, _deps(llm))

    assert ctx.reply is not None and "82.99" in ctx.reply.text
    assert ctx.retrieval is not None and len(ctx.retrieval.products) == 2
    assert any(
        e.type == "tool_call" and e.properties["name"] == "search_products" for e in ctx.events
    )
    assert any(e.type == "agent_recommended" for e in ctx.events)
    # carduri W1 (compact)
    assert ctx.reply.products[0]["name"] == "Crema Hidratantă"
    assert "price" in ctx.reply.products[0]


async def test_no_products_asks_clarify(monkeypatch):
    _patch_search(monkeypatch, [])
    ctx = _ctx()
    llm = FakeLLM(
        tool_calls=[("search_products", {"query": "x", "price_max": None, "limit": 6})], final=""
    )
    await agent_stage(ctx, _deps(llm))
    assert ctx.reply is not None and "n-am găsit" in ctx.reply.text.lower()


async def test_invented_price_falls_back_to_deterministic(monkeypatch):
    _patch_search(monkeypatch, PRODUCTS)
    ctx = _ctx()
    # textul final inventează 999; retry-ul (complete) inventează 888 → fallback determinist
    llm = FakeLLM(
        tool_calls=[("search_products", {"query": "cremă", "price_max": None, "limit": 6})],
        final="Crema la 999 lei",
        retry="Crema la 888 lei",
    )
    await agent_stage(ctx, _deps(llm))
    assert ctx.reply is not None
    assert "82.99" in ctx.reply.text and "999" not in ctx.reply.text
    assert llm.complete_calls == 1  # exact 1 retry


async def test_model_answers_without_tools(monkeypatch):
    # modelul răspunde direct (clarificare), fără să cheme vreun tool → servim textul
    ctx = _ctx()
    llm = FakeLLM(tool_calls=[], final="Salut! Ce tip de ten ai?")
    await agent_stage(ctx, _deps(llm))
    assert ctx.reply is not None and ctx.reply.text == "Salut! Ce tip de ten ai?"
    assert ctx.reply.products is None
    assert ctx.retrieval is not None and ctx.retrieval.products == []
