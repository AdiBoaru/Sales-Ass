"""P1 (ARCH-product-retrieval) — agent_stage pe follow-up „mai ieftin": re-căutare DETERMINISTĂ a
produselor strict mai ieftine (search_cheaper_than), NU re-rank pe setul afișat. Pinning-ul
bug-ului live „cea mai ieftină 80.99 când există 18.99". LLM + DB mockuite (zero apeluri reale)."""

import pytest

from src.agent import planner as planner_mod
from src.config import get_settings
from src.models import (
    BusinessConfig,
    Contact,
    ConversationState,
    InboundMessage,
    ProductRef,
    Route,
    RouteDecision,
    TurnContext,
)
from src.worker.runner import PipelineDeps
from src.worker.stages import agent as agent_mod
from src.worker.stages.agent import agent_stage


@pytest.fixture(autouse=True)
def _stub_prompt_inputs(monkeypatch):
    async def _cats(conn, business_id):
        return ["Parfumuri"]

    async def _aliases(conn, business_id, **k):
        return []

    monkeypatch.setattr(agent_mod, "list_category_names", _cats)
    monkeypatch.setattr(agent_mod, "list_routing_aliases", _aliases)


# Ce a văzut clientul la turul 1 — cel mai ieftin AFIȘAT = 80.99.
SHOWN = [
    ProductRef(product_id="p-mid", name="Pure Arc Daily 284", price=80.99),
    ProductRef(product_id="p-hi", name="Ardent Lab Calm 347", price=97.99),
    ProductRef(product_id="p-top", name="Pure Arc Calm 283", price=149.99),
]
# Produsul real mai ieftin din catalog (18.99) — pe care botul îl rata.
CHEAPER = {
    "id": "p-cheap",
    "name": "Rhea Organics Soft 466",
    "brand": "Rhea",
    "price": 18.99,
    "url": "https://shop/cheap",
    "ai_summary": "parfum lejer",
    "availability": "in_stock",
    "rating": 4.1,
    "top_pros": ["lejer"],
}


class FakeLLM:
    """Modelul nu cheamă tool (sau ar reutiliza setul afișat) — codul determinist preia. Fără
    `complete_schema` → calea rich cade pe proză (`complete`)."""

    def __init__(self, *, final="", retry="Uite o variantă mai ieftină pentru tine."):
        self._final = final
        self._retry = retry

    async def embed(self, texts, *, model=None):
        return [[0.0] * 8 for _ in texts]

    async def complete(self, system, user, *, model=None):
        return self._retry

    async def run_tool_loop(self, system, user, tools, execute, *, max_steps=3, model=None):
        return self._final


def _ctx(body="ceva mai ieftin"):
    ctx = TurnContext(
        turn_id="t",
        business=BusinessConfig(id="b", slug="d", name="D"),
        contact=Contact(id="c", business_id="b"),
        message=InboundMessage(provider_msg_id="m", body=body),
        conversation_id="conv",
        state=ConversationState(displayed_products=list(SHOWN)),
    )
    ctx.route = RouteDecision(route=Route.SALES)
    return ctx


async def test_cheaper_shows_only_strictly_cheaper_product(monkeypatch):
    captured = {}

    async def fake_cheaper(conn, business_id, ref_ids, max_excl, *, limit=6):
        captured["baseline"] = max_excl
        captured["ref_ids"] = list(ref_ids)
        return [dict(CHEAPER)]  # un SINGUR produs mai ieftin

    monkeypatch.setattr(planner_mod, "search_cheaper_than", fake_cheaper)

    ctx = _ctx()
    await agent_stage(ctx, PipelineDeps(conn=object(), redis=None, llm=FakeLLM()))

    # baseline = cel mai ieftin AFIȘAT (80.99), nu 18.99; ref-urile = produsele afișate
    assert captured["baseline"] == 80.99
    assert set(captured["ref_ids"]) == {"p-mid", "p-hi", "p-top"}
    # reply produs, cu cardurile DOAR pe produsul mai ieftin (1 dacă e 1, zero padding)
    assert ctx.reply is not None
    ids = [p["product_id"] for p in (ctx.reply.products or [])]
    assert ids == ["p-cheap"]  # NU re-arată setul vechi (80.99/97.99/149.99)


async def test_no_cheaper_returns_graceful_message(monkeypatch):
    async def empty_cheaper(conn, business_id, ref_ids, max_excl, *, limit=6):
        return []

    monkeypatch.setattr(planner_mod, "search_cheaper_than", empty_cheaper)

    ctx = _ctx()
    await agent_stage(ctx, PipelineDeps(conn=object(), redis=None, llm=FakeLLM()))

    assert ctx.reply is not None
    assert "cea mai ieftină" in ctx.reply.text.lower()  # niciodată tăcere/padding (P6)
    assert ctx.reply.cacheable is False  # context-relativ → nu se cache-uiește
    assert not ctx.reply.products  # fără carduri


async def test_killswitch_off_skips_deterministic_cheaper(monkeypatch):
    monkeypatch.setattr(get_settings(), "cheaper_intent_enabled", False)
    called = {"n": 0}

    async def cheaper_spy(*a, **k):
        called["n"] += 1
        return [dict(CHEAPER)]

    async def fake_by_ids(conn, business_id, ids, **k):
        return []  # R3 (calea veche) — nu reutilizează nimic în test

    monkeypatch.setattr(planner_mod, "search_cheaper_than", cheaper_spy)
    monkeypatch.setattr(planner_mod, "get_products_by_ids", fake_by_ids)

    ctx = _ctx()
    await agent_stage(ctx, PipelineDeps(conn=object(), redis=None, llm=FakeLLM(final="")))

    assert called["n"] == 0  # cu flag OFF, calea deterministă „mai ieftin" e sărită
