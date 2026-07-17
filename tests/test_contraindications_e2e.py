"""NX-173 (P0) — MATRICEA de căi: fiecare cale prin care un produs poate ajunge la client, cu
produs permis + produs blocat, assert pe **ID-uri** (nu pe cuvinte în reply).

Căile vin din review-ul Codex pe #229 — cele care ocoleau gate-ul inițial sunt marcate `[leak]`:
search · page · details · compare · link intent `[leak]` · compare intent `[leak]` ·
attr query `[leak]` · cheaper `[leak]` · cross-sell `[leak]` · rehidratare `[leak]` ·
cart_add `[mutație]` · checkout_link `[mutație]` · back_in_stock `[mutație]` · state vechi.

Pentru mutații se verifică **lipsa side-effect-ului** (rând scris / state_patch / link întors), nu
doar `products` — o filtrare de rezultat nu poate anula o scriere.
"""

import pytest

from src.models import (
    BusinessConfig,
    Contact,
    ConversationState,
    InboundMessage,
    Message,
    ProductRef,
    TurnContext,
)
from src.tools import catalog_tools as ct
from src.tools import commerce_tools as cm
from src.tools.base import run_tool
from src.worker.runner import PipelineDeps

SAFE_IDS = {"safe-bakuchiol", "safe-vitc"}
UNSAFE_IDS = {"unsafe-retinal", "unsafe-retinol-name"}
PREG = "sunt însărcinată"

CATALOG = [
    {
        "id": "safe-bakuchiol",
        "name": "Ser Bakuchiol Gentle",
        "brand": "Auralis",
        "price": 84.0,
        "url": "https://shop.x/p/1",
        "ai_summary": "alternativă blândă pentru primele riduri",
        "availability": "in_stock",
        "rating": 4.6,
        "attributes": {"key_ingredients": ["bakuchiol", "squalan"], "best_for": "primele riduri"},
    },
    {
        "id": "unsafe-retinal",  # retinoid DOAR în key_ingredients
        "name": "LumaDerm Renew Ser",
        "brand": "LumaDerm",
        "price": 149.0,
        "url": "https://shop.x/p/2",
        "ai_summary": "ser concentrat pentru riduri",
        "availability": "out_of_stock",
        "rating": 4.9,
        "attributes": {"key_ingredients": ["retinal", "squalan"], "best_for": "riduri"},
    },
    {
        "id": "safe-vitc",
        "name": "Nova Vitamina C Ser",
        "brand": "Nova",
        "price": 99.0,
        "url": "https://shop.x/p/3",
        "ai_summary": "ser cu vitamina C",
        "availability": "in_stock",
        "rating": 4.5,
        "attributes": {"key_ingredients": ["vitamina C"], "best_for": "luminozitate"},
    },
    {
        "id": "unsafe-retinol-name",  # retinoid DOAR în nume
        "name": "Auralis Retinol Ser de noapte",
        "brand": "Auralis",
        "price": 119.0,
        "url": "https://shop.x/p/4",
        "ai_summary": "ser de noapte pentru riduri",
        "availability": "out_of_stock",
        "rating": 4.8,
        "attributes": {"best_for": "riduri"},
    },
]
BY_ID = {p["id"]: p for p in CATALOG}


def _ctx(body: str, *, displayed=(), history=(), safety=None) -> TurnContext:
    state = ConversationState(
        displayed_products=[
            ProductRef(product_id=i, name=BY_ID[i]["name"], price=BY_ID[i]["price"])
            for i in displayed
        ],
        safety=safety or {},
    )
    return TurnContext(
        turn_id="t",
        # `checkout_url` setat → testele de checkout ajung LA gate-ul de siguranță; fără el,
        # tool-ul iese devreme cu `no_checkout_url` și n-am testa nimic.
        business=BusinessConfig(
            id="biz-1", slug="s", name="n", settings={"checkout_url": "https://shop.x/cart"}
        ),
        contact=Contact(id="c", business_id="biz-1"),
        message=InboundMessage(provider_msg_id="m", body=body),
        conversation_id="conv",
        history=[Message(direction="inbound", author="contact", body=h) for h in history],
        state=state,
    )


def _deps() -> PipelineDeps:
    return PipelineDeps(conn=object(), redis=None, llm=None)


