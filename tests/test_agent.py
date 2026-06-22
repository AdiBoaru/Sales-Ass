"""G7-1 — stagiul Agent cu tool-calling (LLM + tool-uri mockuite, fără DB/apeluri reale).

FakeLLM scriptează tool_calls (prin `execute`) + textul final; query-urile de catalog din
`catalog_tools` sunt monkeypatch-uite. Testăm: recomandare grounded, retrieval acumulat,
fallback la preț inventat, răspuns fără produse, helperele de validare."""

import pytest

from src.models import (
    BusinessConfig,
    Contact,
    InboundMessage,
    ProductRef,
    Route,
    RouteDecision,
    TurnContext,
)
from src.tools import catalog_tools as ct
from src.worker.runner import PipelineDeps
from src.worker.stages import agent as agent_mod
from src.worker.stages.agent import _budget, _links_ok, _prices_ok, agent_stage


@pytest.fixture(autouse=True)
def _stub_prompt_inputs(monkeypatch):
    """NX-78: agent_stage citește categorii/aliase din DB pt promptul generat. Testele folosesc
    `conn=object()` → stubbim cele două query-uri (prompt generic, fără DB reală)."""

    async def _cats(conn, business_id):
        return ["Creme", "Parfumuri"]

    async def _aliases(conn, business_id, **k):
        return []

    monkeypatch.setattr(agent_mod, "list_category_names", _cats)
    monkeypatch.setattr(agent_mod, "list_routing_aliases", _aliases)


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

    async def fake_lexical(conn, business_id, **k):  # NX-113b: lexical rulează MEREU (hibrid)
        return []

    async def has_emb(conn, business_id):  # NX-98: tenant cu embeddings → calea semantică
        return True

    monkeypatch.setattr(ct, "has_embeddings", has_emb)
    monkeypatch.setattr(ct, "search_products_semantic", fake_search)
    monkeypatch.setattr(ct, "search_products_lexical", fake_lexical)


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
    # «n-am găsit» NU se cache-uiește (altfel otrăvește semantic_cache → re-servit, sare agentul).
    assert ctx.reply.cacheable is False


async def test_sales_rehydrates_displayed_products_when_no_retrieval(monkeypatch):
    """R3: follow-up („care e cea mai bună?") la care modelul NU recheamă un tool → re-hidratează
    produsele DEJA arătate (din state, după id) și răspunde grounded pe ele, NU «n-am găsit»."""

    async def fake_by_ids(conn, business_id, ids, **k):
        assert ids == ["p1", "p2"]  # id-urile produselor afișate (din state)
        return PRODUCTS

    monkeypatch.setattr("src.worker.stages.agent.get_products_by_ids", fake_by_ids)
    ctx = _ctx(body="care dintre ele e cea mai bună?")
    ctx.state.displayed_products = [
        ProductRef("p1", "Crema Hidratantă", 82.99),
        ProductRef("p2", "Ser Calmant", 120.50),
    ]
    # retrieved gol + preț negroundat în text (82.99, fără produse) → fără re-hidratare ar pica
    # validatorul → «n-am găsit». Re-hidratarea aduce produsele care groundează prețul. (Fără
    # superlativ în reply — NX-117 l-ar respinge ca claim; testăm doar grounding-ul de preț.)
    llm = FakeLLM(tool_calls=[], final="Îți recomand Crema Hidratantă la 82.99 lei.")
    await agent_stage(ctx, _deps(llm))

    assert ctx.reply is not None and "n-am găsit" not in ctx.reply.text.lower()
    assert "82.99" in ctx.reply.text  # preț groundat acum, din produsele re-hidratate
    assert ctx.retrieval is not None and len(ctx.retrieval.products) == 2  # re-hidratate din state
    assert ctx.reply.products and ctx.reply.products[0]["name"] == "Crema Hidratantă"


async def test_no_rehydrate_when_state_empty(monkeypatch):
    """Fără produse afișate în state → R3 nu se aplică; comportament neschimbat («n-am găsit»)."""

    async def boom(conn, business_id, ids, **k):
        raise AssertionError("get_products_by_ids NU trebuie chemat fără displayed_products")

    monkeypatch.setattr("src.worker.stages.agent.get_products_by_ids", boom)
    ctx = _ctx(body="care e cea mai bună?")  # state.displayed_products gol (default)
    llm = FakeLLM(tool_calls=[], final="")
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


async def test_sales_no_products_invented_price_uses_sales_fallback(monkeypatch):
    # G7-3 regresie: SALES fără produse + preț inventat → mesaj de VÂNZARE, NU fallback de comandă.
    _patch_search(monkeypatch, [])
    ctx = _ctx()
    llm = FakeLLM(
        tool_calls=[("search_products", {"query": "x", "price_max": None, "limit": 6})],
        final="Avem Crema X la 999 lei",
    )
    await agent_stage(ctx, _deps(llm))
    assert ctx.reply is not None
    assert "999" not in ctx.reply.text  # prețul inventat nu ajunge la client
    assert "n-am găsit" in ctx.reply.text.lower()  # mesaj de vânzare
    assert "comand" not in ctx.reply.text.lower()  # NU fallback-ul de status comandă


