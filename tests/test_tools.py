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
    """LLM stub care numără apelurile `embed` (spy pentru calea semantică vs SQL-only)."""

    def __init__(self):
        self.embed_calls = 0

    async def embed(self, texts, *, model=None):
        self.embed_calls += 1
        return [[0.0] * 8 for _ in texts]


class _RaisingLLM:
    """`embed` aruncă (pică rețeaua/API) → tool-ul trebuie să cadă pe SQL-only, nu să tacă."""

    async def embed(self, texts, *, model=None):
        raise RuntimeError("embed down")


async def _has_emb_true(conn, business_id):
    return True


async def _has_emb_false(conn, business_id):
    return False


def _ctx() -> TurnContext:
    return TurnContext(
        turn_id="t",
        business=BusinessConfig(id="biz-1", slug="s", name="n"),
        contact=Contact(id="c", business_id="biz-1"),
        message=InboundMessage(provider_msg_id="m", body="x"),
        conversation_id="conv",
    )


def _ctx_beauty() -> TurnContext:
    """Ctx cu vertical=beauty (NX-72) — taxonomia concern→cheie are tabel doar pentru beauty."""
    return TurnContext(
        turn_id="t",
        business=BusinessConfig(id="biz-1", slug="s", name="n", vertical="beauty"),
        contact=Contact(id="c", business_id="biz-1"),
        message=InboundMessage(provider_msg_id="m", body="x"),
        conversation_id="conv",
    )


def _deps(llm=None) -> PipelineDeps:
    return PipelineDeps(conn=object(), redis=None, llm=llm or _LLM())


def _deps_no_llm() -> PipelineDeps:
    """Deps fără LLM (cheie OpenAI absentă) — forțează calea SQL-only."""
    return PipelineDeps(conn=object(), redis=None, llm=None)


def test_enabled_tools_phase1_and_2():
    # Faza 1 (read) + Faza 2 (comerț): checkout_link + faq_lookup (NX-74) + cart_add/reorder/
    # subscribe_back_in_stock (NX-79/80). Importul explicit garantează înregistrarea.
    import src.tools.commerce_tools  # noqa: F401 — populează TOOL_REGISTRY cu tool-urile de comerț

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


def _search_event(ctx):
    """Ultimul event `product_search` emis (pentru aserții pe mode/PII)."""
    return next(e for e in reversed(ctx.events) if e.type == "product_search")


async def test_search_products_tool(monkeypatch):
    """NX-113b: HIBRID — ambele retrievere rulează MEREU (nu XOR), pe pool, apoi fuziune RRF."""
    captured = {}

    async def fake_search(conn, business_id, vec, **k):
        captured["business_id"] = business_id  # business_id vine din ctx, nu din args
        captured["sem_pool"] = k.get("pool")
        return PRODUCTS

    async def fake_sql(conn, business_id, **k):  # lexical rulează ȘI el (hibrid)
        captured["lex_pool"] = k.get("pool")
        return []

    monkeypatch.setattr(ct, "has_embeddings", _has_emb_true)
    monkeypatch.setattr(ct, "search_products_semantic", fake_search)
    monkeypatch.setattr(ct, "search_products_lexical", fake_sql)
    ctx, llm = _ctx(), _LLM()
    res = await run_tool(ctx, _deps(llm), "search_products", {"query": "cremă", "limit": 6})
    assert res.ok and len(res.products) == 2
    assert "Crema A" in res.llm_view and "[p1]" in res.llm_view
    assert captured["business_id"] == "biz-1"
    assert captured["lex_pool"] == 50 and captured["sem_pool"] == 50  # pool de fuziune, nu 6
    assert llm.embed_calls == 1  # un singur embedding (P2)
    ev = _search_event(ctx)
    assert ev.properties["mode"] == "semantic" and ev.properties["count"] == 2
    assert "query" not in ev.properties  # P12 — fără PII în analytics