@pytest.fixture(autouse=True)
def _patch(monkeypatch):
    async def fake_lex(conn, business_id, **k):
        return [dict(p) for p in CATALOG]

    async def fake_by_ids(conn, business_id, ids, **k):
        return [dict(BY_ID[i]) for i in ids if i in BY_ID]

    for mod in (ct, cm):
        monkeypatch.setattr(mod, "get_products_by_ids", fake_by_ids, raising=False)
    monkeypatch.setattr(ct, "search_products_lexical", fake_lex)


def _ids(products) -> set[str]:
    return {str(p.get("id") or p.get("product_id")) for p in products}


# --- catalog: search / page / details / compare -------------------------------------------------


async def test_search_blocks_contraindicated_keeps_safe():
    ctx = _ctx(f"{PREG}, ce ser antirid pot folosi?")
    res = await run_tool(ctx, _deps(), "search_products", {"query": "ser antirid"})
    assert _ids(res.products) & UNSAFE_IDS == set()
    assert _ids(res.products) & SAFE_IDS  # P6: alternativele sigure ies


async def test_search_pool_excludes_contraindicated():
    ctx = _ctx(f"{PREG}, ce ser antirid?")
    await run_tool(ctx, _deps(), "search_products", {"query": "ser"})
    pool = set((ctx.state_patch.get("active_search") or {}).get("pool") or [])
    assert pool & UNSAFE_IDS == set()


async def test_search_llm_view_never_names_blocked_product():
    ctx = _ctx(f"{PREG}, ce ser antirid?")
    res = await run_tool(ctx, _deps(), "search_products", {"query": "ser"})
    assert "LumaDerm" not in res.llm_view and "Retinol Ser de noapte" not in res.llm_view


async def test_search_view_has_no_internal_jargon():
    """Modelul nu mai primește pseudo-copy intern („EXCLUS determinist / REGULI DURE")."""
    ctx = _ctx(f"{PREG}, ce ser antirid?")
    res = await run_tool(ctx, _deps(), "search_products", {"query": "ser"})
    for bad in ("EXCLUS", "REGULI DURE", "CONTEXT DE SIGURANȚĂ"):
        assert bad not in res.llm_view


async def test_page_refilters_stale_pool():
    """[leak] pool semănat înainte de declarație → paginarea nu-l scapă."""
    ctx = _ctx("arată-mi seruri")
    await run_tool(ctx, _deps(), "search_products", {"query": "ser"})
    stale = ctx.state_patch["active_search"]
    assert set(stale["pool"]) & UNSAFE_IDS, "precondiție: pool-ul vechi conține contraindicate"
    ctx2 = _ctx("mai arată-mi", history=[PREG])
    ctx2.state = ConversationState(active_search=stale)
    res = await ct.continue_search_session(ctx2, _deps(), stale, 6)
    assert _ids(res.products) & UNSAFE_IDS == set()


async def test_details_refuses_blocked_and_allows_safe():
    ctx = _ctx(f"{PREG}, spune-mi despre LumaDerm")
    res = await run_tool(ctx, _deps(), "get_product_details", {"product_id": "unsafe-retinal"})
    assert res.ok is False and res.error == "safety_excluded" and res.products == []
    assert "149" not in res.llm_view  # nici prețul (validatorul l-ar accepta ca grounded)

    ok = await run_tool(ctx, _deps(), "get_product_details", {"product_id": "safe-bakuchiol"})
    assert ok.ok is True and _ids(ok.products) == {"safe-bakuchiol"}


async def test_compare_refuses_when_one_is_blocked():
    ctx = _ctx(f"{PREG}, compară-le")
    res = await run_tool(
        ctx, _deps(), "compare_products", {"product_ids": ["safe-bakuchiol", "unsafe-retinal"]}
    )
    assert res.ok is False and res.error == "safety_excluded" and res.products == []
    assert "LumaDerm" not in res.llm_view


async def test_compare_two_safe_still_works():
    ctx = _ctx(f"{PREG}, compară-le")
    res = await run_tool(
        ctx, _deps(), "compare_products", {"product_ids": ["safe-bakuchiol", "safe-vitc"]}
    )
    assert res.ok is True and len(res.products) == 2


# --- MUTAȚII: efectul NU are voie să se producă -------------------------------------------------


