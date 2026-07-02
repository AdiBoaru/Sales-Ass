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
from src.worker.stages.agent import (
    _COMPARE_RE,
    _budget,
    _links_ok,
    _prices_ok,
    _rich_bundle,
    agent_stage,
)


@pytest.fixture(autouse=True)
def _stub_prompt_inputs(monkeypatch):
    """NX-78: agent_stage citește categorii/aliase din DB pt promptul generat. Testele folosesc
    `conn=object()` → stubbim cele două query-uri (prompt generic, fără DB reală)."""

    async def _cats(conn, business_id):
        return ["Creme", "Parfumuri"]

    async def _aliases(conn, business_id, **k):
        return []

    # #7b: cart_add declanșează un lookup de produse complementare (DB). Default → fără
    # complementare (cross-sell cade în flux normal); testele de cross-sell îl mock-uiesc separat.
    async def _no_complementary(conn, business_id, anchor_id, **k):
        return []

    monkeypatch.setattr(agent_mod, "list_category_names", _cats)
    monkeypatch.setattr(agent_mod, "list_routing_aliases", _aliases)
    monkeypatch.setattr(agent_mod, "get_complementary_products", _no_complementary)


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


def test_rich_bundle_includes_description():
    # PR-3: bundle-ul rich duce ai_summary (componente reale) → modelul scrie fit SPECIFIC.
    out = _rich_bundle(
        [
            {
                "id": "p1",
                "name": "Ser X",
                "price": 99.0,
                "rating": 4.5,
                "top_pros": ["bun"],
                "ai_summary": "cu acid hialuronic, pentru ten uscat",
            }
        ]
    )
    assert "[p1]" in out and "descriere: cu acid hialuronic, pentru ten uscat" in out


def test_rich_bundle_omits_empty_description():
    out = _rich_bundle([{"id": "p1", "name": "Ser X", "price": 99.0, "top_pros": ["bun"]}])
    assert "descriere" not in out  # date sărace → fără segment gol


def test_rich_bundle_includes_facets():
    # Tier 2b: fațetele STRUCTURATE (din attributes) ajung în bundle → model scrie fit grounded.
    from src.domain.pack import FacetSpec

    facets = (
        FacetSpec(key="key_ingredients", labels={"ro": "Ingrediente cheie"}),
        FacetSpec(key="concerns", labels={"ro": "Potrivit pentru"}),
    )
    out = _rich_bundle(
        [
            {
                "id": "p1",
                "name": "Ser X",
                "price": 99.0,
                "top_pros": ["bun"],
                "attributes": {
                    "key_ingredients": ["acid hialuronic", "niacinamidă"],
                    "concerns": ["ten uscat"],
                },
            }
        ],
        facets,
        "ro",
    )
    assert "fațete:" in out
    assert "Ingrediente cheie: acid hialuronic, niacinamidă" in out
    assert "Potrivit pentru: ten uscat" in out


def test_rich_bundle_no_facets_unchanged():
    # fără facets pasate (default) → segment absent (back-compat cu apelurile vechi).
    out = _rich_bundle(
        [
            {
                "id": "p1",
                "name": "Ser X",
                "price": 99.0,
                "top_pros": ["bun"],
                "attributes": {"key_ingredients": ["x"]},
            }
        ]
    )
    assert "fațete" not in out


def test_compare_re_matches_intent_not_face():
    # IZI-parity G2: ÎNALTĂ PRECIZIE — verbul de comparație (RO/EN/HU) + vs/versus; generic.
    assert _COMPARE_RE.search("Compară-mi primele două")
    assert _COMPARE_RE.search("Compară-mi primele două variante Velvet")
    assert _COMPARE_RE.search("compare these two")
    assert _COMPARE_RE.search("Crema A vs Crema B")
    assert _COMPARE_RE.search("hasonlítsd össze a kettőt")
    # fals-pozitive de evitat (gate-ul n-are recurs la model): policy „diferența", „compartiment",
    # „față" (zona feței). Frazele laxe de tip „ce diferență" cad intenționat pe calea model-driven.
    assert not _COMPARE_RE.search("care e diferența dintre garanție și retur")
    assert not _COMPARE_RE.search("are un compartiment mare")
    assert not _COMPARE_RE.search("recomandă-mi un ser pentru față")
    assert not _COMPARE_RE.search("vreau ceva pentru ten gras")


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


# --- NX-137: eșec de comerț → nota NB în compunerea rich ----------------------