async def test_search_fuses_both_retrievers(monkeypatch):
    """RRF: produsul prezent în AMBELE liste urcă peste cel prezent doar într-una."""
    p_only_vec = {**PRODUCTS[0], "id": "vec-only"}
    p_both = {**PRODUCTS[1], "id": "both"}

    async def fake_search(conn, business_id, vec, **k):  # vector: [vec-only(1), both(2)]
        return [p_only_vec, p_both]

    async def fake_lex(conn, business_id, **k):  # lexical: [both(1)]
        return [p_both]

    monkeypatch.setattr(ct, "has_embeddings", _has_emb_true)
    monkeypatch.setattr(ct, "search_products_semantic", fake_search)
    monkeypatch.setattr(ct, "search_products_lexical", fake_lex)
    ctx = _ctx()
    res = await run_tool(ctx, _deps(_LLM()), "search_products", {"query": "x"})
    # both: 1/(60+2)+1/(60+1) > vec-only: 1/(60+1) → „both" primul
    assert [p["id"] for p in res.products] == ["both", "vec-only"]
    assert _search_event(ctx).properties["mode"] == "semantic"


async def test_search_dedups_displayed_products(monkeypatch):
    """Dedup vs `state.displayed_products` ÎNAINTE de trunchiere (paritate „arată altele", P8)."""
    from src.models import ProductRef

    async def fake_lex(conn, business_id, **k):
        return PRODUCTS  # p1 + p2

    monkeypatch.setattr(ct, "has_embeddings", _has_emb_false)
    monkeypatch.setattr(ct, "search_products_lexical", fake_lex)
    ctx = _ctx()
    ctx.state.displayed_products = [ProductRef("p1", "Crema A", 82.99)]  # p1 deja arătat
    res = await run_tool(ctx, _deps(_LLM()), "search_products", {"query": "x"})
    assert [p["id"] for p in res.products] == ["p2"]  # p1 exclus (deja afișat)


async def test_search_dedup_before_truncate(monkeypatch):
    """Dedup ÎNAINTE de trunchiere: cu >6 candidați și un produs afișat în top-6, al 7-lea trebuie
    să apară (truncate-first l-ar pierde). Regression-guard pe ordinea cerută de card."""
    from src.models import ProductRef

    seven = [{"id": f"q{i}", "name": f"P{i}", "price": 10.0 + i} for i in range(7)]

    async def fake_lex(conn, business_id, **k):
        return seven

    monkeypatch.setattr(ct, "has_embeddings", _has_emb_false)
    monkeypatch.setattr(ct, "search_products_lexical", fake_lex)
    ctx = _ctx()
    ctx.state.displayed_products = [ProductRef("q0", "P0", 10.0)]  # rank-1 deja afișat
    res = await run_tool(ctx, _deps(_LLM()), "search_products", {"query": "x"})
    ids = [p["id"] for p in res.products]
    assert "q0" not in ids and "q6" in ids and len(ids) == 6  # q6 supraviețuiește (dedup întâi)


async def test_search_all_displayed_is_graceful_empty(monkeypatch):
    """Card Edge: TOȚI candidații deja în displayed_products → după dedup gol → răspuns gol grațios
    (P6, fără tăcere), nu negare de brand (fără brand cerut)."""
    from src.models import ProductRef

    async def fake_lex(conn, business_id, **k):
        return PRODUCTS  # p1 + p2

    monkeypatch.setattr(ct, "has_embeddings", _has_emb_false)
    monkeypatch.setattr(ct, "search_products_lexical", fake_lex)
    ctx = _ctx()
    ctx.state.displayed_products = [
        ProductRef("p1", "Crema A", 82.99),
        ProductRef("p2", "Ser B", 1.0),
    ]
    res = await run_tool(ctx, _deps(_LLM()), "search_products", {"query": "x"})
    assert res.ok and res.products == [] and "Niciun produs" in res.llm_view
    assert _search_event(ctx).properties["count"] == 0


