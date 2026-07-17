"""NX-173 (P0) — test ADVERSARIAL prin `search_products_tool` REAL (retrieverul +
`get_products_by_ids` monkeypatch-uite, fără DB/LLM).

Catalogul de test e adversarial prin construcție: **produse sigure + produse contraindicate**, iar
assert-urile sunt pe **ID-urile SURFACED** (`ToolResult.products`, pool-ul sesiunii, vederea spre
model) — NU pe cuvinte interzise în reply. Exact gaura din golden-ul `nx172-contraindicatie`: acolo
fixture-ul avea doar un produs sigur fictiv, deci scenariul nu putea pica orice ar fi făcut botul.
"""

import pytest

from src.models import (
    BusinessConfig,
    Contact,
    ConversationState,
    InboundMessage,
    Message,
    TurnContext,
)
from src.tools import catalog_tools as ct
from src.tools.base import run_tool
from src.worker.runner import PipelineDeps

SAFE_IDS = {"safe-bakuchiol", "safe-vitc"}
UNSAFE_IDS = {"unsafe-retinal", "unsafe-retinol-name"}

CATALOG = [
    {
        "id": "safe-bakuchiol",
        "name": "Ser Bakuchiol Gentle",
        "brand": "Auralis",
        "price": 84.0,
        "url": "u1",
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
        "url": "u2",
        "ai_summary": "ser concentrat pentru riduri",
        "availability": "in_stock",
        "rating": 4.9,
        "attributes": {"key_ingredients": ["retinal", "squalan"], "best_for": "riduri"},
    },
    {
        "id": "safe-vitc",
        "name": "Nova Botanics Vitamina C Ser",
        "brand": "Nova",
        "price": 99.0,
        "url": "u3",
        "ai_summary": "ser cu vitamina C pentru luminozitate",
        "availability": "in_stock",
        "rating": 4.5,
        "attributes": {"key_ingredients": ["vitamina C"], "best_for": "luminozitate"},
    },
    {
        "id": "unsafe-retinol-name",  # retinoid DOAR în nume (fără key_ingredients)
        "name": "Auralis Retinol Ser de noapte",
        "brand": "Auralis",
        "price": 119.0,
        "url": "u4",
        "ai_summary": "ser de noapte pentru riduri",
        "availability": "in_stock",
        "rating": 4.8,
        "attributes": {"best_for": "riduri"},
    },
]


def _ctx(body: str, history: list[Message] | None = None) -> TurnContext:
    return TurnContext(
        turn_id="t",
        business=BusinessConfig(id="biz-1", slug="s", name="n"),
        contact=Contact(id="c", business_id="biz-1"),
        message=InboundMessage(provider_msg_id="m", body=body),
        conversation_id="conv",
        history=history or [],
    )


def _deps() -> PipelineDeps:
    return PipelineDeps(conn=object(), redis=None, llm=None)  # lexical-only


@pytest.fixture(autouse=True)
def _patch(monkeypatch):
    by_id = {p["id"]: p for p in CATALOG}

    async def fake_lex(conn, business_id, **k):
        return [dict(p) for p in CATALOG]

    async def fake_by_ids(conn, business_id, ids, **k):
        return [dict(by_id[i]) for i in ids if i in by_id]

    monkeypatch.setattr(ct, "search_products_lexical", fake_lex)
    monkeypatch.setattr(ct, "get_products_by_ids", fake_by_ids)


async def _search(ctx, **args):
    return await run_tool(ctx, _deps(), "search_products", {"query": "ser antirid", **args})


# --- P0: scenariul de sarcină ------------------------------------------------------------------


async def test_pregnancy_surfaces_no_contraindicated_product():
    """DoD: niciun produs contraindicat în retrieval final — assert pe ID-uri, nu pe text."""
    ctx = _ctx("sunt însărcinată, ce cremă antirid pot folosi?")
    res = await _search(ctx)
    surfaced = {p["id"] for p in res.products}
    assert surfaced & UNSAFE_IDS == set(), f"produs contraindicat surfacat: {surfaced & UNSAFE_IDS}"
    assert surfaced, "P6: gate-ul nu are voie să golească tot când există alternative sigure"
    assert surfaced <= SAFE_IDS


async def test_pregnancy_pool_has_no_contraindicated_product():
    """DoD: nici în sesiunea de căutare — altfel «arată-mi altele» le readuce."""
    ctx = _ctx("sunt însărcinată, ce ser antirid pot folosi?")
    await _search(ctx)
    pool = set((ctx.state_patch.get("active_search") or {}).get("pool") or [])
    assert pool & UNSAFE_IDS == set(), f"produs contraindicat în pool-ul sesiunii: {pool}"


