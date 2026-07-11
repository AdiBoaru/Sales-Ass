"""NX-79/80 — tool-uri de comerț: cart_add + reorder + subscribe_back_in_stock.

Cod determinist; query-urile de DB monkeypatch-uite la nivelul modulului `commerce_tools`
(unde sunt importate). `run_tool` real → exersează dispatch + validare args + degradare.
ZERO DB/LLM real în CI. Izolarea: `business_id`/`contact_id` vin din `ctx`, nu din args.
"""

from src.agent.tool_definitions import tool_schemas
from src.models import BusinessConfig, Contact, ConversationState, InboundMessage, TurnContext
from src.tools import commerce_tools as ctools
from src.tools.base import run_tool
from src.worker.runner import PipelineDeps

P_OOS = {"id": "p1", "name": "Crema A", "price": 82.99, "availability": "out_of_stock"}
P_IN = {"id": "p2", "name": "Ser B", "price": 120.0, "availability": "in_stock"}


def _ctx() -> TurnContext:
    return TurnContext(
        turn_id="t",
        business=BusinessConfig(id="biz-1", slug="s", name="n"),
        contact=Contact(id="contact-1", business_id="biz-1"),
        message=InboundMessage(provider_msg_id="m", body="x"),
        conversation_id="conv",
    )


def _deps() -> PipelineDeps:
    return PipelineDeps(conn=object(), redis=None, llm=None)


def _event(ctx, type_):
    return next((e for e in reversed(ctx.events) if e.type == type_), None)


def _patch_by_ids(monkeypatch, products):
    async def fake(conn, business_id, ids, **k):
        return [p for p in products if p["id"] in ids]

    monkeypatch.setattr(ctools, "get_products_by_ids", fake)


# --- cart_add ----------------------------------------------------------------


async def test_cart_add_happy(monkeypatch):
    _patch_by_ids(monkeypatch, [P_IN])
    ctx = _ctx()
    res = await run_tool(ctx, _deps(), "cart_add", {"product_id": "p2", "quantity": 2})
    assert res.ok
    assert res.state_patch["cart"] == [
        {"product_id": "p2", "variant_id": None, "name": "Ser B", "price": 120.0, "quantity": 2}
    ]
    assert res.prices == [240.0]  # total grounded (price × qty)
    ev = _event(ctx, "cart_updated")
    # NX-163: + product_ids (ref-uri, P8) pt raportul de cerere; fără PII.
    assert ev and ev.properties == {
        "lines": 1,
        "value": 240.0,
        "product_ids": ["p2"],
        "turn_id": "t",
    }


async def test_cart_add_merges_same_line(monkeypatch):
    _patch_by_ids(monkeypatch, [P_IN])
    ctx = _ctx()
    ctx.state.cart = [
        {"product_id": "p2", "variant_id": None, "name": "Ser B", "price": 120.0, "quantity": 1}
    ]
    res = await run_tool(ctx, _deps(), "cart_add", {"product_id": "p2", "quantity": 2})
    assert res.ok and len(res.state_patch["cart"]) == 1  # o singură linie, nu duplicat
    assert res.state_patch["cart"][0]["quantity"] == 3  # 1 + 2


async def test_cart_add_quantity_capped_at_99(monkeypatch):
    _patch_by_ids(monkeypatch, [P_IN])
    ctx = _ctx()
    ctx.state.cart = [
        {"product_id": "p2", "variant_id": None, "name": "Ser B", "price": 120.0, "quantity": 99}
    ]
    res = await run_tool(ctx, _deps(), "cart_add", {"product_id": "p2", "quantity": 5})
    assert res.state_patch["cart"][0]["quantity"] == 99  # min(99 + 5, 99)


async def test_cart_add_caps_at_10_lines(monkeypatch):
    products = [
        {"id": f"p{i}", "name": f"P{i}", "price": 10.0, "availability": "in_stock"}
        for i in range(12)
    ]
    _patch_by_ids(monkeypatch, products)
    ctx = _ctx()
    ctx.state.cart = [
        {"product_id": f"p{i}", "variant_id": None, "name": f"P{i}", "price": 10.0, "quantity": 1}
        for i in range(10)
    ]
    res = await run_tool(ctx, _deps(), "cart_add", {"product_id": "p11", "quantity": 1})
    assert len(res.state_patch["cart"]) == 10  # cap dur


