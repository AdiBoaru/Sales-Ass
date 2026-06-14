"""Teste unit pentru stagiul Agent — LLM + search mockuite (fără DB/apeluri reale)."""

from src.models import BusinessConfig, Contact, InboundMessage, Route, RouteDecision, TurnContext
from src.worker.runner import PipelineDeps
from src.worker.stages import agent as agent_mod
from src.worker.stages.agent import _budget, _prices_ok, agent_stage

PRODUCTS = [
    {
        "id": "1", "name": "Crema Hidratantă", "brand": "BrandA", "price": 82.99,
        "url": "u1", "ai_summary": "hidratare profundă", "stock": 5, "availability": "in_stock",
    },
    {
        "id": "2", "name": "Ser Calmant", "brand": "BrandB", "price": 120.50,
        "url": "u2", "ai_summary": "calmare", "stock": 3, "availability": "in_stock",
    },
]


class FakeLLM:
    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.embed_calls = 0
        self.complete_calls = 0

    async def embed(self, texts, *, model=None):
        self.embed_calls += 1
        return [[0.0] * 8 for _ in texts]

    async def complete(self, system, user, *, model=None):
        self.complete_calls += 1
        return self.replies.pop(0) if self.replies else "fallback"


def _ctx(body="vreau o cremă", route=Route.SALES, category=None) -> TurnContext:
    ctx = TurnContext(
        turn_id="t",
        business=BusinessConfig(id="b", slug="d", name="D"),
        contact=Contact(id="c", business_id="b"),
        message=InboundMessage(provider_msg_id="m", body=body),
        conversation_id="conv",
    )
    if route is not None:
        ctx.route = RouteDecision(route=route, category_key=category)
    return ctx


def _deps(llm) -> PipelineDeps:
    return PipelineDeps(conn=object(), redis=None, llm=llm)


def test_budget_extraction():
    assert _budget("o cremă sub 150 lei") == 150
    assert _budget("buget 200") == 200
    assert _budget("maxim 90 ron") == 90
    assert _budget("vreau o cremă") is None


def test_prices_ok():
    assert _prices_ok("Crema la 82.99 lei", PRODUCTS) is True
    assert _prices_ok("Crema la 82,99 lei e bună", PRODUCTS) is True  # virgulă
    assert _prices_ok("Crema la 999 lei", PRODUCTS) is False  # inventat
    assert _prices_ok("fără niciun preț", PRODUCTS) is True


async def test_non_sales_is_noop():
    ctx = _ctx(route=Route.SIMPLE)
    llm = FakeLLM(["x"])
    await agent_stage(ctx, _deps(llm))
    assert ctx.reply is None
    assert llm.embed_calls == 0  # nici nu atinge LLM-ul


async def test_no_llm_is_noop():
    ctx = _ctx()
    await agent_stage(ctx, PipelineDeps(conn=object(), redis=None, llm=None))
    assert ctx.reply is None


async def test_sales_recommends(monkeypatch):
    async def fake_search(*a, **k):
        return PRODUCTS

    monkeypatch.setattr(agent_mod, "search_products_semantic", fake_search)
    ctx = _ctx()
    llm = FakeLLM(["Îți recomand Crema Hidratantă la 82.99 lei — bună pentru hidratare."])
    await agent_stage(ctx, _deps(llm))
    assert ctx.reply is not None
    assert "82.99" in ctx.reply.text
    assert ctx.retrieval is not None
    assert len(ctx.retrieval.products) == 2
    assert any(e.type == "agent_recommended" for e in ctx.events)


async def test_no_products_asks_clarify(monkeypatch):
    async def fake_search(*a, **k):
        return []

    monkeypatch.setattr(agent_mod, "search_products_semantic", fake_search)
    ctx = _ctx()
    llm = FakeLLM([])
    await agent_stage(ctx, _deps(llm))
    assert ctx.reply is not None
    assert "n-am găsit" in ctx.reply.text.lower()


async def test_invented_price_falls_back_to_deterministic(monkeypatch):
    async def fake_search(*a, **k):
        return PRODUCTS

    monkeypatch.setattr(agent_mod, "search_products_semantic", fake_search)
    ctx = _ctx()
    llm = FakeLLM(["Crema la 999 lei", "Crema la 888 lei"])  # ambele inventează
    await agent_stage(ctx, _deps(llm))
    assert ctx.reply is not None
    assert "82.99" in ctx.reply.text  # fallback cu prețuri REALE
    assert "999" not in ctx.reply.text
    assert llm.complete_calls == 2  # 1 + 1 retry