async def test_pregnancy_llm_view_never_names_contraindicated_product():
    """Modelul nu primește NICIODATĂ produsul exclus — nu poate să-l scrie chiar dacă vrea."""
    ctx = _ctx("sunt însărcinată, ce ser antirid pot folosi?")
    res = await _search(ctx)
    assert "LumaDerm" not in res.llm_view
    assert "Retinol Ser de noapte" not in res.llm_view


async def test_pregnancy_view_forbids_medical_advice():
    ctx = _ctx("sunt însărcinată, ce ser antirid pot folosi?")
    res = await _search(ctx)
    assert "nu da sfat medical" in res.llm_view.lower()
    assert "medicul" in res.llm_view


async def test_safety_block_event_emitted():
    ctx = _ctx("sunt însărcinată, ce ser antirid pot folosi?")
    await _search(ctx)
    ev = next((e for e in ctx.events if e.type == "safety_contraindication_block"), None)
    assert ev is not None
    assert ev.properties["blocked"] == 2
    assert ev.properties["contexts"] == ["pregnancy"]
    assert set(ev.properties["product_ids"]) == UNSAFE_IDS


# --- fără context: nicio schimbare de comportament ---------------------------------------------


async def test_no_context_surfaces_everything():
    """Fără declarație → catalogul curge normal (retinoidul e un produs legitim)."""
    ctx = _ctx("ce ser antirid recomanzi?")
    res = await _search(ctx)
    assert {p["id"] for p in res.products} & UNSAFE_IDS, "gate-ul excludeuri preventiv"
    assert not [e for e in ctx.events if e.type == "safety_contraindication_block"]


# --- multi-tur ---------------------------------------------------------------------------------


async def test_context_from_previous_turn_still_filters():
    """«sunt însărcinată» (t1) → «arată-mi seruri antirid» (t2): gate-ul ține pe turul 2."""
    ctx = _ctx(
        "arată-mi seruri antirid",
        history=[Message(direction="inbound", author="contact", body="sunt însărcinată")],
    )
    res = await _search(ctx)
    assert {p["id"] for p in res.products} & UNSAFE_IDS == set()


async def test_stale_session_pool_is_refiltered_on_paging():
    """Pool semănat ÎNAINTE de declarație → «mai arată-mi» după declarație nu-l scapă."""
    ctx = _ctx("arată-mi seruri antirid")
    await _search(ctx)  # t1: fără context → pool cu tot
    stale = ctx.state_patch["active_search"]
    assert set(stale["pool"]) & UNSAFE_IDS, "precondiție: pool-ul vechi CONȚINE contraindicate"

    ctx2 = _ctx("mai arată-mi", history=[Message("inbound", "contact", "sunt însărcinată")])
    ctx2.state = ConversationState(active_search=stale)
    res = await ct.continue_search_session(ctx2, _deps(), stale, 6)
    assert {p["id"] for p in res.products} & UNSAFE_IDS == set()


# --- celelalte căi de afișare ------------------------------------------------------------------


async def test_get_product_details_refuses_contraindicated():
    ctx = _ctx("sunt însărcinată, spune-mi despre LumaDerm Renew")
    res = await run_tool(ctx, _deps(), "get_product_details", {"product_id": "unsafe-retinal"})
    assert res.ok is False and res.error == "safety_excluded"
    assert res.products == []
    assert "149" not in res.llm_view  # nici prețul nu scapă (validatorul l-ar accepta ca grounded)


async def test_get_product_details_allows_safe():
    ctx = _ctx("sunt însărcinată, spune-mi despre Bakuchiol")
    res = await run_tool(ctx, _deps(), "get_product_details", {"product_id": "safe-bakuchiol"})
    assert res.ok is True and res.products[0]["id"] == "safe-bakuchiol"


async def test_compare_refuses_when_one_is_contraindicated():
    ctx = _ctx("sunt însărcinată, compară primele două")
    res = await run_tool(
        ctx, _deps(), "compare_products", {"product_ids": ["safe-bakuchiol", "unsafe-retinal"]}
    )
    assert res.ok is False and res.error == "safety_excluded"
    assert res.products == []
    assert "LumaDerm" not in res.llm_view


async def test_compare_two_safe_products_still_works():
    ctx = _ctx("sunt însărcinată, compară-le")
    res = await run_tool(
        ctx, _deps(), "compare_products", {"product_ids": ["safe-bakuchiol", "safe-vitc"]}
    )
    assert res.ok is True and len(res.products) == 2


# --- kill-switch -------------------------------------------------------------------------------


async def test_kill_switch_off_restores_old_behaviour(monkeypatch):
    from src.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("SAFETY_CONTRAINDICATIONS_ENABLED", "false")
    try:
        ctx = _ctx("sunt însărcinată, ce ser antirid pot folosi?")
        res = await _search(ctx)
        assert {p["id"] for p in res.products} & UNSAFE_IDS  # comportamentul (nesigur) de dinainte
    finally:
        get_settings.cache_clear()