async def test_cart_add_product_not_found(monkeypatch):
    _patch_by_ids(monkeypatch, [])  # catalogul (scoped pe business) nu-l are
    ctx = _ctx()
    res = await run_tool(ctx, _deps(), "cart_add", {"product_id": "ghost"})
    assert res.ok is False and res.error == "product_not_found"
    assert res.state_patch == {}  # coșul existent neatins


# --- NX-118: variant-membership pe cart_add ----------------------------------

_P_VAR = {
    "id": "p2",
    "name": "Ser B",
    "price": 89.0,
    "availability": "in_stock",
    "variants": [{"id": "v1", "label": "50ml", "price": 89.0}, {"id": "v2", "label": "100ml"}],
}


async def test_cart_add_valid_variant(monkeypatch):
    _patch_by_ids(monkeypatch, [_P_VAR])
    ctx = _ctx()
    res = await run_tool(ctx, _deps(), "cart_add", {"product_id": "p2", "variant_id": "v2"})
    assert res.ok and res.state_patch["cart"][0]["variant_id"] == "v2"


async def test_cart_add_unknown_variant_rejected(monkeypatch):
    _patch_by_ids(monkeypatch, [_P_VAR])
    ctx = _ctx()
    res = await run_tool(ctx, _deps(), "cart_add", {"product_id": "p2", "variant_id": "fabricat"})
    assert res.ok is False and res.error == "variant_not_found"
    assert res.state_patch == {}
    assert _event(ctx, "variant_rejected") is not None


async def test_cart_add_no_variant_id_unchanged(monkeypatch):
    _patch_by_ids(monkeypatch, [_P_VAR])
    ctx = _ctx()
    res = await run_tool(ctx, _deps(), "cart_add", {"product_id": "p2"})
    assert res.ok and res.state_patch["cart"][0]["variant_id"] is None


# --- reorder -----------------------------------------------------------------


def _patch_orders(monkeypatch, orders):
    captured = {}

    async def fake(conn, business_id, *, external_id=None, contact_id=None, limit=3):
        captured["business_id"] = business_id
        captured["contact_id"] = contact_id
        return orders

    monkeypatch.setattr(ctools, "get_orders_status", fake)
    return captured


async def test_reorder_happy(monkeypatch):
    orders = [
        {
            "id": "ord-1",
            "items": [
                {"name": "Crema A", "quantity": 2, "unit_price": 80.0},
                {"name": "Ser B", "quantity": 1, "unit_price": 120.0},
            ],
        }
    ]
    captured = _patch_orders(monkeypatch, orders)
    ctx = _ctx()
    res = await run_tool(ctx, _deps(), "reorder", {})
    assert res.ok and res.prices == [80.0, 120.0]
    assert "Crema A" in res.llm_view and "Ser B" in res.llm_view
    ev = _event(ctx, "reorder_suggested")
    assert ev and ev.properties == {"order_id": "ord-1", "lines": 2, "turn_id": "t"}
    # contact_id/business_id din ctx, nu din args (izolare)
    assert captured["business_id"] == "biz-1" and captured["contact_id"] == "contact-1"


async def test_reorder_no_orders(monkeypatch):
    _patch_orders(monkeypatch, [])
    res = await run_tool(_ctx(), _deps(), "reorder", {})
    assert res.ok is False and res.error == "no_orders"


async def test_reorder_order_without_items(monkeypatch):
    _patch_orders(monkeypatch, [{"id": "ord-1", "items": []}])
    res = await run_tool(_ctx(), _deps(), "reorder", {})
    assert res.ok is False and res.error == "no_items"


# --- subscribe_back_in_stock -------------------------------------------------


