"""G7-1 — tool-uri de catalog + framework (run_tool/registry), fără DB/LLM real.

Query-urile de catalog sunt monkeypatch-uite; testăm: dispatch, validare args, vederile
compacte, izolarea (business_id din ctx), degradarea (tool inexistent / args invalide)."""

import asyncio
import inspect
import time

from src.config import get_settings
from src.db.queries.catalog import search_products_lexical, search_products_semantic
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


class _SlowLLM:
    """NX-225: furnizor LENT, nu mort — răspunde corect, dar târziu. `cancelled` confirmă că
    anularea de la `wait_for` a ajuns în corutină și că `CancelledError` NU scapă în tur."""

    def __init__(self, delay_s: float):
        self.delay_s = delay_s
        self.cancelled = False

    async def embed(self, texts, *, model=None):
        try:
            await asyncio.sleep(self.delay_s)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return [[0.0] * 8 for _ in texts]


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
    """Ctx cu DomainPack beauty (NX-124): maparea concern→cheie vine din `concern_map`."""
    from src.domain.pack import DomainPack
    from src.tools.taxonomy import _BEAUTY

    business = BusinessConfig(id="biz-1", slug="s", name="n", vertical="beauty")
    business.domain_pack = DomainPack(vertical="beauty_salon", concern_map=_BEAUTY)
    return TurnContext(
        turn_id="t",
        business=business,
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


def _events(ctx, type_):
    """Toate evenimentele de un tip (lista goală = semnalul NU s-a emis)."""
    return [e for e in ctx.events if e.type == type_]


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
    assert "stoc: in_stock" in res.llm_view  # NX-118: tokenul de availability în _brief
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


async def test_search_emit_observability_fields(monkeypatch):
    """NX-113c: emit are fused/pool-counts/relax_depth/zero_result/top_cosine_distance (P12)."""

    async def fake_sem(conn, business_id, vec, **k):
        return [
            {**PRODUCTS[0], "cosine_distance": 0.12},
            {**PRODUCTS[1], "cosine_distance": 0.30},
        ]

    async def fake_lex(conn, business_id, **k):
        return [PRODUCTS[0]]

    monkeypatch.setattr(ct, "has_embeddings", _has_emb_true)
    monkeypatch.setattr(ct, "search_products_semantic", fake_sem)
    monkeypatch.setattr(ct, "search_products_lexical", fake_lex)
    ctx = _ctx()
    await run_tool(ctx, _deps(_LLM()), "search_products", {"query": "x"})
    ev = _search_event(ctx)
    assert ev.properties["fused"] is True  # ambele retrievere au întors candidați
    assert ev.properties["lexical_pool"] == 1 and ev.properties["vector_pool"] == 2
    assert ev.properties["relax_depth"] == 0 and ev.properties["zero_result"] is False
    assert ev.properties["top_cosine_distance"] == 0.12  # cel mai apropiat (min distanță)
    assert "query" not in ev.properties and "concerns" not in ev.properties  # P12


async def test_search_emit_zero_result_and_unfused(monkeypatch):
    """zero_result=True + fused=False când nimic nu iese; vector_pool=0 fără embeddings."""

    async def empty(conn, business_id, *a, **k):
        return []

    monkeypatch.setattr(ct, "has_embeddings", _has_emb_false)
    monkeypatch.setattr(ct, "search_products_lexical", empty)
    ctx = _ctx()
    await run_tool(ctx, _deps(_LLM()), "search_products", {"query": "zzz"})
    ev = _search_event(ctx)
    assert ev.properties["zero_result"] is True and ev.properties["fused"] is False
    assert ev.properties["vector_pool"] == 0 and ev.properties["top_cosine_distance"] is None


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


async def test_named_product_found_helper():
    """A1 unit: cele mai lungi tokenuri distinctive trebuie să apară într-un produs întors."""
    assert ct._named_product_found("Hidra Boost Ultra", [{"name": "Hidra Boost Ultra Cream"}])
    assert not ct._named_product_found("Hidra Boost", [{"name": "Crema A"}, {"name": "Ser B"}])
    assert not ct._named_product_found("Hidra Boost", [])  # zero rezultate
    assert ct._named_product_found("ser", [{"name": "Crema A"}])  # niciun token distinctiv


async def test_search_named_product_not_found_discloses(monkeypatch):
    """A1: produs NUMIT inexistent → disclosure «nu există ca atare», dar arată alternative."""

    async def fake_lex(conn, business_id, **k):
        return PRODUCTS  # Crema A / Ser B — niciunul nu e «Hidra Boost Ultra»

    monkeypatch.setattr(ct, "has_embeddings", _has_emb_false)
    monkeypatch.setattr(ct, "search_products_lexical", fake_lex)
    ctx = _ctx()
    res = await run_tool(
        ctx,
        _deps(_LLM()),
        "search_products",
        {"query": "hidra boost", "product_name": "Hidra Boost Ultra"},
    )
    assert res.ok and len(res.products) == 2  # alternativele rămân
    assert "nu există ca atare" in res.llm_view
    assert any(e.type == "named_product_not_found" for e in ctx.events)


async def test_search_named_product_found_no_disclosure(monkeypatch):
    """A1: produsul numit CHIAR e printre rezultate → fără disclosure falsă."""
    prod = [{**PRODUCTS[0], "id": "px", "name": "Hidra Boost Ultra Cream"}]

    async def fake_lex(conn, business_id, **k):
        return prod

    monkeypatch.setattr(ct, "has_embeddings", _has_emb_false)
    monkeypatch.setattr(ct, "search_products_lexical", fake_lex)
    ctx = _ctx()
    res = await run_tool(
        ctx,
        _deps(_LLM()),
        "search_products",
        {"query": "hidra boost", "product_name": "Hidra Boost Ultra"},
    )
    assert res.ok and "nu există ca atare" not in res.llm_view
    assert not any(e.type == "named_product_not_found" for e in ctx.events)


async def test_search_relaxed_discloses(monkeypatch):
    """Relaxare: treapta strictă (cu category) goală → treapta relaxată (fără) cu rezultate →
    llm_view marcat «relaxat» + flag relaxed în event (agentul e sincer că nu e match exact)."""

    async def fake_lex(conn, business_id, **k):
        return [] if k.get("category") else PRODUCTS

    monkeypatch.setattr(ct, "has_embeddings", _has_emb_false)
    monkeypatch.setattr(ct, "search_products_lexical", fake_lex)
    # Exercită explicit calea legacy de category-drop; NX-220 o dezactivează implicit.
    monkeypatch.setattr(get_settings(), "search_category_hard_enabled", False)
    monkeypatch.setattr(get_settings(), "search_offcategory_guard_enabled", False)
    ctx = _ctx()
    res = await run_tool(
        ctx, _deps(_LLM()), "search_products", {"query": "x", "category": "skincare"}
    )
    assert res.ok and res.products  # rezultate de la treapta relaxată
    assert "relaxat" in res.llm_view
    assert _search_event(ctx).properties["relaxed"] is True


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
    # NX-225: furnizorul MORT are semnalul lui — tipul erorii, niciodată mesajul (P12).
    assert _events(ctx, "embed_failed")[0].properties["error_type"] == "RuntimeError"
    assert _events(ctx, "embed_timeout") == []  # mort ≠ lent


# --- NX-225: buget de timp pe embed-ul de query (lent = același drum ca mort) ------


async def test_search_embed_timeout_falls_back_lexical(monkeypatch):
    """Furnizor LENT (nu mort): wait_for taie la buget → lexical-only + `embed_timeout`, iar
    turul NU așteaptă cele 5s ale furnizorului (P4/P6)."""
    monkeypatch.setattr(get_settings(), "embed_timeout_ms", 200)
    monkeypatch.setattr(ct, "has_embeddings", _has_emb_true)
    sem_calls = []

    async def fake_sem(conn, business_id, vec, **k):
        sem_calls.append(vec)
        return PRODUCTS

    async def fake_sql(conn, business_id, **k):
        return PRODUCTS

    monkeypatch.setattr(ct, "search_products_semantic", fake_sem)
    monkeypatch.setattr(ct, "search_products_lexical", fake_sql)
    ctx, llm = _ctx(), _SlowLLM(delay_s=5.0)
    t0 = time.monotonic()
    res = await run_tool(ctx, _deps(llm), "search_products", {"query": "x"})
    elapsed_ms = (time.monotonic() - t0) * 1000

    assert res.ok and len(res.products) == 2  # rezultate LEXICALE, nu tăcere
    assert _search_event(ctx).properties["mode"] == "lexical"
    assert sem_calls == []  # fără query_vec → retrieverul semantic nici nu e chemat
    assert _events(ctx, "embed_timeout")[0].properties["timeout_ms"] == 200
    assert _events(ctx, "embed_failed") == []  # lent ≠ mort
    assert elapsed_ms < 400  # < 2× timeout (nu cele 5000ms ale furnizorului)
    assert llm.cancelled  # corutina a fost anulată, iar CancelledError n-a scăpat în tur


async def test_search_embed_fast_emits_no_degradation_events(monkeypatch):
    """Anti-regresie: embed rapid → semantic activ, ZERO events noi (nu poluăm analytics)."""
    monkeypatch.setattr(get_settings(), "embed_timeout_ms", 200)
    monkeypatch.setattr(ct, "has_embeddings", _has_emb_true)

    async def fake_sem(conn, business_id, vec, **k):
        return PRODUCTS

    async def fake_sql(conn, business_id, **k):
        return []

    monkeypatch.setattr(ct, "search_products_semantic", fake_sem)
    monkeypatch.setattr(ct, "search_products_lexical", fake_sql)
    ctx, llm = _ctx(), _LLM()
    res = await run_tool(ctx, _deps(llm), "search_products", {"query": "x"})
    assert res.ok and len(res.products) == 2 and llm.embed_calls == 1
    assert _search_event(ctx).properties["mode"] == "semantic"
    assert _events(ctx, "embed_timeout") == [] and _events(ctx, "embed_failed") == []


async def test_search_embed_timeout_zero_disables_budget(monkeypatch):
    """`embed_timeout_ms=0` = kill-switch numeric: fără buget de timp, exact ca înainte —
    embed-ul lent apucă să răspundă și calea semantică rămâne activă."""
    monkeypatch.setattr(get_settings(), "embed_timeout_ms", 0)
    monkeypatch.setattr(ct, "has_embeddings", _has_emb_true)

    async def fake_sem(conn, business_id, vec, **k):
        return PRODUCTS

    async def fake_sql(conn, business_id, **k):
        return []

    monkeypatch.setattr(ct, "search_products_semantic", fake_sem)
    monkeypatch.setattr(ct, "search_products_lexical", fake_sql)
    ctx, llm = _ctx(), _SlowLLM(delay_s=0.15)  # ar depăși orice buget rezonabil
    res = await run_tool(ctx, _deps(llm), "search_products", {"query": "x"})
    assert res.ok and len(res.products) == 2
    assert _search_event(ctx).properties["mode"] == "semantic"  # vectorul a ajuns întreg
    assert not llm.cancelled and _events(ctx, "embed_timeout") == []


async def test_search_no_embeddings_never_calls_embed(monkeypatch):
    """`has_embeddings` False → embed-ul nici nu se apelează (bugetul nu atinge drumul ăsta)."""
    monkeypatch.setattr(get_settings(), "embed_timeout_ms", 200)
    monkeypatch.setattr(ct, "has_embeddings", _has_emb_false)

    async def fake_sql(conn, business_id, **k):
        return PRODUCTS

    monkeypatch.setattr(ct, "search_products_lexical", fake_sql)
    ctx, llm = _ctx(), _LLM()
    res = await run_tool(ctx, _deps(llm), "search_products", {"query": "x"})
    assert res.ok and llm.embed_calls == 0
    assert _events(ctx, "embed_timeout") == [] and _events(ctx, "embed_failed") == []


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


async def test_get_product_details_lists_variants(monkeypatch):
    # NX-118: variantele (etichetă + preț real) ajung la model → poate recomanda grounded.
    prod = {
        **PRODUCTS[0],
        "variants": [
            {"id": "v1", "label": "50ml", "price": 82.99, "stock": 4},
            {"id": "v2", "label": "100ml", "price": 149.0, "stock": 0},
        ],
    }

    async def fake_by_ids(conn, business_id, ids, **k):
        return [prod]

    monkeypatch.setattr(ct, "get_products_by_ids", fake_by_ids)
    res = await run_tool(_ctx(), _deps(), "get_product_details", {"product_id": "p1"})
    assert "variante:" in res.llm_view and "50ml" in res.llm_view and "100ml" in res.llm_view
    assert "149.00 lei" in res.llm_view  # prețul per-variantă vizibil modelului
    assert "stoc 0" in res.llm_view  # OOS per-variantă vizibil agentului


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


# --- NX-134: diversify_pool (funcție pură) -----------------------------------


def _c(pid: str, brand: str | None, price: float | None) -> dict:
    return {"id": pid, "name": pid, "brand": brand, "price": price}


def test_diversify_spread_price_and_brands():
    # 9 candidați relevanță-ordonați: primii 3 = același brand + preț mic (clone). diversify trebuie
    # să floteze branduri + terțe de preț diferite în primele `limit`, păstrând top-1.
    cands = [
        _c("a1", "A", 20.0),
        _c("a2", "A", 22.0),
        _c("a3", "A", 24.0),  # 3× brand A, ieftin (clone)
        _c("b1", "B", 60.0),
        _c("c1", "C", 118.0),
        _c("d1", "D", 65.0),
        _c("e1", "E", 119.0),
        _c("b2", "B", 25.0),
        _c("c2", "C", 115.0),
    ]
    out = ct.diversify_pool(cands, 6)
    page = out[:6]
    assert page[0]["id"] == "a1"  # top-1 (relevanță) neschimbat
    # max 2 per brand în pagină
    from collections import Counter

    bc = Counter(p["brand"] for p in page)
    assert all(v <= 2 for v in bc.values())
    # acoperă cele 3 terțe de preț (min 20, max 119 → ieftin/mediu/scump)
    lo, hi = 20.0, 119.0
    terts = {ct._price_tertile(p["price"], lo, hi) for p in page}
    assert terts == {0, 1, 2}
    # pool complet păstrat (doar reordonat), fără pierderi/dubluri
    assert sorted(p["id"] for p in out) == sorted(p["id"] for p in cands)


def test_diversify_noop_when_pool_not_larger_than_limit():
    cands = [_c("a", "A", 10.0), _c("b", "B", 20.0)]
    assert ct.diversify_pool(cands, 6) == cands  # n <= limit → neschimbat


def test_diversify_all_same_brand_fills_to_limit():
    # toate de la un brand → cota imposibilă → fallback pe relevanță (limit rezultate, nu 2)
    cands = [_c(f"a{i}", "A", 10.0 + i) for i in range(9)]
    page = ct.diversify_pool(cands, 6)[:6]
    assert len(page) == 6 and page[0]["id"] == "a0"


def test_diversify_all_same_price_keeps_brand_diversity():
    # preț uniform → terță degenerată; totuși cota de brand se aplică (nu 3 clone de brand)
    cands = [
        _c("a1", "A", 50.0),
        _c("a2", "A", 50.0),
        _c("a3", "A", 50.0),
        _c("b1", "B", 50.0),
        _c("c1", "C", 50.0),
        _c("d1", "D", 50.0),
        _c("e1", "E", 50.0),
    ]
    page = ct.diversify_pool(cands, 6)[:6]
    from collections import Counter

    assert all(v <= 2 for v in Counter(p["brand"] for p in page).values())
    assert page[0]["id"] == "a1"


def test_diversify_no_brand_does_not_consume_quota():
    cands = [_c(f"n{i}", None, 10.0 + i) for i in range(9)]
    page = ct.diversify_pool(cands, 6)[:6]
    assert len(page) == 6  # produse fără brand nu consumă cotă → pot apărea >2


def test_diversify_limit_one_is_top_one_only():
    cands = [_c("a", "A", 10.0), _c("b", "B", 20.0), _c("c", "C", 30.0)]
    out = ct.diversify_pool(cands, 1)
    assert out[0]["id"] == "a" and len(out) == 3  # top-1 primul, pool întreg păstrat


def test_diversify_deterministic():
    cands = [_c(f"p{i}", chr(65 + i % 4), 10.0 + i * 7) for i in range(12)]
    assert ct.diversify_pool(cands, 6) == ct.diversify_pool(cands, 6)


# --- NX-135: fallback gradat pe variantă (variant_label) ---------------------


def test_variant_label_clause_sql():
    """SQL-ul de variantă e parametrizat (safe) + normalizat pe diacritice, corelat pe produs."""
    from src.db.queries import catalog as cq

    params = []

    def ph(v):
        params.append(v)
        return f"${len(params)}"

    sql = cq._variant_label_clause("Warm Beige", ph)
    assert "product_variants v" in sql and "v.product_id = p.id" in sql
    assert "translate(lower(v.label)" in sql  # match normalizat RO (ăâîșț→aaist)
    assert "like" in sql.lower()
    assert params == ["Warm Beige"]  # valoarea trece ca PARAMETRU, nu inline (anti-injection)


async def test_search_passes_variant_label_and_marks_match(monkeypatch):
    """NX-135: `variant_label` ajunge la AMBELE retrievere; rezultatele-s marcate variant_match."""
    captured = {}

    async def fake_sem(conn, business_id, vec, **k):
        captured["sem_vl"] = k.get("variant_label")
        return [dict(PRODUCTS[0])]

    async def fake_lex(conn, business_id, **k):
        captured["lex_vl"] = k.get("variant_label")
        return []

    monkeypatch.setattr(ct, "has_embeddings", _has_emb_true)
    monkeypatch.setattr(ct, "search_products_semantic", fake_sem)
    monkeypatch.setattr(ct, "search_products_lexical", fake_lex)
    ctx = _ctx()
    res = await run_tool(
        ctx,
        _deps(_LLM()),
        "search_products",
        {"query": "fond de ten", "variant_label": "Warm Beige"},
    )
    assert res.ok
    assert captured["sem_vl"] == "Warm Beige" and captured["lex_vl"] == "Warm Beige"
    assert res.products[0].get("variant_match") is True
    assert "are varianta cerută" in res.llm_view  # brief semnalează → fit grounded
    assert _search_event(ctx).properties["had_variant_label"] is True


async def test_search_without_variant_label_is_unchanged(monkeypatch):
    """Fără variant_label → retrievere primesc None, fără marcaj (back-compat, byte-identic)."""
    captured = {}

    async def fake_sem(conn, business_id, vec, **k):
        captured["sem_vl"] = k.get("variant_label")
        return [dict(PRODUCTS[0])]

    async def fake_lex(conn, business_id, **k):
        return []

    monkeypatch.setattr(ct, "has_embeddings", _has_emb_true)
    monkeypatch.setattr(ct, "search_products_semantic", fake_sem)
    monkeypatch.setattr(ct, "search_products_lexical", fake_lex)
    ctx = _ctx()
    res = await run_tool(ctx, _deps(_LLM()), "search_products", {"query": "cremă"})
    assert captured["sem_vl"] is None
    assert res.products[0].get("variant_match") is None  # nemarcat
    assert "are varianta cerută" not in res.llm_view
    assert _search_event(ctx).properties["had_variant_label"] is False


async def test_variant_label_too_long_is_rejected():
    # cap Pydantic (max_length 80) → args invalide → ok=False (nu crash, nu query pe frază)
    long = "x" * 200
    res = await run_tool(_ctx(), _deps(), "search_products", {"query": "y", "variant_label": long})
    assert res.ok is False


# --- NX-223: guard de completitudine al fingerprint-ului de sesiune ---------------------
#
# Regula pe care o păzește: fp-ul sesiunii (`_session_filters`) trebuie să conțină ORICE argument
# din `SearchArgs` care ajunge filtru în retrievere. Un argument nou pasat retrieverelor dar uitat
# în fp NU se vede ca eroare — se vede ca „bot-ul răspunde din pool-ul vechi", luni mai târziu.
# De aceea guard-ul e derivat prin introspecție din semnăturile REALE, nu dintr-o listă scrisă de
# mână care ar rămâne și ea în urmă.

# `SearchArgs.query` ajunge la retriever sub alt nume (`query_text`) — singura redenumire.
_ARG_TO_RETRIEVER_PARAM = {"query": "query_text"}

# Excepții EXPLICITE: argumente pasate retrieverelor care NU definesc sesiunea.
# `limit` = mărimea PAGINII servite din pool, nu un filtru — aceeași sesiune se paginează cu
# limite diferite (vezi test_page_index_monotonic_when_limit_varies).
_FP_EXEMPT = {"limit"}


def _retriever_params() -> set[str]:
    """Parametrii ACCEPTAȚI de cele două retrievere (sursa de adevăr = semnăturile lor)."""
    return {
        name
        for fn in (search_products_lexical, search_products_semantic)
        for name in inspect.signature(fn).parameters
    }


def _fp_gaps(arg_fields: set[str], retriever_params: set[str], fp_keys: set[str]) -> set[str]:
    """Argumentele de `SearchArgs` care ajung la retrievere dar LIPSESC din fp (pur, testabil)."""
    return {
        f
        for f in arg_fields - _FP_EXEMPT
        if _ARG_TO_RETRIEVER_PARAM.get(f, f) in retriever_params and f not in fp_keys
    }


def _fp_keys() -> set[str]:
    """Cheile fp-ului (statice — nu depind de valori); `SearchArgs` minimal valid ca sondă."""
    return set(ct._session_filters(ct.SearchArgs(query="q"), None, None))


def test_session_fp_covers_every_filtering_arg():
    """NX-223: fiecare câmp de `SearchArgs` care ajunge filtru în retrievere e în fp."""
    gaps = _fp_gaps(set(ct.SearchArgs.model_fields), _retriever_params(), _fp_keys())
    assert gaps == set(), (
        f"argumente de filtrare absente din `_session_filters`: {sorted(gaps)} — "
        "rafinarea pe ele ar servi pool-ul VECHI (NX-223). Adaugă-le în fp sau, dacă chiar nu "
        "schimbă setul de rezultate, în `_FP_EXEMPT` cu justificare."
    )


def test_fp_guard_catches_a_new_unfingerprinted_filter():
    """Guard-ul chiar prinde: un filtru NOU pasat retrieverelor și uitat în fp → roșu."""
    gaps = _fp_gaps(
        {"query", "brand", "shade_family"},  # câmp fictiv nou de `SearchArgs`
        _retriever_params() | {"shade_family"},  # …pasat retrieverelor
        _fp_keys(),  # …dar absent din fp
    )
    assert gaps == {"shade_family"}


def test_fp_guard_accepts_exempt_and_known_args():
    """Anti-fals-pozitiv: `limit` (exceptat) și argumentele care NU merg la retrievere nu pică."""
    assert _fp_gaps({"limit"}, _retriever_params(), _fp_keys()) == set()
    assert _fp_gaps({"nu_ajunge_la_retriever"}, _retriever_params(), _fp_keys()) == set()


# --- NX-224: top-up — pagina se umple cu potriviri PARȚIALE, etichetate ------------------
#
# Bucla de relaxare se oprea la PRIMA treaptă cu rezultate: o treaptă care dă 1 produs lăsa
# clientul cu 1 card. Top-up-ul umple RESTUL paginii din treptele rămase, marcând per produs ce
# criteriu nu respectă. Ce se păzește aici: ordinea (strictele primele), corectitudinea marcajului
# (diff REAL de ladder, față de treapta CÂȘTIGĂTOARE), faptul că umplutura NU ocolește safety
# gate-ul și că filtrele DURE (brand/variantă) rămân aplicate pe toate treptele.

_TOPUP_ARGS = {"query": "ser", "concerns": ["ten gras"], "features": ["niacinamida"]}


def _ctx_topup(body: str = "x") -> TurnContext:
    """Ctx cu DomainPack care activează AMBELE constrângeri soft relaxabile (concerns + features)
    → ladder de 3 trepte: [0] strict · [1] fără concerns · [2] fără concerns + fără features."""
    from src.domain.pack import DomainPack
    from src.tools.taxonomy import _BEAUTY

    business = BusinessConfig(id="biz-1", slug="s", name="n", vertical="beauty")
    business.domain_pack = DomainPack(
        vertical="beauty_salon", concern_map=_BEAUTY, searchable_facets=("key_ingredients",)
    )
    return TurnContext(
        turn_id="t",
        business=business,
        contact=Contact(id="c", business_id="biz-1"),
        message=InboundMessage(provider_msg_id="m", body=body),
        conversation_id="conv",
    )


def _tp(i: int, **over) -> dict:
    return {
        "id": f"t{i}",
        "name": f"Produs {i}",
        "brand": f"B{i}",
        "price": 50.0 + i,
        "url": f"https://shop/t{i}",
        "ai_summary": "",
        "availability": "in_stock",
        "rating": 4.0,
        **over,
    }


def _ladder_step(k: dict) -> int:
    """Treapta de ladder din care vine apelul, dedusă din filtrele primite de retriever."""
    if k.get("concerns"):
        return 0
    return 1 if k.get("features") else 2


def _patch_ladder(monkeypatch, by_step: dict[int, list[dict]], *, calls: list | None = None):
    """Retriever lexical care răspunde DIFERIT pe fiecare treaptă (copii — dict-urile de test nu
    trebuie adnotate global). `calls` colectează kwargs-urile fiecărui apel."""

    async def fake_lex(conn, business_id, **k):
        if calls is not None:
            calls.append(k)
        return [dict(p) for p in by_step.get(_ladder_step(k), [])]

    monkeypatch.setattr(ct, "has_embeddings", _has_emb_false)
    monkeypatch.setattr(ct, "search_products_lexical", fake_lex)


def _topup(monkeypatch, value: bool = True) -> None:
    monkeypatch.setattr(get_settings(), "search_topup_enabled", value)


def _partial(products: list[dict]) -> dict[str, list[str]]:
    """id → constrângerile pe care produsul NU le respectă (doar produsele marcate)."""
    return {p["id"]: p["partial_match"] for p in products if p.get("partial_match")}


# --- unit: diff-ul de trepte (sursa marcajului) ------------------------------------------


def test_dropped_constraints_identical_steps_is_empty():
    """Failure case: trepte identice → listă goală → produsul NU primește marcaj fals de parțial."""
    step = {"price_max": 100, "concerns": ["oily"], "category": "seruri", "features": None}
    assert ct._dropped_constraints(step, dict(step)) == []


def test_dropped_constraints_reports_only_real_drops():
    strict = {"price_max": 100, "concerns": ["oily"], "category": "seruri", "features": ["niac"]}
    assert ct._dropped_constraints(strict, {**strict, "concerns": None}) == ["concerns"]
    # ordine stabilă (`_LADDER_KEYS`), independentă de ordinea de relaxare
    assert ct._dropped_constraints(strict, {**strict, "category": None, "concerns": None}) == [
        "concerns",
        "category",
    ]
    # constrângere deja absentă în treapta strictă → nu poate fi „renunțată"
    lax = {**strict, "concerns": None}
    assert ct._dropped_constraints(lax, lax) == []


def test_dropped_constraints_handles_boolean_relaxation():
    """`in_stock_only` e bool, nu None: True→False = renunțare reală (marcaj corect, nu tăcere)."""
    assert ct._dropped_constraints({"in_stock_only": True}, {"in_stock_only": False}) == [
        "in_stock_only"
    ]
    assert ct._dropped_constraints({"in_stock_only": False}, {"in_stock_only": False}) == []


def test_hard_filters_are_not_ladder_keys():
    """Structural: brand / variant_label (și orice filtru DUR viitor, ex. excluderile NX-222) NU
    sunt în ladder → prin construcție top-up-ul nu poate umple peste ele."""
    assert "brand" not in ct._LADDER_KEYS and "variant_label" not in ct._LADDER_KEYS
    step0 = ct._relax_ladder(
        price_max=1.0, concerns=["oily"], category="c", in_stock_only=True, features=["f"]
    )[0]
    assert set(ct._LADDER_KEYS) == set(step0)  # marcajul acoperă TOT ce poate relaxa ladder-ul


def test_partial_marker_in_brief_only_on_partial_products():
    """`_brief`: marcajul apare DOAR pe produsele de umplere și spune CE nu respectă."""
    view = ct._brief([_tp(0), _tp(1, partial_match=["price_max", "concerns"])])
    strict_line, filler_line = view.split("\n")
    assert "aproape de criterii" not in strict_line
    assert "aproape de criterii: iese din buget, fără nevoia cerută" in filler_line


# --- happy path -------------------------------------------------------------------------


async def test_topup_fills_page_strict_first_with_labels(monkeypatch):
    """DoD: strict = 2, relaxat = alte 7 → pagina are 6: cele 2 stricte PRIMELE, 4 marcate cu
    diff-ul REAL al treptelor."""
    _patch_ladder(monkeypatch, {0: [_tp(0), _tp(1)], 1: [_tp(i) for i in range(2, 9)]})
    _topup(monkeypatch)
    ctx = _ctx_topup()
    res = await run_tool(ctx, _deps_no_llm(), "search_products", dict(_TOPUP_ARGS))

    ids = [p["id"] for p in res.products]
    assert len(ids) == 6 and ids[:2] == ["t0", "t1"]  # strictele primele, în ordinea lor
    marked = _partial(res.products)
    assert set(marked) == set(ids[2:])  # exact umplutura e marcată
    assert all(v == ["concerns"] for v in marked.values())
    ev = _search_event(ctx)
    assert ev.properties["topup_added"] == 4 and ev.properties["topup_depth"] == 1
    assert ev.properties["relax_depth"] == 0  # treapta câștigătoare rămâne cea strictă


async def test_topup_view_discloses_partial_matches(monkeypatch):
    """Modelul primește marcaj PER produs + nota agregată cu câte sunt potriviri complete."""
    _patch_ladder(monkeypatch, {0: [_tp(0)], 1: [_tp(i) for i in range(1, 9)]})
    _topup(monkeypatch)
    res = await run_tool(_ctx_topup(), _deps_no_llm(), "search_products", dict(_TOPUP_ARGS))
    lines = res.llm_view.split("\n")
    assert "primul produs de mai jos respectă toate criteriile" in lines[0]
    assert res.llm_view.count("aproape de criterii: fără nevoia cerută") == 5
    assert lines[1].startswith("[t0]") and "aproape de criterii" not in lines[1]


async def test_full_strict_page_skips_topup(monkeypatch):
    """Strict ≥ limit → zero top-up: nici query în plus, nici marcaje, `topup_added=0`."""
    calls: list = []
    _patch_ladder(monkeypatch, {0: [_tp(i) for i in range(7)]}, calls=calls)
    _topup(monkeypatch)
    ctx = _ctx_topup()
    res = await run_tool(ctx, _deps_no_llm(), "search_products", dict(_TOPUP_ARGS))
    assert len(calls) == 1  # o singură treaptă interogată (fără latență cheltuită degeaba)
    assert _partial(res.products) == {} and "aproape de criterii" not in res.llm_view
    ev = _search_event(ctx)
    assert ev.properties["topup_added"] == 0 and ev.properties["topup_depth"] == 0


async def test_topup_flag_off_leaves_page_hungry(monkeypatch):
    """Kill-switch OFF (default) → pagina rămâne înfometată, exact ca înainte: 2 produse, zero
    query-uri în plus, zero marcaje, `topup_added=0`."""
    calls: list = []
    _patch_ladder(monkeypatch, {0: [_tp(0), _tp(1)], 1: [_tp(i) for i in range(2, 9)]}, calls=calls)
    _topup(monkeypatch, False)
    ctx = _ctx_topup()
    res = await run_tool(ctx, _deps_no_llm(), "search_products", dict(_TOPUP_ARGS))
    assert [p["id"] for p in res.products] == ["t0", "t1"] and len(calls) == 1
    assert _partial(res.products) == {} and "aproape de criterii" not in res.llm_view
    ev = _search_event(ctx)
    assert ev.properties["topup_added"] == 0 and ev.properties["topup_depth"] == 0


async def test_topup_labels_are_relative_to_winning_step(monkeypatch):
    """Strict = 0 → prima treaptă NEVIDĂ câștigă (comportament existent, NEschimbat) și devine
    referința marcajului: umplutura de pe treapta 2 e marcată DOAR cu ce a pierdut față de EA."""
    _patch_ladder(monkeypatch, {0: [], 1: [_tp(1), _tp(2)], 2: [_tp(3)]})
    _topup(monkeypatch)
    ctx = _ctx_topup()
    res = await run_tool(ctx, _deps_no_llm(), "search_products", dict(_TOPUP_ARGS))
    assert [p["id"] for p in res.products] == ["t1", "t2", "t3"]
    assert _partial(res.products) == {"t3": ["features"]}  # NU ["concerns", "features"]
    assert "fără ingredientul cerut" in res.llm_view and "fără nevoia cerută" not in res.llm_view
    ev = _search_event(ctx)
    assert ev.properties["relax_depth"] == 1 and ev.properties["topup_depth"] == 2


# --- safety + filtre dure ---------------------------------------------------------------


async def test_topup_products_go_through_safety_gate(monkeypatch):
    """DoD (P0): produsul contraindicat adus de treapta relaxată NU ajunge la client — top-up-ul
    rulează ÎNAINTE de gate, deci umplutura n-are ușă din dos (nici spre pool)."""
    unsafe = _tp(9, name="Ser Retinol de noapte", attributes={"key_ingredients": ["retinal"]})
    _patch_ladder(monkeypatch, {0: [_tp(0)], 1: [unsafe, _tp(1), _tp(2)]})
    _topup(monkeypatch)
    ctx = _ctx_topup("sunt însărcinată, ce ser antirid pot folosi?")
    res = await run_tool(ctx, _deps_no_llm(), "search_products", dict(_TOPUP_ARGS))
    ids = [p["id"] for p in res.products]
    assert "t9" not in ids and ids[0] == "t0" and {"t1", "t2"} <= set(ids)
    assert "Retinol" not in res.llm_view
    assert "t9" not in ((ctx.state_patch.get("active_search") or {}).get("pool") or [])


async def test_topup_all_filler_blocked_keeps_survivors(monkeypatch):
    """Edge: safety taie TOATĂ umplutura → pagina rămâne cu ce a supraviețuit (fără re-fetch în
    buclă). `topup_added=0` cu `topup_depth>0` = «s-a umplut, dar gate-ul a tăiat»."""
    unsafe = [
        _tp(8, name="Ser Retinol A", attributes={"key_ingredients": ["retinal"]}),
        _tp(9, name="Ser Retinol B", attributes={"key_ingredients": ["retinol"]}),
    ]
    _patch_ladder(monkeypatch, {0: [_tp(0)], 1: unsafe})
    _topup(monkeypatch)
    ctx = _ctx_topup("sunt însărcinată, ce ser antirid pot folosi?")
    res = await run_tool(ctx, _deps_no_llm(), "search_products", dict(_TOPUP_ARGS))
    assert [p["id"] for p in res.products] == ["t0"]
    ev = _search_event(ctx)
    assert ev.properties["topup_added"] == 0 and ev.properties["topup_depth"] == 1


async def test_topup_keeps_hard_filters_on_every_step(monkeypatch):
    """DoD: brand + variant_label cerute → TOATE interogările (strictă și de umplere) le poartă;
    umplutura nu poate ieși din ele."""
    calls: list = []
    _patch_ladder(monkeypatch, {0: [_tp(0)], 1: [_tp(1), _tp(2)]}, calls=calls)
    _topup(monkeypatch)
    args = {**_TOPUP_ARGS, "brand": "B0", "variant_label": "Warm Beige"}
    res = await run_tool(_ctx_topup(), _deps_no_llm(), "search_products", args)
    assert len(calls) == 3 and len(res.products) == 3  # treapta strictă + 2 trepte de umplere
    assert all(c["brand"] == "B0" and c["variant_label"] == "Warm Beige" for c in calls)


# --- edge / failure ---------------------------------------------------------------------


async def test_topup_dedups_products_seen_on_earlier_step(monkeypatch):
    """Edge: același produs apare și pe treapta relaxată → o SINGURĂ dată, ca STRICT (nemarcat)."""
    _patch_ladder(monkeypatch, {0: [_tp(0)], 1: [_tp(0), _tp(1)]})
    _topup(monkeypatch)
    res = await run_tool(_ctx_topup(), _deps_no_llm(), "search_products", dict(_TOPUP_ARGS))
    assert [p["id"] for p in res.products] == ["t0", "t1"]
    assert _partial(res.products) == {"t1": ["concerns"]}


async def test_topup_short_page_pools_in_served_order_then_exhausts(monkeypatch):
    """Edge: toate treptele sub limit cumulat → pagină de 3, pool = stricte + umplutură ÎN ORDINEA
    servită (paginarea continuă natural în zona de umplutură), fără epuizare falsă; „mai arată-mi"
    (fp identic) → `_NO_MORE_VIEW`, nu repetarea produselor deja arătate."""
    from src.models import ProductRef

    async def no_ids(conn, business_id, ids, **k):
        return []

    _patch_ladder(monkeypatch, {0: [_tp(0)], 1: [_tp(1), _tp(2)]})
    monkeypatch.setattr(ct, "get_products_by_ids", no_ids)
    _topup(monkeypatch)
    ctx1 = _ctx_topup()
    res1 = await run_tool(ctx1, _deps_no_llm(), "search_products", dict(_TOPUP_ARGS))
    sess = ctx1.state_patch["active_search"]
    assert [p["id"] for p in res1.products] == ["t0", "t1", "t2"]
    assert sess["pool"] == ["t0", "t1", "t2"] and sess["cursor"] == 3

    ctx2 = _ctx_topup()
    ctx2.state.active_search = sess
    ctx2.state.displayed_products = [
        ProductRef(p["id"], p["name"], p["price"]) for p in res1.products
    ]
    res2 = await run_tool(ctx2, _deps_no_llm(), "search_products", dict(_TOPUP_ARGS))
    assert res2.products == [] and res2.llm_view == ct._NO_MORE_VIEW
    assert _events(ctx2, "search_session")[-1].properties["action"] == "exhausted"


async def test_topup_survives_semantic_failure(monkeypatch):
    """Failure: semanticul aruncă pe treapta de umplere → umplutura continuă lexical-only (aceeași
    degradare ca bucla principală, P6)."""

    async def boom_sem(conn, business_id, vec, **k):
        raise RuntimeError("pgvector down")

    _patch_ladder(monkeypatch, {0: [_tp(0)], 1: [_tp(1), _tp(2)]})
    monkeypatch.setattr(ct, "has_embeddings", _has_emb_true)
    monkeypatch.setattr(ct, "search_products_semantic", boom_sem)
    _topup(monkeypatch)
    res = await run_tool(_ctx_topup(), _deps(_LLM()), "search_products", dict(_TOPUP_ARGS))
    assert [p["id"] for p in res.products] == ["t0", "t1", "t2"]
    assert _partial(res.products) == {"t1": ["concerns"], "t2": ["concerns"]}
