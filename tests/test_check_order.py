"""G7-3 — tool check_order + ruta ORDER activă + validator grounded pe sume. ZERO DB/OpenAI.

`get_orders_status` monkeypatch-uit; LLM-ul scriptat la e2e. Acoperă: lookup pe contact/nr,
izolarea (scoped pe contact în apel), totaluri grounded, toolset per-rută, și calea fără produse
(status comandă validat + fallback sigur la sumă inventată)."""

from src.models import BusinessConfig, Contact, InboundMessage, Route, RouteDecision, TurnContext
from src.tools import orders_tools as om
from src.tools.base import enabled_tools
from src.worker.runner import PipelineDeps
from src.worker.stages.agent import _prices_ok, _valid, agent_stage

ORDER = {
    "id": "o1",
    "contact_id": "c",
    "external_id": "ORD-1",
    "status": "shipped",
    "total": 247.50,
    "currency": "RON",
    "carrier": "FAN",
    "awb": "RO123456789",
    "shipment_status": "in_transit",
    "eta": "2026-06-18",
    "items": [{"name": "Crema", "quantity": 1, "unit_price": 82.99}],
}


def _ctx(*, route=Route.ORDER, body="unde e comanda mea?", contact_id="c") -> TurnContext:
    ctx = TurnContext(
        turn_id="t",
        business=BusinessConfig(id="b", slug="d", name="D"),
        contact=Contact(id=contact_id, business_id="b"),
        message=InboundMessage(provider_msg_id="m", body=body),
        conversation_id="conv",
    )
    if route is not None:
        ctx.route = RouteDecision(route=route)
    return ctx


def _deps(llm=None) -> PipelineDeps:
    return PipelineDeps(conn=object(), redis=None, llm=llm)


def _patch_orders(monkeypatch, orders, sink=None):
    async def fake(conn, business_id, *, external_id=None, contact_id=None, limit=3):
        if sink is not None:
            sink.update(
                business_id=business_id, external_id=external_id, contact_id=contact_id, limit=limit
            )
        return orders

    monkeypatch.setattr(om, "get_orders_status", fake)


# --- toolset per rută --------------------------------------------------------


def test_enabled_tools_route_aware():
    assert set(enabled_tools(None)) == {
        "search_products",
        "get_product_details",
        "compare_products",
        "checkout_link",
    }
    assert set(enabled_tools(None, "order")) == {"check_order"}
    assert "check_order" not in enabled_tools(None, "sales")  # nu pe SALES


# --- tool check_order --------------------------------------------------------


async def test_check_order_by_contact(monkeypatch):
    sink: dict = {}
    _patch_orders(monkeypatch, [ORDER], sink)
    res = await om.check_order_tool(_ctx(), _deps(), {"order_ref": None})
    assert res.ok is True and res.products == []
    assert 247.50 in res.prices and 82.99 in res.prices  # total + unit_price grounded
    assert "ORD-1" in res.llm_view and "shipped" in res.llm_view and "RO123456789" in res.llm_view
    # izolare: lookup scoped pe contactul curent, fără order_ref → ultimele 3
    assert sink["contact_id"] == "c" and sink["external_id"] is None and sink["limit"] == 3


async def test_check_order_by_ref_is_contact_scoped(monkeypatch):
    sink: dict = {}
    _patch_orders(monkeypatch, [ORDER], sink)
    await om.check_order_tool(_ctx(), _deps(), {"order_ref": "ORD-1"})
    # IZOLARE: și pe lookup după nr comandă, filtrăm pe contactul curent (în SQL) + limit 1
    assert sink["external_id"] == "ORD-1" and sink["contact_id"] == "c" and sink["limit"] == 1


async def test_check_order_not_found(monkeypatch):
    _patch_orders(monkeypatch, [])  # comandă inexistentă SAU a altui contact
    res = await om.check_order_tool(_ctx(), _deps(), {"order_ref": "GHOST"})
    assert res.ok is False and res.error == "not_found"
    assert res.prices == []


async def test_check_order_no_shipment_no_crash(monkeypatch):
    bare = {**ORDER, "awb": None, "carrier": None, "shipment_status": None, "eta": None}
    _patch_orders(monkeypatch, [bare])
    res = await om.check_order_tool(_ctx(), _deps(), {"order_ref": None})
    assert res.ok is True and "AWB" not in res.llm_view and "ETA" not in res.llm_view


# --- validator grounded pe sume ----------------------------------------------


def test_prices_ok_accepts_grounded_total():
    assert _prices_ok("Total: 247.50 lei", [], {247.50}) is True
    assert _prices_ok("Total: 999 lei", [], {247.50}) is False  # sumă negroundată
    assert _valid("Comanda ta, total 247.50 lei", [], set(), {247.50}) is True


# --- e2e prin agent_stage (ruta ORDER) ---------------------------------------


class _FakeLLM:
    def __init__(self, *, tool_calls=(), final="", retry="Statusul comenzii tale e în regulă."):
        self._tc = list(tool_calls)
        self._final = final
        self._retry = retry
        self.complete_calls = 0

    async def embed(self, texts, *, model=None):
        return [[0.0] * 8 for _ in texts]

    async def complete(self, system, user, *, model=None):
        self.complete_calls += 1
        return self._retry

    async def run_tool_loop(self, system, user, tools, execute, *, max_steps=3, model=None):
        for name, args in self._tc:
            await execute(name, args)
        return self._final


async def test_order_route_serves_grounded_status(monkeypatch):
    _patch_orders(monkeypatch, [ORDER])
    ctx = _ctx()
    llm = _FakeLLM(
        tool_calls=[("check_order", {"order_ref": None})],
        final="Comanda ORD-1 e în livrare (AWB RO123456789), ajunge ~18 iun. Total 247.50 lei.",
    )
    await agent_stage(ctx, _deps(llm))
    assert ctx.reply is not None and "247.50" in ctx.reply.text  # total grounded → acceptat
    assert ctx.reply.products is None  # fără carduri de produs pe ORDER
    assert llm.complete_calls == 0  # valid din prima, fără retry


async def test_order_invented_total_falls_back_safely(monkeypatch):
    _patch_orders(monkeypatch, [ORDER])
    ctx = _ctx()
    # final inventează 999; retry-ul (complete) inventează 888 → fallback sigur, non-produs
    llm = _FakeLLM(
        tool_calls=[("check_order", {"order_ref": None})],
        final="Comanda ta, total 999 lei.",
        retry="De fapt 888 lei.",
    )
    await agent_stage(ctx, _deps(llm))
    assert ctx.reply is not None
    assert "999" not in ctx.reply.text and "888" not in ctx.reply.text  # zero sume inventate
    assert "verificat comanda" in ctx.reply.text  # fallback de status, NU „Îți recomand…"
    assert llm.complete_calls == 1  # exact 1 retry order-shaped


async def test_non_sales_non_order_is_noop():
    ctx = _ctx(route=Route.SIMPLE)
    await agent_stage(ctx, _deps(_FakeLLM(final="x")))
    assert ctx.reply is None  # agentul nu rulează pe alte rute
