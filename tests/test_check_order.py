"""G7-3 — tool check_order + ruta ORDER activă + validator grounded pe sume. ZERO DB/OpenAI.

`get_orders_status` monkeypatch-uit; LLM-ul scriptat la e2e. Acoperă: lookup pe contact/nr,
izolarea (scoped pe contact în apel), totaluri grounded, toolset per-rută, și calea fără produse
(status comandă validat + fallback sigur la sumă inventată)."""

from src.models import BusinessConfig, Contact, InboundMessage, Route, RouteDecision, TurnContext
from src.tools import orders_tools as om
from src.tools.base import enabled_tools
from src.worker.order_gate import login_required_message
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
    async def fake(
        conn, business_id, *, external_id=None, contact_id=None, external_customer_ref=None, limit=3
    ):
        if sink is not None:
            sink.update(
                business_id=business_id,
                external_id=external_id,
                contact_id=contact_id,
                external_customer_ref=external_customer_ref,
                limit=limit,
            )
        return orders

    monkeypatch.setattr(om, "get_orders_status", fake)


# --- toolset per rută --------------------------------------------------------


def test_enabled_tools_route_aware():
    assert set(enabled_tools(None)) == {
        "search_products",
        "get_product_details",
        "compare_products",
        "cart_add",
        "checkout_link",
        "reorder",
        "subscribe_back_in_stock",
        "faq_lookup",
    }
    # ORDER: status comandă + faq_lookup (o întrebare de proces/politică rutată aici e răspunsă
    # din baza de cunoștințe, FĂRĂ cont — nu cade în zidul de login).
    assert set(enabled_tools(None, "order")) == {"check_order", "faq_lookup"}
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
    # NX-130: canal identificat (fără login passthrough) → pe contact_id, NU pe customer_ref
    assert sink["external_customer_ref"] is None


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


# --- NX-128++: poarta de comandă/retur pe web anonim (FAQ-first) -------------


def _web_ctx(*, route=Route.ORDER, body="vreau să fac retur") -> TurnContext:
    ctx = TurnContext(
        turn_id="t",
        business=BusinessConfig(id="b", slug="d", name="D"),
        contact=Contact(id="c", business_id="b"),
        message=InboundMessage(provider_msg_id="m", body=body, channel_kind="webchat"),
        conversation_id="conv",
    )
    if route is not None:
        ctx.route = RouteDecision(route=route)
    return ctx