async def test_search_tool_threads_price_sort_into_fusion(monkeypatch):
    """sort_mode=price_asc trebuie să ajungă în fuziune → re-sort pe preț, NU RRF (mutation-guard:
    dacă sort_mode nu e threaded, „cheap" (în ambele liste) ar urca prin RRF, nu prin preț)."""
    cheap = {"id": "cheap", "name": "Cheap", "price": 10.0}
    mid = {"id": "mid", "name": "Mid", "price": 50.0}
    expensive = {"id": "expensive", "name": "Exp", "price": 90.0}

    async def fake_sem(conn, business_id, vec, **k):
        return [cheap, expensive]  # vector

    async def fake_lex(conn, business_id, **k):
        return [mid, cheap]  # lexical (cheap în ambele)

    monkeypatch.setattr(ct, "has_embeddings", _has_emb_true)
    monkeypatch.setattr(ct, "search_products_semantic", fake_sem)
    monkeypatch.setattr(ct, "search_products_lexical", fake_lex)
    ctx = _ctx()
    res = await run_tool(
        ctx, _deps(_LLM()), "search_products", {"query": "x", "sort_mode": "price_asc"}
    )
    assert [p["id"] for p in res.products] == ["cheap", "mid", "expensive"]  # pe preț, nu RRF


async def test_search_brand_present_all_displayed_no_false_denial(monkeypatch):
    """Fix NX-113b: brand PREZENT dar tot ce avea e deja afișat → răspuns gol grațios, NU negarea
    falsă „nu lucrăm cu brandul X" (ar fi dezinformare CAT-001 în sens invers)."""
    from src.models import ProductRef

    async def fake_lex(conn, business_id, **k):
        return [PRODUCTS[0]]  # p1 = de la BrandA, dar deja afișat

    monkeypatch.setattr(ct, "has_embeddings", _has_emb_false)
    monkeypatch.setattr(ct, "search_products_lexical", fake_lex)
    ctx = _ctx()
    ctx.state.displayed_products = [ProductRef("p1", "Crema A", 82.99)]
    res = await run_tool(ctx, _deps(_LLM()), "search_products", {"query": "x", "brand": "BrandA"})
    assert res.ok and res.products == []
    assert "Nu am găsit niciun produs de la brandul" not in res.llm_view  # fără negare falsă
    assert "Niciun produs" in res.llm_view  # răspuns gol normal (P6)


async def test_search_brand_truly_absent_still_denies(monkeypatch):
    """Contra-test: brand chiar ABSENT (zero match real) → negarea brandului rămâne (CAT-001)."""

    async def fake_lex(conn, business_id, **k):
        return []  # niciun produs de la brand

    monkeypatch.setattr(ct, "has_embeddings", _has_emb_false)
    monkeypatch.setattr(ct, "search_products_lexical", fake_lex)
    ctx = _ctx()
    res = await run_tool(ctx, _deps(_LLM()), "search_products", {"query": "x", "brand": "Chanel"})
    assert res.ok and res.products == []
    assert "Nu am găsit niciun produs de la brandul «Chanel»" in res.llm_view


async def test_search_mode_lexical_when_all_vector_deduped(monkeypatch):
    """Fix NX-113b: mode=lexical dacă toate hiturile vector sunt eliminate de dedup (deși vectorul
    a întors ceva) — analytics-ul reflectă setul ÎNTORS, nu ce-a întors vectorul brut."""
    from src.models import ProductRef

    async def fake_sem(conn, business_id, vec, **k):
        return [PRODUCTS[0]]  # vector întoarce p1 ...

    async def fake_lex(conn, business_id, **k):
        return [PRODUCTS[1]]  # lexical întoarce p2

    monkeypatch.setattr(ct, "has_embeddings", _has_emb_true)
    monkeypatch.setattr(ct, "search_products_semantic", fake_sem)
    monkeypatch.setattr(ct, "search_products_lexical", fake_lex)
    ctx = _ctx()
    ctx.state.displayed_products = [ProductRef("p1", "Crema A", 82.99)]  # ... dar p1 e deja afișat
    res = await run_tool(ctx, _deps(_LLM()), "search_products", {"query": "x"})
    assert [p["id"] for p in res.products] == ["p2"]  # doar lexical supraviețuiește
    assert _search_event(ctx).properties["mode"] == "lexical"  # nu „semantic", deși vector a întors