async def test_finalize_rich_notes_reach_user():
    """`notes` (NX-137) devine linie „NB:" în mesajul user al compunerii; fără notes → absentă."""
    captured: list[str] = []

    class _CapSchemaLLM(FakeLLM):
        async def complete_schema(self, system, user, schema, *, model=None):
            captured.append(user)
            raise RuntimeError("stop")  # nu testăm assemble-ul, doar prompt-ul compus

    ctx = _ctx()
    out = await agent_mod._finalize_rich(
        _CapSchemaLLM(), "sys", "vreau o cremă", PRODUCTS, ctx, "", notes="fără chips de coș"
    )
    assert out is None  # excepție la apel → fallback pe proză (comportament existent)
    assert "NB: fără chips de coș" in captured[0]

    await agent_mod._finalize_rich(_CapSchemaLLM(), "sys", "vreau o cremă", PRODUCTS, ctx, "")
    assert "NB:" not in captured[1]  # fără notes → prompt byte-identic cu înainte


async def test_checkout_link_attached_as_offer(monkeypatch):
    """NX-137 root cause (găsit live pe sim): checkout_link REUȘEA (link creat în DB), dar linkul
    nu ajungea la client — regulile rich interzic linkuri în proza modelului. Fix: Offer(open_url)
    atașat pe reply → buton pe web, URL în text pe canalele fără CTA (floor-ul din set_offer)."""
    import src.tools.commerce_tools as cm

    async def fake_by_ids(conn, business_id, ids, *, limit=6):
        return [p for p in PRODUCTS if p["id"] in set(ids)]

    async def fake_create(conn, business_id, conversation_id, contact_id, ref, cart, url, exp):
        return {"id": "cl1", "ref_code": ref, "url": url}

    monkeypatch.setattr(cm, "get_products_by_ids", fake_by_ids)
    monkeypatch.setattr(cm, "create_checkout_link", fake_create)

    ctx = _ctx(body="da, cumpăr prima, dă-mi checkout")
    ctx.business = BusinessConfig(
        id="b", slug="d", name="D", settings={"checkout_url": "https://shop.example/checkout"}
    )
    llm = FakeLLM(
        tool_calls=[
            (
                "checkout_link",
                {"cart_items": [{"product_id": "p1", "variant_id": None, "quantity": 1}]},
            )
        ],
        final="",
    )
    await agent_stage(ctx, _deps(llm))

    assert ctx.reply is not None and ctx.reply.offer is not None
    assert ctx.reply.offer.kind == "open_url"
    assert ctx.reply.offer.url == "https://shop.example/checkout?ref=t"
    # floor-ul (canale fără buton): URL-ul e garantat și în text, o singură dată
    assert ctx.reply.text.count("https://shop.example/checkout?ref=t") == 1
    assert any(e.type == "checkout_offer_attached" for e in ctx.events)


async def test_purchase_intent_checkout_fallback(monkeypatch):
    """NX-137 (găsit live pe sim): la „adaugă în coș și dă-mi link de plată" modelul cheamă doar
    cart_add, iar turul e deturnat de cross-sell — fără link. Cu purchase_intent + coș + fără link
    creat de model → CODUL cheamă checkout_link determinist și atașează Offer-ul."""
    import src.tools.commerce_tools as cm

    async def fake_by_ids(conn, business_id, ids, *, limit=6):
        return [p for p in PRODUCTS if p["id"] in set(ids)]

    async def fake_create(conn, business_id, conversation_id, contact_id, ref, cart, url, exp):
        return {"id": "cl1", "ref_code": ref, "url": url}

    monkeypatch.setattr(cm, "get_products_by_ids", fake_by_ids)
    monkeypatch.setattr(cm, "create_checkout_link", fake_create)

    ctx = _ctx(body="da, adaugă primul în coș și dă-mi linkul de plată")
    ctx.business = BusinessConfig(
        id="b", slug="d", name="D", settings={"checkout_url": "https://shop.example/checkout"}
    )
    ctx.route = RouteDecision(route=Route.SALES, purchase_intent=True)
    # modelul cheamă DOAR cart_add (non-compliance) — codul trebuie să completeze checkout-ul
    llm = FakeLLM(
        tool_calls=[("cart_add", {"product_id": "p1", "variant_id": None, "quantity": 1})],
        final="",
    )
    await agent_stage(ctx, _deps(llm))

    assert ctx.reply is not None and ctx.reply.offer is not None
    assert ctx.reply.offer.url == "https://shop.example/checkout?ref=t"
    assert any(e.type == "checkout_intent_fallback" for e in ctx.events)
    # checkout creat → cross-sell NU deturnează turul (generated_links îl blochează)
    assert not any(e.type == "cross_sell" for e in ctx.events)


async def test_purchase_intent_no_cart_no_fallback(monkeypatch):
    """purchase_intent fără coș (nicio linie) → fallback-ul NU inventează un checkout gol."""
    _patch_search(monkeypatch, PRODUCTS)
    ctx = _ctx(body="vreau să cumpăr ceva bun")
    ctx.route = RouteDecision(route=Route.SALES, purchase_intent=True)
    llm = FakeLLM(
        tool_calls=[("search_products", {"query": "cremă", "price_max": None, "limit": 6})],
        final="",
    )
    await agent_stage(ctx, _deps(llm))
    assert ctx.reply is not None and ctx.reply.offer is None
    assert not any(e.type == "checkout_intent_fallback" for e in ctx.events)


