"""NX-131 — ramura deterministă de LINK (cerere „trimite-mi linkul" pe un produs deja arătat).

Garanție: o cerere de link NU mai intră în calea rich (care interzice modelului linkurile și
producea bucla de coaching repetat — „partea asta e foarte repetitiva"). E servită direct din
state → product_url proaspăt → Offer(open_url) + card, FĂRĂ bucla LLM. product_url NULL (gaură de
date demo) → mesaj ONEST, NU link inventat (PP-F4). Stub-uri DB/LLM, zero apeluri reale.
"""

import pytest

from src.config import get_settings
from src.models import (
    BusinessConfig,
    Contact,
    InboundMessage,
    ProductRef,
    Route,
    RouteDecision,
    TurnContext,
)
from src.worker.runner import PipelineDeps
from src.worker.stages import agent as agent_mod
from src.worker.stages.agent import _LINK_RE, agent_stage


@pytest.fixture(autouse=True)
def _stub_prompt_inputs(monkeypatch):
    """Pe căile care CAD pe bucla LLM (link_intent skipped) agent_stage citește categorii/aliase
    din DB pt promptul generat → stubbim cele două query-uri (conn=object(), fără DB reală)."""

    async def _cats(conn, business_id):
        return ["Creme"]

    async def _aliases(conn, business_id, **k):
        return []

    monkeypatch.setattr(agent_mod, "list_category_names", _cats)
    monkeypatch.setattr(agent_mod, "list_routing_aliases", _aliases)


class _NoLoopLLM:
    """Eșuează dacă intră în bucla LLM — dovedește că link_intent e DETERMINIST (fără inferență)."""

    async def embed(self, texts, *, model=None):
        return [[0.0] * 8 for _ in texts]

    async def run_tool_loop(self, *a, **k):
        raise AssertionError("link_intent NU trebuie să cheme bucla LLM")

    async def complete(self, *a, **k):
        raise AssertionError("link_intent NU trebuie să cheme LLM")


class _RecordLLM(_NoLoopLLM):
    """Înregistrează dacă bucla LLM a fost atinsă (link_intent NU s-a declanșat → fall-through)."""

    def __init__(self):
        self.loop_called = False

    async def run_tool_loop(self, *a, **k):
        self.loop_called = True
        return ""


def _ctx(body: str) -> TurnContext:
    ctx = TurnContext(
        turn_id="t",
        business=BusinessConfig(id="b", slug="d", name="D"),
        contact=Contact(id="c", business_id="b"),
        message=InboundMessage(provider_msg_id="m", body=body),
        conversation_id="conv",
    )
    ctx.route = RouteDecision(route=Route.SALES)
    ctx.state.displayed_products = [ProductRef("p1", "Mira Soft 389", 60.99)]
    return ctx


def _deps(llm=None) -> PipelineDeps:
    return PipelineDeps(conn=object(), redis=None, llm=llm or _NoLoopLLM())


# --- regexul de intenție -----------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "imi dai te rog linkul direct catre crema asta?",
        "Trimite-mi linkul, te rog",
        "dă-mi link",
        "Care e link-ul?",
        "unde pot cumpăra crema asta?",
        "unde o găsesc?",
        "where can I buy this?",
        "where to buy",
    ],
)
def test_link_re_matches(text):
    assert _LINK_RE.search(text) is not None


@pytest.mark.parametrize(
    "text",
    ["vreau o cremă", "care e cea mai bună?", "compară primele două", "ceva mai ieftin"],
)
def test_link_re_no_false_positive(text):
    assert _LINK_RE.search(text) is None


# --- ramura din agent_stage --------------------------------------------------


async def test_link_intent_emits_offer_and_card(monkeypatch):
    """Un produs cu product_url → buton open_url + card, FĂRĂ rich/coaching, fără bucla LLM."""

    async def fake_by_ids(conn, business_id, ids, **k):
        assert ids == ["p1"]
        return [
            {
                "id": "p1",
                "name": "Mira Soft 389",
                "price": 60.99,
                "url": "https://shop.ro/p/mira-389",
            }
        ]

    monkeypatch.setattr("src.agent.deterministic.get_products_by_ids", fake_by_ids)
    ctx = _ctx("imi dai linkul direct catre crema asta?")
    await agent_stage(ctx, _deps())

    assert ctx.reply is not None
    assert ctx.reply.offer is not None and ctx.reply.offer.kind == "open_url"
    assert ctx.reply.offer.url == "https://shop.ro/p/mira-389"
    assert ctx.reply.products and ctx.reply.products[0]["url"] == "https://shop.ro/p/mira-389"
    assert ctx.reply.cacheable is False
    # NU re-recomandare bogată / coaching repetat: fără rich, fără comparison
    assert ctx.reply.rich is None and ctx.reply.comparison is None
    assert any(e.type == "link_intent" and e.properties["served"] == 1 for e in ctx.events)