async def test_search_no_embeddings_falls_back_sql_only(monkeypatch):
    """Tenant fără embeddings → lexical-only direct, ZERO apel embed, mode=lexical."""
    monkeypatch.setattr(ct, "has_embeddings", _has_emb_false)

    async def fake_sql(conn, business_id, **k):
        return PRODUCTS

    async def boom_semantic(*a, **k):  # nu trebuie atins
        raise AssertionError("calea semantică nu trebuie chemată fără embeddings")

    monkeypatch.setattr(ct, "search_products_lexical", fake_sql)
    monkeypatch.setattr(ct, "search_products_semantic", boom_semantic)
    ctx, llm = _ctx(), _LLM()
    res = await run_tool(ctx, _deps(llm), "search_products", {"query": "cremă"})
    assert res.ok and len(res.products) == 2
    assert llm.embed_calls == 0  # SQL-only n-are LLM deloc (cost $0)
    assert _search_event(ctx).properties["mode"] == "lexical"


async def test_search_no_llm_sql_only(monkeypatch):
    """Fără LLM (cheie absentă) → SQL-only direct; `has_embeddings` nici nu se evaluează."""

    async def boom_has_emb(conn, business_id):
        raise AssertionError("has_embeddings nu trebuie chemat fără LLM (short-circuit)")

    async def fake_sql(conn, business_id, **k):
        return PRODUCTS

    monkeypatch.setattr(ct, "has_embeddings", boom_has_emb)
    monkeypatch.setattr(ct, "search_products_lexical", fake_sql)
    ctx = _ctx()
    res = await run_tool(ctx, _deps_no_llm(), "search_products", {"query": "x"})
    assert res.ok and len(res.products) == 2
    assert _search_event(ctx).properties["mode"] == "lexical"


async def test_search_semantic_empty_falls_back_sql(monkeypatch):
    """Embeddings prezente dar semantic gol (chiar și fără preț) → cade pe SQL-only."""
    monkeypatch.setattr(ct, "has_embeddings", _has_emb_true)

    async def empty_semantic(conn, business_id, vec, **k):
        return []

    async def fake_sql(conn, business_id, **k):
        return PRODUCTS

    monkeypatch.setattr(ct, "search_products_semantic", empty_semantic)
    monkeypatch.setattr(ct, "search_products_lexical", fake_sql)
    ctx, llm = _ctx(), _LLM()
    res = await run_tool(ctx, _deps(llm), "search_products", {"query": "x", "price_max": 50})
    assert res.ok and len(res.products) == 2
    assert llm.embed_calls == 1  # a încercat semantic o dată, apoi a căzut
    assert _search_event(ctx).properties["mode"] == "lexical"


async def test_search_embed_error_falls_back_sql(monkeypatch):
    """`embed` aruncă deși există embeddings → prins, cade pe SQL-only, NU propagă (P6)."""
    monkeypatch.setattr(ct, "has_embeddings", _has_emb_true)

    async def fake_sql(conn, business_id, **k):
        return PRODUCTS

    monkeypatch.setattr(ct, "search_products_lexical", fake_sql)
    ctx = _ctx()
    res = await run_tool(ctx, _deps(_RaisingLLM()), "search_products", {"query": "x"})
    assert res.ok and len(res.products) == 2
    assert _search_event(ctx).properties["mode"] == "lexical"


async def test_search_all_empty_is_graceful(monkeypatch):
    """Și semantic și SQL-only goale → ToolResult ok cu listă goală + „Niciun produs" (P6)."""
    monkeypatch.setattr(ct, "has_embeddings", _has_emb_true)

    async def empty(conn, business_id, *a, **k):
        return []

    monkeypatch.setattr(ct, "search_products_semantic", empty)
    monkeypatch.setattr(ct, "search_products_lexical", empty)
    ctx = _ctx()
    res = await run_tool(ctx, _deps(_LLM()), "search_products", {"query": "zzz"})
    assert res.ok and res.products == [] and "Niciun produs" in res.llm_view
    assert _search_event(ctx).properties["mode"] == "lexical"


# --- NX-72: filtre concern/category/brand + relaxare progresivă --------------