class _SpyLLM(_FakeLLM):
    """Marchează dacă bucla de tool a fost atinsă."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.loop_called = False

    async def run_tool_loop(self, system, user, tools, execute, *, max_steps=3, model=None):
        self.loop_called = True
        return await super().run_tool_loop(
            system, user, tools, execute, max_steps=max_steps, model=model
        )


async def test_web_order_check_order_gated_to_login(monkeypatch):
    # NX-128++ (FAQ-first): zidul de login NU mai e un scurtcircuit pe toată ruta. Agentul RULEAZĂ;
    # dacă modelul cere chiar un lookup de comandă (check_order) pe web anonim, tool-ul îl gateuie
    # ÎNAINTE de orice DB → servim login determinist. (Un FAQ/altă cerere → nu ajunge aici.)
    calls = {"n": 0}

    async def spy(*a, **k):
        calls["n"] += 1
        return []

    monkeypatch.setattr(om, "get_orders_status", spy)
    ctx = _web_ctx()
    llm = _SpyLLM(tool_calls=[("check_order", {"order_ref": None})], final="x")
    await agent_stage(ctx, _deps(llm))
    assert ctx.reply is not None
    assert ctx.reply.text == login_required_message("ro")  # mesaj de login determinist, nu „x"
    assert ctx.reply.cacheable is False  # context-relativ → nu otrăvește cache-ul
    assert llm.loop_called is True  # agentul rulează (FAQ-first) — nu mai scurtcircuităm pre-buclă
    assert calls["n"] == 0  # gating ÎNAINTE de DB: nu interogăm comenzi pe web anonim


async def test_web_order_faq_answer_not_gated_to_login():
    # Cheia cererii Adi: pe ruta ORDER + web anonim, dacă modelul răspunde FĂRĂ să ceară un lookup
    # de cont (ex. „cum comand" → răspuns de proces, fără check_order), NU apare zidul de login.
    ctx = _web_ctx(body="cum comand un produs?")
    answer = "Adaugi produsul în coș, apoi mergi la checkout și completezi adresa."
    await agent_stage(ctx, _deps(_FakeLLM(tool_calls=[], final=answer)))
    assert ctx.reply is not None
    assert ctx.reply.text == answer  # răspunsul de proces servit, FĂRĂ login
    assert "intră în contul tău" not in ctx.reply.text


async def test_web_order_login_no_handoff_offer_on_web():
    # Handoff off pe web → NU oferim operator, chiar dacă tenantul are `request_human` activ.
    # (Codul `with_handoff`/sufixul rămâne — doar gardat pe canal; reversibil din env.)
    ctx = _web_ctx()
    ctx.business.settings = {"tools": {"request_human": True}}  # tenant CU operator
    llm = _FakeLLM(tool_calls=[("check_order", {"order_ref": None})], final="x")
    await agent_stage(ctx, _deps(llm))
    assert ctx.reply is not None
    assert ctx.reply.text == login_required_message("ro")  # fără sufix de „coleg"
    assert "coleg" not in ctx.reply.text


async def test_no_orders_message_is_channel_aware(monkeypatch):
    _patch_orders(monkeypatch, [])  # canal identificat (whatsapp), fără comenzi
    res = await om.check_order_tool(_ctx(), _deps(), {"order_ref": "GHOST"})
    assert res.ok is False and res.error == "not_found"
    # onest „pe contul tău" (telefon = cont), NU „pe acest cont" (cont căutat inexistent)
    assert "contul tău" in res.llm_view and "acest cont" not in res.llm_view


async def test_web_order_verified_reaches_tool_loop(monkeypatch):
    # NX-129: web cu login passthrough verificat (verified_customer_ref) NU mai e gated → ajunge la
    # bucla de tool (check_order). (Lookup-ul real pe customer_ref e NX-130; aici doar poarta.)
    _patch_orders(monkeypatch, [ORDER])
    ctx = _web_ctx()
    ctx.verified_customer_ref = "cust_1"
    llm = _SpyLLM(tool_calls=[("check_order", {"order_ref": None})], final="Comanda ta e ok.")
    await agent_stage(ctx, _deps(llm))
    assert llm.loop_called is True  # identitate verificată → NU scurtcircuitat de poartă


async def test_verified_web_looks_up_by_customer_ref(monkeypatch):
    # NX-130: web cu identitate verificată → check_order caută pe customer_ref (din sesiunea
    # verificată), NU pe contactul throwaway. Un order_ref în args doar îngustează, nu sare peste.
    sink: dict = {}
    _patch_orders(monkeypatch, [ORDER], sink)
    ctx = _web_ctx(body="unde e comanda mea?")
    ctx.verified_customer_ref = "cust_42"
    res = await om.check_order_tool(ctx, _deps(), {"order_ref": None})
    assert res.ok is True
    assert sink["external_customer_ref"] == "cust_42"  # cheia = customer_ref verificat (din ctx)
    assert sink["contact_id"] is None  # NU contactul web (comenzile reale nu-s legate de el)


# --- NX-128++: zidul de login e la marginea TOOL-ului de cont (nu pe toată ruta) --------------


async def test_check_order_tool_walls_web_anonymous(monkeypatch):
    # Apel direct: pe web anonim, check_order NU interoghează DB (contact throwaway = gol garantat),
    # întoarce DETERMINIST mesajul de login + emite `order_lookup_gated`.
    calls = {"n": 0}

    async def spy(*a, **k):
        calls["n"] += 1
        return []

    monkeypatch.setattr(om, "get_orders_status", spy)
    ctx = _web_ctx(body="unde e comanda mea?")
    res = await om.check_order_tool(ctx, _deps(), {"order_ref": "ORD-1"})
    assert res.ok is False and res.error == "login_required"
    assert res.llm_view == login_required_message("ro")
    assert calls["n"] == 0  # ZERO lookup pe web anonim
    assert any(e.type == "order_lookup_gated" for e in ctx.events)


async def test_reorder_tool_walls_web_anonymous(monkeypatch):
    # Re-comanda e legată de contul clientului → pe web anonim, login, nu „n-ai comenzi anterioare".
    from src.tools import commerce_tools as cm

    calls = {"n": 0}

    async def spy(*a, **k):
        calls["n"] += 1
        return []

    monkeypatch.setattr(cm, "get_orders_status", spy)
    ctx = _web_ctx(body="vreau să comand iar ce am luat data trecută")
    res = await cm.reorder_tool(ctx, _deps(), {})
    assert res.ok is False and res.error == "login_required"
    assert res.llm_view == login_required_message("ro")
    assert calls["n"] == 0


async def test_check_order_tool_identified_channel_not_walled(monkeypatch):
    # Canal identificat (whatsapp = telefon = cont): NU se aplică zidul de login — lookup normal.
    sink: dict = {}
    _patch_orders(monkeypatch, [ORDER], sink)
    res = await om.check_order_tool(_ctx(), _deps(), {"order_ref": None})  # _ctx → channel whatsapp
    assert res.ok is True and "ORD-1" in res.llm_view
