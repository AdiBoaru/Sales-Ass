"""NX-143 — teste izolate pentru `ToolRun` (tool executor, faza D).

Stub-uim `run_tool` (nu rulăm tool-uri reale / DB) și verificăm că `ToolRun.execute` acumulează
corect în câmpuri, emite `tool_call` cu args sanitizate (fără PII) și pasează `ctx` (invariantul de
securitate: `business_id` din context, nu din args). Zero LLM/DB.
"""

from types import SimpleNamespace

from src.agent import tool_executor as te
from src.agent.tool_executor import ToolRun, _safe_tool_args


def _result(**kw):
    base = dict(
        products=[],
        ok=True,
        links=[],
        prices=set(),
        relevance=None,
        state_patch=None,
        llm_view="view",
        error=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _ctx():
    events: list[tuple] = []
    return SimpleNamespace(
        emit=lambda event_type, **kw: events.append((event_type, kw)),
        state_patch={},
        business=SimpleNamespace(id="biz-1"),
        _events=events,
    )


async def test_execute_accumulates_products(monkeypatch):
    async def fake_run_tool(ctx, deps, name, args):
        return _result(products=[{"id": "p1"}, {"id": "p2"}], prices={82.99})

    monkeypatch.setattr(te, "run_tool", fake_run_tool)
    run = ToolRun(_ctx(), object())
    view = await run.execute("search_products", {"category": "creme"})
    assert run.retrieved == [{"id": "p1"}, {"id": "p2"}]
    assert run.grounded_prices == {82.99}
    assert view == "view"


async def test_execute_passes_ctx_business_not_args(monkeypatch):
    """Invariant NX-150: tool-ul primește `ctx` (tenantul din context); un `business_id` fals în
    args al modelului NU e folosit de executor ca sursă de tenant."""
    seen = {}

    async def fake_run_tool(ctx, deps, name, args):
        seen["ctx_business"] = ctx.business.id
        seen["args"] = dict(args)
        return _result()

    monkeypatch.setattr(te, "run_tool", fake_run_tool)
    run = ToolRun(_ctx(), object())
    await run.execute("search_products", {"business_id": "EVIL", "category": "x"})
    assert seen["ctx_business"] == "biz-1"  # sursa de tenant = ctx, nu args


async def test_execute_checkout_link_sets_url_and_link(monkeypatch):
    async def fake_run_tool(ctx, deps, name, args):
        return _result(links=["https://shop/checkout/abc"])

    monkeypatch.setattr(te, "run_tool", fake_run_tool)
    run = ToolRun(_ctx(), object())
    await run.execute("checkout_link", {"cart_items": []})
    assert run.checkout_url == "https://shop/checkout/abc"
    assert "https://shop/checkout/abc" in run.generated_links


async def test_execute_failed_commerce_recorded(monkeypatch):
    async def fake_run_tool(ctx, deps, name, args):
        return _result(ok=False, error="stock", llm_view=None)

    monkeypatch.setattr(te, "run_tool", fake_run_tool)
    run = ToolRun(_ctx(), object())
    await run.execute("cart_add", {"product_id": "p1"})
    assert run.failed_commerce == {"cart_add"}


async def test_execute_check_order_login_gate(monkeypatch):
    async def fake_run_tool(ctx, deps, name, args):
        return _result(ok=False, error="login_required", llm_view=None)

    monkeypatch.setattr(te, "run_tool", fake_run_tool)
    run = ToolRun(_ctx(), object())
    await run.execute("check_order", {"order_number": "123"})
    assert run.order_gated_login is True


async def test_execute_compare_and_added_product(monkeypatch):
    calls = iter(
        [
            _result(products=[{"id": "a"}, {"id": "b"}]),  # compare_products
            _result(products=[{"id": "c"}]),  # cart_add
        ]
    )

    async def fake_run_tool(ctx, deps, name, args):
        return next(calls)

    monkeypatch.setattr(te, "run_tool", fake_run_tool)
    run = ToolRun(_ctx(), object())
    await run.execute("compare_products", {"product_ids": ["a", "b"]})
    await run.execute("cart_add", {"product_id": "c"})
    assert run.compared == [{"id": "a"}, {"id": "b"}]
    assert run.added_product == {"id": "c"}


async def test_execute_emits_tool_call_with_safe_args(monkeypatch):
    async def fake_run_tool(ctx, deps, name, args):
        return _result(products=[{"id": "p1"}])

    monkeypatch.setattr(te, "run_tool", fake_run_tool)
    ctx = _ctx()
    run = ToolRun(ctx, object())
    await run.execute("check_order", {"order_number": "SECRET-123"})
    ev = next(e for e in ctx._events if e[0] == "tool_call")
    assert ev[1]["args"] == {"has_arg": True}  # numărul comenzii NU ajunge în analytics
    assert ev[1]["name"] == "check_order"


def test_safe_tool_args_whitelist():
    assert _safe_tool_args("search_products", {"category": "x", "secret": "y"}) == {"category": "x"}
    assert _safe_tool_args("check_order", {"order_number": "123"}) == {"has_arg": True}
    assert _safe_tool_args("unknown_tool", {"a": 1}) == {}