async def test_search_maps_concerns_and_passes_filters_semantic(monkeypatch):
    """concerns liberi → chei canonice; category/concerns ajung la AMBELE retrievere (paritate)."""
    sem_calls, lex_calls = [], []

    async def fake_search(conn, business_id, vec, **k):
        sem_calls.append(k)
        return PRODUCTS

    async def fake_lex(conn, business_id, **k):  # NX-113b: lexical rulează ȘI el (hibrid)
        lex_calls.append(k)
        return []

    monkeypatch.setattr(ct, "has_embeddings", _has_emb_true)
    monkeypatch.setattr(ct, "search_products_semantic", fake_search)
    monkeypatch.setattr(ct, "search_products_lexical", fake_lex)
    ctx = _ctx_beauty()
    res = await run_tool(
        ctx,
        _deps(_LLM()),
        "search_products",
        {"query": "cremă", "concerns": ["ten gras"], "category": "creme-fata"},
    )
    assert res.ok and len(res.products) == 2
    assert sem_calls[0]["concerns"] == ["oily"]  # „ten gras" → „oily"
    assert sem_calls[0]["category"] == "creme-fata"
    assert lex_calls[0]["concerns"] == ["oily"]  # același mapping pe lexical (paritate filtre)
    assert lex_calls[0]["category"] == "creme-fata"
    ev = _search_event(ctx)
    assert ev.properties["n_concerns"] == 1
    assert ev.properties["had_category"] is True and ev.properties["relaxed"] is False
    assert "query" not in ev.properties and "concerns" not in ev.properties  # P12


async def test_search_sql_only_gets_mapped_concerns_and_brand(monkeypatch):
    """Fără embeddings → SQL-only primește category/brand/concerns mapate (paritate)."""
    calls = []

    async def fake_sql(conn, business_id, **k):
        calls.append(k)
        return PRODUCTS

    monkeypatch.setattr(ct, "has_embeddings", _has_emb_false)
    monkeypatch.setattr(ct, "search_products_lexical", fake_sql)
    ctx = _ctx_beauty()
    res = await run_tool(
        ctx,
        _deps(_LLM()),
        "search_products",
        {"query": "x", "concerns": ["piele sensibilă"], "brand": "BrandA"},
    )
    assert res.ok and len(res.products) == 2
    assert calls[0]["concerns"] == ["sensitive"] and calls[0]["brand"] == "BrandA"
    assert _search_event(ctx).properties["had_brand"] is True


async def test_search_unknown_concern_no_false_filter(monkeypatch):
    """concern necunoscut taxonomiei → fără condiție de concern (n_concerns=0), nu golește."""
    calls = []

    async def fake_sql(conn, business_id, **k):
        calls.append(k)
        return PRODUCTS

    monkeypatch.setattr(ct, "has_embeddings", _has_emb_false)
    monkeypatch.setattr(ct, "search_products_lexical", fake_sql)
    ctx = _ctx_beauty()
    res = await run_tool(
        ctx, _deps(_LLM()), "search_products", {"query": "x", "concerns": ["frigider"]}
    )
    assert res.ok and len(res.products) == 2
    assert calls[0]["concerns"] is None  # necunoscut → niciun filtru
    assert _search_event(ctx).properties["n_concerns"] == 0


async def test_search_progressive_relaxation(monkeypatch):
    """Filtre dure golesc tot → relaxăm SOFTUL (concerns), dar PREȚUL rămâne fixat
    (ARCH-product-retrieval cauza #3: nu mai scoatem bound-ul de buget). relaxed=True."""
    calls = []

    async def fake_sql(conn, business_id, **k):
        calls.append(k)
        # Întoarce produse DOAR când nu mai e niciun filtru de concern (după relaxare).
        return PRODUCTS if not k.get("concerns") else []

    monkeypatch.setattr(ct, "has_embeddings", _has_emb_false)
    monkeypatch.setattr(ct, "search_products_lexical", fake_sql)
    ctx = _ctx_beauty()
    res = await run_tool(
        ctx,
        _deps(_LLM()),
        "search_products",
        {"query": "x", "concerns": ["ten gras"], "price_max": 50},
    )
    assert res.ok and len(res.products) == 2
    # ladder NOU: {price+concern} → {price, fără concern}; prețul (50) rămâne fixat.
    assert calls[-1]["concerns"] is None and calls[-1]["price_max"] == 50
    assert _search_event(ctx).properties["relaxed"] is True


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