async def test_cart_add_blocked_writes_no_cart():
    """[mutație] backstop-ul filtra `products`, dar `state_patch['cart']` plecase deja."""
    ctx = _ctx(f"{PREG}, adaugă-l în coș")
    res = await run_tool(ctx, _deps(), "cart_add", {"product_id": "unsafe-retinal"})
    assert res.ok is False and res.error == "safety_excluded"
    assert res.state_patch == {}, "coșul NU are voie să fie mutat"
    assert "cart" not in ctx.state_patch
    assert res.products == [] and res.prices == []


async def test_cart_add_safe_still_works():
    ctx = _ctx(f"{PREG}, adaugă-l în coș")
    res = await run_tool(ctx, _deps(), "cart_add", {"product_id": "safe-bakuchiol"})
    assert res.ok is True and res.state_patch["cart"][0]["product_id"] == "safe-bakuchiol"


async def test_checkout_link_blocked_writes_nothing_and_returns_no_link(monkeypatch):
    """[mutație] `create_checkout_link` scria rândul, iar linkul se întorcea oricum."""
    called = []

    async def spy(*a, **k):
        called.append(a)

    monkeypatch.setattr(cm, "create_checkout_link", spy)
    ctx = _ctx(f"{PREG}, vreau să-l cumpăr")
    res = await run_tool(
        ctx, _deps(), "checkout_link", {"cart_items": [{"product_id": "unsafe-retinal"}]}
    )
    assert res.ok is False and res.error == "safety_excluded"
    assert called == [], "NU are voie să scrie checkout_links"
    assert res.links == [] and res.prices == []
    assert "http" not in res.llm_view


async def test_checkout_link_refuses_whole_cart_if_any_line_blocked(monkeypatch):
    """Checkout „parțial", tăcut, ar schimba comanda clientului fără să-i spună → refuz total."""
    called = []
    monkeypatch.setattr(cm, "create_checkout_link", lambda *a, **k: called.append(a))
    ctx = _ctx(f"{PREG}, cumpăr")
    res = await run_tool(
        ctx,
        _deps(),
        "checkout_link",
        {"cart_items": [{"product_id": "safe-bakuchiol"}, {"product_id": "unsafe-retinal"}]},
    )
    assert res.ok is False and res.error == "safety_excluded" and called == []


async def test_back_in_stock_blocked_writes_no_subscription(monkeypatch):
    """[mutație] cea mai gravă formă: abonarea declanșează un mesaj PROACTIV peste zile."""
    called = []

    async def spy(*a, **k):
        called.append(a)
        return {"created": True}

    monkeypatch.setattr(cm, "subscribe_back_in_stock", spy)
    monkeypatch.setattr(cm, "has_back_in_stock_sub", lambda *a, **k: False)
    ctx = _ctx(f"{PREG}, anunță-mă când revine")
    res = await run_tool(ctx, _deps(), "subscribe_back_in_stock", {"product_id": "unsafe-retinal"})
    assert res.ok is False and res.error == "safety_excluded"
    assert called == [], "NU are voie să scrie back_in_stock_subscriptions"


async def test_back_in_stock_safe_still_subscribes(monkeypatch):
    called = []

    async def spy(*a, **k):
        called.append(a)
        return {"created": True}

    monkeypatch.setattr(cm, "subscribe_back_in_stock", spy)

    async def no_sub(*a, **k):
        return False

    monkeypatch.setattr(cm, "has_back_in_stock_sub", no_sub)
    safe_oos = dict(BY_ID["safe-vitc"], availability="out_of_stock")
    monkeypatch.setattr(cm, "get_products_by_ids", lambda *a, **k: _async([safe_oos]))
    ctx = _ctx(f"{PREG}, anunță-mă")
    res = await run_tool(ctx, _deps(), "subscribe_back_in_stock", {"product_id": "safe-vitc"})
    assert res.ok is True and len(called) == 1


def _async(v):
    async def _c():
        return v

    return _c()


# --- fără context: zero schimbare de comportament -----------------------------------------------


async def test_no_context_surfaces_everything():
    ctx = _ctx("ce ser antirid recomanzi?")
    res = await run_tool(ctx, _deps(), "search_products", {"query": "ser antirid"})
    assert _ids(res.products) & UNSAFE_IDS, "gate-ul NU are voie să excludă preventiv"
    assert not [e for e in ctx.events if e.type == "safety_contraindication_block"]


