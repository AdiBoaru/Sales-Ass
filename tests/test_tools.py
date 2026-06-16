"""G7-1 — tool-uri de catalog + framework (run_tool/registry), fără DB/LLM real.

Query-urile de catalog sunt monkeypatch-uite; testăm: dispatch, validare args, vederile
compacte, izolarea (business_id din ctx), degradarea (tool inexistent / args invalide)."""

from src.models import BusinessConfig, Contact, InboundMessage, TurnContext
from src.tools import catalog_tools as ct
from src.tools.base import enabled_tools, run_tool
from src.worker.runner import PipelineDeps

PRODUCTS = [
    {
        "id": "p1",
        "name": "Crema A",
        "brand": "BrandA",
        "price": 82.99,
        "url": "https://shop/p1",
        "ai_summary": "hidratare profundă",
        "availability": "in_stock",
        "rating": 4.6,
        "review_summary": "clienții apreciază hidratarea",
        "top_pros": ["hidratează bine"],
        "top_cons": [],
        "sentiment": 0.9,
    },
    {
        "id": "p2",
        "name": "Ser B",
        "brand": "BrandB",
        "price": 120.50,
        "url": "https://shop/p2",
        "ai_summary": "calmare",
        "availability": "in_stock",
        "rating": 4.3,
        "review_summary": "textură ușoară",
        "top_pros": ["se absoarbe repede"],
        "top_cons": ["preț"],
        "sentiment": 0.7,
    },
]


class _LLM:
    async def embed(self, texts, *, model=None):
        return [[0.0] * 8 for _ in texts]


def _ctx() -> TurnContext:
    return TurnContext(
        turn_id="t",
        business=BusinessConfig(id="biz-1", slug="s", name="n"),
        contact=Contact(id="c", business_id="biz-1"),
        message=InboundMessage(provider_msg_id="m", body="x"),
        conversation_id="conv",
    )


def _deps(llm=None) -> PipelineDeps:
    return PipelineDeps(conn=object(), redis=None, llm=llm or _LLM())


def test_enabled_tools_phase1():
    assert set(enabled_tools(None)) == {
        "search_products",
        "get_product_details",
        "compare_products",
    }


async def test_search_products_tool(monkeypatch):
    captured = {}

    async def fake_search(conn, business_id, vec, **k):
        captured["business_id"] = business_id  # business_id vine din ctx, nu din args
        return PRODUCTS

    monkeypatch.setattr(ct, "search_products_semantic", fake_search)
    res = await run_tool(_ctx(), _deps(), "search_products", {"query": "cremă", "limit": 6})
    assert res.ok and len(res.products) == 2
    assert "Crema A" in res.llm_view and "[p1]" in res.llm_view
    assert captured["business_id"] == "biz-1"


async def test_get_product_details_tool(monkeypatch):
    async def fake_by_ids(conn, business_id, ids, **k):
        return [PRODUCTS[0]]

    monkeypatch.setattr(ct, "get_products_by_ids", fake_by_ids)
    res = await run_tool(_ctx(), _deps(), "get_product_details", {"product_id": "p1"})
    assert res.ok
    assert "4.6★" in res.llm_view and "hidratează bine" in res.llm_view


async def test_get_product_details_not_found(monkeypatch):
    async def fake_by_ids(*a, **k):
        return []

    monkeypatch.setattr(ct, "get_products_by_ids", fake_by_ids)
    res = await run_tool(_ctx(), _deps(), "get_product_details", {"product_id": "x"})
    assert res.ok is False and res.error == "not_found"


async def test_compare_products_tool(monkeypatch):
    async def fake_by_ids(conn, business_id, ids, **k):
        return PRODUCTS

    monkeypatch.setattr(ct, "get_products_by_ids", fake_by_ids)
    res = await run_tool(_ctx(), _deps(), "compare_products", {"product_ids": ["p1", "p2"]})
    assert res.ok
    assert "Crema A" in res.llm_view and "Ser B" in res.llm_view


async def test_compare_needs_two_existing(monkeypatch):
    async def fake_by_ids(*a, **k):
        return [PRODUCTS[0]]  # doar unul există

    monkeypatch.setattr(ct, "get_products_by_ids", fake_by_ids)
    res = await run_tool(_ctx(), _deps(), "compare_products", {"product_ids": ["p1", "x"]})
    assert res.ok is False and res.error == "need_2"


async def test_unknown_tool_is_graceful():
    res = await run_tool(_ctx(), _deps(), "foo", {})
    assert res.ok is False and "necunoscut" in (res.error or "")


async def test_invalid_args_are_graceful():
    # search_products fără `query` → Pydantic respinge → run_tool prinde → ok=False
    res = await run_tool(_ctx(), _deps(), "search_products", {"price_max": 10})
    assert res.ok is False


async def test_compare_invalid_args_one_id():
    # un singur id → CompareArgs (min 2) respinge → ok=False (nu aruncă)
    res = await run_tool(_ctx(), _deps(), "compare_products", {"product_ids": ["p1"]})
    assert res.ok is False