async def test_link_intent_multiple_products_no_single_offer(monkeypatch):
    """Mai multe produse → cardurile SUNT linkurile (fiecare cu url), fără buton arbitrar."""

    async def fake_by_ids(conn, business_id, ids, **k):
        return [
            {"id": "p1", "name": "A", "price": 31.99, "url": "https://shop.ro/p/a"},
            {"id": "p2", "name": "B", "price": 60.99, "url": "https://shop.ro/p/b"},
        ]

    monkeypatch.setattr("src.agent.deterministic.get_products_by_ids", fake_by_ids)
    ctx = _ctx("trimite-mi linkurile")
    ctx.state.displayed_products = [ProductRef("p1", "A", 31.99), ProductRef("p2", "B", 60.99)]
    await agent_stage(ctx, _deps())

    assert ctx.reply is not None and ctx.reply.offer is None  # fără buton arbitrar pe mai multe
    assert len(ctx.reply.products) == 2 and all(c.get("url") for c in ctx.reply.products)


async def test_link_intent_no_product_url_is_honest(monkeypatch):
    """product_url NULL (gaură de date demo) → mesaj onest, NU link inventat, fără Offer cu url."""

    async def fake_by_ids(conn, business_id, ids, **k):
        return [{"id": "p1", "name": "Mira Soft 389", "price": 60.99, "url": None}]

    monkeypatch.setattr("src.agent.deterministic.get_products_by_ids", fake_by_ids)
    ctx = _ctx("trimite-mi linkul, te rog")
    await agent_stage(ctx, _deps())

    assert ctx.reply is not None
    assert ctx.reply.offer is None  # fără url → fără ofertă de link
    assert "http" not in ctx.reply.text  # niciun link inventat
    assert ctx.reply.cacheable is False
    assert any(e.type == "link_intent" and e.properties["served"] == 0 for e in ctx.events)


async def test_link_intent_skipped_when_no_displayed_products(monkeypatch):
    """Fără produse afișate → NU e follow-up; cade pe bucla LLM normală (caută fresh)."""

    async def by_ids(conn, business_id, ids, **k):
        return []

    monkeypatch.setattr("src.worker.stages.agent.get_products_by_ids", by_ids)
    ctx = _ctx("trimite-mi linkul")
    ctx.state.displayed_products = []  # nimic afișat
    llm = _RecordLLM()
    await agent_stage(ctx, _deps(llm))

    assert llm.loop_called is True  # link_intent skipped → bucla LLM
    assert not any(e.type == "link_intent" for e in ctx.events)


async def test_link_with_new_filter_falls_through_to_llm(monkeypatch):
    """„link la o cremă sub 50" = căutare NOUĂ cu link (filtru nou din triaj) → bucla LLM, NU
    linkul produselor vechi din state."""

    async def by_ids(conn, business_id, ids, **k):
        return []

    monkeypatch.setattr("src.worker.stages.agent.get_products_by_ids", by_ids)
    ctx = _ctx("trimite-mi linkul la o cremă sub 50 lei")
    ctx.route.filters = {"budget_max": 50}  # triajul a extras o constrângere nouă
    llm = _RecordLLM()
    await agent_stage(ctx, _deps(llm))

    assert llm.loop_called is True  # filtru nou → fall-through, nu link_intent
    assert not any(e.type == "link_intent" for e in ctx.events)


async def test_link_intent_disabled_falls_through(monkeypatch):
    """Kill-switch OFF → fără ramura deterministă; cade pe bucla LLM (comportament vechi)."""

    async def by_ids(conn, business_id, ids, **k):
        return []

    monkeypatch.setattr("src.worker.stages.agent.get_products_by_ids", by_ids)
    monkeypatch.setattr(get_settings(), "link_intent_enabled", False)
    ctx = _ctx("trimite-mi linkul")
    llm = _RecordLLM()
    await agent_stage(ctx, _deps(llm))

    assert llm.loop_called is True
    assert not any(e.type == "link_intent" for e in ctx.events)