async def test_back_in_stock_subscribes_out_of_stock(monkeypatch):
    _patch_by_ids(monkeypatch, [P_OOS])
    captured = {}

    async def fake_sub(conn, business_id, contact_id, product_id, variant_id=None):
        captured.update(
            business_id=business_id,
            contact_id=contact_id,
            product_id=product_id,
            variant_id=variant_id,
        )
        return {"id": "sub-1", "created": True}

    async def no_existing(conn, business_id, contact_id, product_id, variant_id):
        return False

    monkeypatch.setattr(ctools, "subscribe_back_in_stock", fake_sub)
    monkeypatch.setattr(ctools, "has_back_in_stock_sub", no_existing)
    ctx = _ctx()
    res = await run_tool(ctx, _deps(), "subscribe_back_in_stock", {"product_id": "p1"})
    assert res.ok and "revine pe stoc" in res.llm_view
    # contact_id din ctx, NU din args (P12)
    assert captured == {
        "business_id": "biz-1",
        "contact_id": "contact-1",
        "product_id": "p1",
        "variant_id": None,
    }
    ev = _event(ctx, "back_in_stock_subscribed")
    assert ev and ev.properties == {"product_id": "p1", "created": True, "turn_id": "t"}


async def test_back_in_stock_already_subscribed_variant_null(monkeypatch):
    _patch_by_ids(monkeypatch, [P_OOS])

    async def has_existing(conn, business_id, contact_id, product_id, variant_id):
        return True  # guard: deja abonat pe variant NULL

    async def boom_sub(*a, **k):
        raise AssertionError("nu trebuie să mai inserăm dacă există deja (guard variant NULL)")

    monkeypatch.setattr(ctools, "has_back_in_stock_sub", has_existing)
    monkeypatch.setattr(ctools, "subscribe_back_in_stock", boom_sub)
    ctx = _ctx()
    res = await run_tool(ctx, _deps(), "subscribe_back_in_stock", {"product_id": "p1"})
    assert res.ok and "deja pe lista" in res.llm_view
    assert _event(ctx, "back_in_stock_subscribed").properties["created"] is False


async def test_back_in_stock_in_stock_is_noop(monkeypatch):
    _patch_by_ids(monkeypatch, [P_IN])

    async def boom_sub(*a, **k):
        raise AssertionError("produs pe stoc → fără abonare")

    monkeypatch.setattr(ctools, "subscribe_back_in_stock", boom_sub)
    res = await run_tool(_ctx(), _deps(), "subscribe_back_in_stock", {"product_id": "p2"})
    assert res.ok and "pe stoc acum" in res.llm_view


async def test_back_in_stock_product_not_found(monkeypatch):
    _patch_by_ids(monkeypatch, [])  # alt tenant / inexistent

    async def boom_sub(*a, **k):
        raise AssertionError("produs inexistent în catalogul businessului → fără abonare")

    monkeypatch.setattr(ctools, "subscribe_back_in_stock", boom_sub)
    res = await run_tool(_ctx(), _deps(), "subscribe_back_in_stock", {"product_id": "ghost"})
    assert res.ok is False and res.error == "not_found"


# --- state hydration + scheme OpenAI -----------------------------------------


def test_from_jsonb_hydrates_cart_defensively():
    state = ConversationState.from_jsonb(
        {
            "cart": [
                {"product_id": "p1", "name": "Crema A", "price": "80", "quantity": 2},
                {"product_id": "p2", "name": "fără preț"},  # incompletă → sărită
                {"name": "fără id", "price": 10, "quantity": 1},  # fără product_id → sărită
            ]
        }
    )
    assert state.cart == [
        {"product_id": "p1", "variant_id": None, "name": "Crema A", "price": 80.0, "quantity": 2}
    ]


def test_from_jsonb_cart_defaults_empty():
    assert ConversationState.from_jsonb({}).cart == []
    assert ConversationState.from_jsonb(None).cart == []


def test_new_tool_schemas_strict_without_business_id():
    schemas = tool_schemas(["cart_add", "reorder", "subscribe_back_in_stock"])
    by_name = {s["function"]["name"]: s["function"] for s in schemas}
    assert set(by_name) == {"cart_add", "reorder", "subscribe_back_in_stock"}
    for fn in by_name.values():
        assert fn["strict"] is True
        assert "business_id" not in fn["parameters"]["properties"]  # se ia din ctx (P7)
    # reorder = fără argumente
    assert by_name["reorder"]["parameters"]["properties"] == {}
    assert by_name["reorder"]["parameters"]["required"] == []