async def test_no_context_cart_add_works():
    ctx = _ctx("adaugă-l în coș")
    res = await run_tool(ctx, _deps(), "cart_add", {"product_id": "unsafe-retinal"})
    assert res.ok is True  # retinoidul e un produs legitim fără context declarat


# --- context persistat (nu depinde de fereastra de 8 mesaje) ------------------------------------


async def test_context_from_persisted_state_beyond_history_window():
    """Declarația e mai veche decât istoricul (8 mesaje) → `state.safety` o ține (review Codex)."""
    ctx = _ctx("arată-mi seruri antirid", safety={"contexts": ["pregnancy"], "source": "declared"})
    assert not ctx.history  # NIMIC în istoric
    res = await run_tool(ctx, _deps(), "search_products", {"query": "ser"})
    assert _ids(res.products) & UNSAFE_IDS == set()


async def test_context_from_history_still_works():
    ctx = _ctx("arată-mi seruri antirid", history=[PREG])
    res = await run_tool(ctx, _deps(), "search_products", {"query": "ser"})
    assert _ids(res.products) & UNSAFE_IDS == set()


# --- kill-switch --------------------------------------------------------------------------------


async def test_kill_switch_off_restores_old_behaviour(monkeypatch):
    from src.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("SAFETY_CONTRAINDICATIONS_ENABLED", "false")
    try:
        ctx = _ctx(f"{PREG}, ce ser antirid?")
        res = await run_tool(ctx, _deps(), "search_products", {"query": "ser"})
        assert _ids(res.products) & UNSAFE_IDS  # comportamentul (nesigur) de dinainte
    finally:
        get_settings.cache_clear()


# --- FAIL-CLOSED: registru stricat --------------------------------------------------------------


async def test_broken_registry_blocks_everything_when_context_persisted(monkeypatch, tmp_path):
    """[review Codex] Registrul lipsă/corupt DEZACTIVA complet protecția (întorcea registru gol
    → „gate INERT" → catalog nefiltrat). Acum: context activ + registru stricat → nu expunem
    nimic și spunem onest că nu putem verifica (P6: nu tăcere)."""
    from src.safety.contraindications import load_registry
    from src.safety.policy import SafetyPolicy

    monkeypatch.setattr("src.safety.contraindications._RULES_PATH", tmp_path / "gone.json")
    load_registry.cache_clear()
    try:
        # contextul e PERSISTAT în state → nu depinde de pattern-urile din registrul stricat
        ctx = _ctx("arată-mi seruri", safety={"contexts": ["pregnancy"]})
        policy = SafetyPolicy.for_turn(ctx)
        assert policy.registry_ok is False
        d = policy.evaluate([dict(p) for p in CATALOG], purpose="search")
        assert d.unavailable is True
        assert d.kept == [], "fail-closed: nimic expus pe registru stricat"
        assert d.must_refer is True
        res = await run_tool(ctx, _deps(), "search_products", {"query": "ser"})
        assert res.products == []
    finally:
        load_registry.cache_clear()


async def test_broken_registry_does_not_break_normal_turns(monkeypatch, tmp_path):
    """Fail-closed lovește DOAR tururile cu context de siguranță — restul magazinului merge."""
    from src.safety.contraindications import load_registry
    from src.safety.policy import SafetyPolicy

    monkeypatch.setattr("src.safety.contraindications._RULES_PATH", tmp_path / "gone.json")
    load_registry.cache_clear()
    try:
        ctx = _ctx("ce ser antirid recomanzi?")  # fără context declarat
        assert SafetyPolicy.for_turn(ctx).contexts == frozenset()
        res = await run_tool(ctx, _deps(), "search_products", {"query": "ser"})
        assert res.products, "P6: un tur normal nu are voie să fie blocat de registru"
    finally:
        load_registry.cache_clear()


async def test_broken_registry_blocks_mutations(monkeypatch, tmp_path):
    from src.safety.contraindications import load_registry
    from src.safety.policy import SafetyPolicy

    monkeypatch.setattr("src.safety.contraindications._RULES_PATH", tmp_path / "gone.json")
    load_registry.cache_clear()
    try:
        ctx = _ctx("adaugă-l în coș", safety={"contexts": ["pregnancy"]})
        assert SafetyPolicy.for_turn(ctx).allows(dict(BY_ID["safe-bakuchiol"])) is False
    finally:
        load_registry.cache_clear()