async def test_sales_no_products_clarify_served_verbatim(monkeypatch):
    # SALES fără produse + text fără preț (clarificare) → servit ca atare (zero regresie).
    _patch_search(monkeypatch, [])
    ctx = _ctx()
    llm = FakeLLM(
        tool_calls=[("search_products", {"query": "x", "price_max": None, "limit": 6})],
        final="Ce tip de ten cauți?",
    )
    await agent_stage(ctx, _deps(llm))
    assert ctx.reply is not None and ctx.reply.text == "Ce tip de ten cauți?"


# --- NX-78: prompt generat din DB --------------------------------------------


async def test_agent_uses_generated_prompt_with_business_vertical(monkeypatch):
    """Mock LLM capturează system-ul primit → conține verticalul businessului de test
    (din DB, nu „beauty" hardcodat) + categoriile încărcate."""
    _patch_search(monkeypatch, PRODUCTS)
    captured = {}

    class _CapLLM(FakeLLM):
        async def run_tool_loop(self, system, user, tools, execute, *, max_steps=3, model=None):
            captured["system"] = system
            return await super().run_tool_loop(
                system, user, tools, execute, max_steps=max_steps, model=model
            )

    ctx = _ctx()
    ctx.business.vertical = "hvac"  # tenant non-beauty
    llm = _CapLLM(
        tool_calls=[("search_products", {"query": "x", "price_max": None, "limit": 6})],
        final="Îți recomand Crema Hidratantă la 82.99 lei.",
    )
    await agent_stage(ctx, _deps(llm))
    assert "hvac" in captured["system"] and "beauty" not in captured["system"]
    assert "Creme" in captured["system"]  # categorii din stub (DB)


async def test_db_failure_loading_prompt_falls_back_to_echo(monkeypatch):
    """Failure case (card): query categorii aruncă → prins în try-ul buclei → echo (P6),
    nicio excepție propagată, agentul nu setează reply."""

    async def boom(conn, business_id):
        raise RuntimeError("DB down")

    monkeypatch.setattr(agent_mod, "list_category_names", boom)
    ctx = _ctx()
    llm = FakeLLM(tool_calls=[], final="orice")
    await agent_stage(ctx, _deps(llm))
    assert ctx.reply is None  # no-op → fallback_stage (echo) preia mai târziu


async def test_cart_add_state_patch_accumulated_in_ctx(monkeypatch):
    # NX-79: execute (callback-ul buclei) acumulează ToolResult.state_patch în ctx.state_patch
    # → processor-ul îl persistă în conversations.state.cart (path-ul de scriere a state-ului).
    from src.tools import commerce_tools as ctools

    async def fake_by_ids(conn, business_id, ids, **k):
        return [PRODUCTS[0]]  # p1, in_stock, 82.99

    monkeypatch.setattr(ctools, "get_products_by_ids", fake_by_ids)
    ctx = _ctx()
    llm = FakeLLM(tool_calls=[("cart_add", {"product_id": "p1", "quantity": 2})], final="ok")
    await agent_stage(ctx, _deps(llm))
    assert ctx.state_patch == {
        "cart": [
            {
                "product_id": "p1",
                "variant_id": None,
                "name": "Crema Hidratantă",
                "price": 82.99,
                "quantity": 2,
            }
        ]
    }


async def test_bare_number_hallucination_triggers_retry_and_event(monkeypatch):
    """NX-91: text brut cu o cifră bare halucinată (89 ∉ retrieval, fără valută) → validatorul
    o prinde → retry de recompunere → reply curat (fără 89) + event validator_rejected (doar n)."""
    _patch_search(monkeypatch, PRODUCTS)  # PRODUCTS: 82.99 / 120.50, NU 89
    ctx = _ctx()
    llm = FakeLLM(
        tool_calls=[("search_products", {"query": "cremă", "price_max": None, "limit": 6})],
        final="Crema costă 89 super ok",  # 89 negroundat, fără „lei"
        retry="Îți recomand Crema Hidratantă, bună pentru hidratare.",  # recompunere fără cifre
    )
    await agent_stage(ctx, _deps(llm))

    assert ctx.reply is not None and "89" not in ctx.reply.text  # halucinația nu ajunge la client
    assert llm.complete_calls == 1  # s-a declanșat retry-ul de recompunere
    ev = next((e for e in ctx.events if e.type == "validator_rejected"), None)
    assert ev is not None and ev.properties == {"kind": "bare_number", "n": 1}
    assert "text" not in ev.properties and "reply" not in ev.properties  # P12: zero corp reply