async def test_failed_checkout_injects_commerce_note(monkeypatch):
    """NX-137 e2e: checkout_link eșuat (no_checkout_url) în același tur → compunerea rich
    primește NB-ul anti-contradicție (fără chips «adaugă în coș» sub un mesaj care refuză coșul)."""
    import src.tools.commerce_tools as cm  # populează TOOL_REGISTRY cu checkout_link

    _patch_search(monkeypatch, PRODUCTS)
    monkeypatch.setattr(cm.get_settings(), "checkout_base_url", "", raising=False)

    captured: list[str] = []

    class _CapSchemaLLM(FakeLLM):
        async def complete_schema(self, system, user, schema, *, model=None):
            captured.append(user)
            raise RuntimeError("stop")

    llm = _CapSchemaLLM(
        tool_calls=[
            ("search_products", {"query": "cremă", "price_max": None, "limit": 6}),
            (
                "checkout_link",
                {"cart_items": [{"product_id": "p1", "variant_id": None, "quantity": 1}]},
            ),
        ],
        final="",
    )
    ctx = _ctx(body="vreau să cumpăr crema")
    await agent_stage(ctx, _deps(llm))

    assert captured, "calea rich nu a rulat (products goale?)"
    assert "NB:" in captured[0] and "adaugă în coș" in captured[0]
    # eșecul e vizibil și în analytics (tool_call cu ok=False, error=no_checkout_url)
    ev = [
        e for e in ctx.events if e.type == "tool_call" and e.properties["name"] == "checkout_link"
    ]
    assert ev and ev[0].properties["ok"] is False
    assert ev[0].properties["error"] == "no_checkout_url"


async def test_compare_intent_serves_table_deterministically(monkeypatch):
    """IZI-parity G2: „compară primele două" pe un set deja afișat → tabel structurat DETERMINIST,
    fără bucla LLM (nu depinde de model să cheme compare_products). Agnostic de vertical."""

    async def fake_by_ids(conn, business_id, ids, *, limit=6):
        return [p for p in PRODUCTS if p["id"] in ids][:limit]

    monkeypatch.setattr(agent_mod, "get_products_by_ids", fake_by_ids)
    ctx = _ctx(body="compară primele două")
    ctx.state.displayed_products = [
        ProductRef(product_id="p1", name="Crema Hidratantă", price=82.99),
        ProductRef(product_id="p2", name="Ser Calmant", price=120.50),
    ]
    llm = FakeLLM(final="NU ar trebui folosit textul modelului")
    await agent_stage(ctx, _deps(llm))

    assert ctx.reply is not None and ctx.reply.comparison is not None
    assert len(ctx.reply.comparison.columns) == 2  # tabel pe primele 2 afișate, ordinea păstrată
    assert ctx.reply.comparison.columns[0].product_id == "p1"
    assert llm.complete_calls == 0  # determinist → ZERO inferență LLM


async def test_compare_intent_falls_through_when_under_two_displayed(monkeypatch):
    """<2 produse afișate → NU intră pe calea deterministă de comparație (lasă bucla LLM)."""

    async def fake_by_ids(conn, business_id, ids, *, limit=6):  # n-ar trebui chemat
        raise AssertionError("get_products_by_ids nu trebuie chemat sub 2 produse afișate")

    monkeypatch.setattr(agent_mod, "get_products_by_ids", fake_by_ids)
    _patch_search(monkeypatch, PRODUCTS)
    ctx = _ctx(body="compară-le")
    ctx.state.displayed_products = [
        ProductRef(product_id="p1", name="Crema Hidratantă", price=82.99)
    ]
    llm = FakeLLM(
        tool_calls=[("search_products", {"query": "x", "price_max": None, "limit": 6})],
        final="Îți recomand Crema Hidratantă la 82.99 lei.",
    )
    await agent_stage(ctx, _deps(llm))
    assert ctx.reply is not None and ctx.reply.comparison is None  # nu e tabel determinist


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
    assert ev is not None and ev.properties == {"kind": "bare_number", "n": 1, "turn_id": "t"}
    assert "text" not in ev.properties and "reply" not in ev.properties  # P12: zero corp reply


# --- NX-119b: ramura deterministă „mai arată-mi" (paginare fără bucla LLM) -----


class _NoLoopLLM(FakeLLM):
    """Eșuează dacă intră în bucla LLM — dovedește că show-more e DETERMINIST (fără inferență)."""

    async def run_tool_loop(self, *a, **k):
        raise AssertionError("show_more NU trebuie să cheme bucla LLM")


def _session_ctx(pool_n=8, cursor=6, shown=6):
    ctx = _ctx(body="mai arată-mi")
    ctx.state.active_search = {
        "filters": {"query": "creme"},
        "pool": [f"p{i}" for i in range(pool_n)],
        "cursor": cursor,
        "fp": "x",
        "page": 0,
    }
    ctx.state.displayed_products = [ProductRef(f"p{i}", f"P{i}", 10.0 + i) for i in range(shown)]
    return ctx


async def test_show_more_paginates_without_tool_loop(monkeypatch):
    ctx = _session_ctx(pool_n=8, cursor=6)

    async def fake_by_ids(conn, business_id, ids, **k):
        return [
            {
                "id": i,
                "name": i.upper(),
                "brand": "B",
                "price": 9.0,
                "url": "u",
                "ai_summary": "",
                "availability": "in_stock",
                "rating": 4.0,
            }
            for i in ids
        ]

    monkeypatch.setattr(ct, "get_products_by_ids", fake_by_ids)
    await agent_stage(ctx, _deps(_NoLoopLLM()))
    assert ctx.reply is not None
    assert [p["id"] for p in ctx.retrieval.products] == ["p6", "p7"]  # pagina următoare din pool
    assert ctx.state_patch["active_search"]["cursor"] == 8  # cursor avansat
    assert any(e.type == "show_more" and e.properties["served"] == 2 for e in ctx.events)


async def test_show_more_exhausted_returns_deterministic_msg(monkeypatch):
    ctx = _session_ctx(pool_n=6, cursor=6)  # tot pool-ul deja servit

    async def boom_by_ids(conn, business_id, ids, **k):
        raise AssertionError("nu hidratăm nimic pe pool epuizat")

    monkeypatch.setattr(ct, "get_products_by_ids", boom_by_ids)
    await agent_stage(ctx, _deps(_NoLoopLLM()))
    assert ctx.reply is not None and ctx.reply.cacheable is False
    assert "toate" in ctx.reply.text.lower()  # _no_more_msg(ro)
    assert any(e.type == "show_more" and e.properties["served"] == 0 for e in ctx.events)


async def test_show_more_no_session_falls_through_to_llm(monkeypatch):
    # „mai arată-mi" FĂRĂ sesiune activă → flux normal (bucla LLM rulează), nu crapă
    _patch_search(monkeypatch, PRODUCTS)
    ctx = _ctx(body="mai arată-mi")  # state.active_search = None (default)
    llm = FakeLLM(tool_calls=[("search_products", {"query": "creme"})], final="ok")
    await agent_stage(ctx, _deps(llm))
    assert ctx.reply is not None


async def test_show_more_with_refinement_falls_through_to_llm(monkeypatch):
    """Review NX-119b: „mai multe sub 50" pe sesiune activă DAR cu slot nou din triaj = RAFINARE →
    bucla LLM (sesiune rafinată), NU pagina sesiunii VECHI (p6/p7)."""
    _patch_search(monkeypatch, PRODUCTS)
    ctx = _session_ctx()  # sesiune activă, pool p0..p7
    ctx.message = InboundMessage(provider_msg_id="m", body="mai multe sub 50 lei")
    ctx.route.filters = {"budget_max": 50}  # triajul a extras o constrângere nouă
    llm = FakeLLM(tool_calls=[("search_products", {"query": "creme", "price_max": 50})], final="ok")
    await agent_stage(ctx, _deps(llm))
    assert ctx.reply is not None
    served = {p["id"] for p in (ctx.retrieval.products if ctx.retrieval else [])}
    assert "p6" not in served and "p7" not in served  # NU pagina sesiunii vechi (refinarea câștigă)


# --- Val3: _lead_score_hint (lead_score citit de agent) ----------------------


def _settings_lead(monkeypatch, *, enabled=True, threshold=70.0):
    from types import SimpleNamespace

    monkeypatch.setattr(
        agent_mod,
        "get_settings",
        lambda: SimpleNamespace(
            lead_score_hint_enabled=enabled, lead_score_high_threshold=threshold
        ),
    )


def test_lead_score_hint_high_injects_nudge(monkeypatch):
    _settings_lead(monkeypatch)
    ctx = _ctx()
    ctx.contact.lead_score = 80.0
    assert "intenție mare" in agent_mod._lead_score_hint(ctx)


def test_lead_score_hint_low_is_empty(monkeypatch):
    _settings_lead(monkeypatch)
    ctx = _ctx()
    ctx.contact.lead_score = 20.0
    assert agent_mod._lead_score_hint(ctx) == ""


def test_lead_score_hint_disabled_is_empty(monkeypatch):
    _settings_lead(monkeypatch, enabled=False)
    ctx = _ctx()
    ctx.contact.lead_score = 95.0
    assert agent_mod._lead_score_hint(ctx) == ""
