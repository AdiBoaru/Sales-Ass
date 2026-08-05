"""Follow-up-uri de recenzii ancorate în produsele afișate, fără LLM sau alegere arbitrară."""

from types import SimpleNamespace

import pytest

from src.agent import deterministic as det
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
from src.worker.stages.agent import agent_stage


class _NoLoopLLM:
    async def run_tool_loop(self, *args, **kwargs):
        raise AssertionError("review follow-up must not enter the LLM loop")


def _ctx(body: str, *, refs: list[ProductRef] | None = None) -> TurnContext:
    ctx = TurnContext(
        turn_id="turn-review",
        business=BusinessConfig(id="biz-1", slug="demo", name="Demo"),
        contact=Contact(id="contact-1", business_id="biz-1"),
        message=InboundMessage(provider_msg_id="msg-1", body=body),
        conversation_id="conv-1",
    )
    ctx.route = RouteDecision(route=Route.SALES)
    ctx.state.displayed_products = refs or [ProductRef("p1", "Solora Shield Cremă SPF 50", 54.99)]
    return ctx


def _product(product_id: str = "p1", name: str = "Solora Shield Cremă SPF 50") -> dict:
    return {
        "id": product_id,
        "name": name,
        "price": 54.99,
        "url": f"https://shop.test/{product_id}",
        "image": None,
        "review_summary": "Nu lasă film alb; confortabilă sub fond de ten.",
        "top_pros": ["finish non-gras", "fără urme albe"],
        "top_cons": ["necesită reaplicare"],
        "rating": 4.6,
        "review_count": 233,
    }


@pytest.mark.parametrize(
    "text",
    ["Vezi recenzii", "ce păreri are?", "customer reviews", "Vélemények"],
)
def test_review_intent_matches_multilingual_text(text):
    assert det._REVIEW_RE.search(det._norm_followup(text)) is not None


@pytest.mark.parametrize("text", ["mai ieftin", "vezi produsul", "compară primele două"])
def test_review_intent_does_not_match_other_actions(text):
    assert det._REVIEW_RE.search(det._norm_followup(text)) is None


def test_numeric_choice_is_contextual_not_any_digit_in_product_name():
    refs = [
        ProductRef("p1", "Ser Formula 2", 40.0),
        ProductRef("p2", "NudeLab Soft Blush", 22.74),
    ]
    assert det._resolve_review_product("Recenzii 2", refs).product_id == "p2"
    assert det._resolve_review_product("Recenzii Ser Formula 2", refs).product_id == "p1"


async def test_single_product_returns_real_review_evidence_without_llm(monkeypatch):
    async def fake_by_ids(conn, business_id, ids, **kwargs):
        assert business_id == "biz-1"
        assert ids == ["p1"]
        return [_product()]

    monkeypatch.setattr(det, "get_products_by_ids", fake_by_ids)
    ctx = _ctx("Vezi recenzii")
    ctx.route.filters = {"price_max": 100, "concerns": ["cadou pentru ea"]}

    assert await det.try_pre_intents(ctx, SimpleNamespace(conn=object())) is True

    assert "Nu lasă film alb" in ctx.reply.text
    assert "finish non-gras" in ctx.reply.text
    assert "necesită reaplicare" in ctx.reply.text
    assert "4,6/5 din 233" in ctx.reply.text
    assert ctx.reply.products[0]["product_id"] == "p1"
    assert "Vezi recenzii" not in ctx.reply.suggestions
    assert ctx.retrieval.source == "review_intent"


async def test_review_followup_runs_end_to_end_through_agent_stage(monkeypatch):
    async def fake_by_ids(conn, business_id, ids, **kwargs):
        return [_product()]

    monkeypatch.setattr(det, "get_products_by_ids", fake_by_ids)
    ctx = _ctx("Vezi recenzii")

    await agent_stage(ctx, PipelineDeps(conn=object(), llm=_NoLoopLLM()))

    assert ctx.reply is not None
    assert ctx.reply.products[0]["product_id"] == "p1"
    assert "Nu lasă film alb" in ctx.reply.text
    assert any(event.type == "review_intent" for event in ctx.events)


async def test_generic_reviews_with_multiple_products_clarifies_without_db(monkeypatch):
    async def should_not_fetch(*args, **kwargs):
        raise AssertionError("un produs ambiguu nu trebuie ales arbitrar")

    monkeypatch.setattr(det, "get_products_by_ids", should_not_fetch)
    refs = [
        ProductRef("p1", "Solora Shield Cremă SPF 50", 54.99),
        ProductRef("p2", "NudeLab Soft Blush", 22.74),
        ProductRef("p3", "NudeLab Fresh Cremă CC", 69.99),
    ]
    ctx = _ctx("Vezi recenzii", refs=refs)

    assert await det.try_pre_intents(ctx, SimpleNamespace(conn=object())) is True

    assert ctx.reply.pending_question["field"] == "product_for_reviews"
    assert ctx.reply.text == "Pentru care produs vrei să vezi recenziile?"
    assert len(ctx.reply.suggestions) == 3
    assert all("Recenzii:" in suggestion for suggestion in ctx.reply.suggestions)
    assert ctx.reply.suggestions[1].startswith("Recenzii: 2.")
    assert any(
        event.type == "review_intent" and event.properties["reason"] == "ambiguous"
        for event in ctx.events
    )


async def test_ordinal_resolves_second_displayed_product(monkeypatch):
    seen: list[str] = []

    async def fake_by_ids(conn, business_id, ids, **kwargs):
        seen.extend(ids)
        return [_product("p2", "NudeLab Soft Blush")]

    monkeypatch.setattr(det, "get_products_by_ids", fake_by_ids)
    refs = [
        ProductRef("p1", "Solora Shield Cremă SPF 50", 54.99),
        ProductRef("p2", "NudeLab Soft Blush", 22.74),
    ]
    ctx = _ctx("Arată-mi recenziile pentru a doua", refs=refs)

    assert await det.try_pre_intents(ctx, SimpleNamespace(conn=object())) is True
    assert seen == ["p2"]
    assert ctx.reply.products[0]["product_id"] == "p2"


async def test_pending_review_question_accepts_bare_ordinal(monkeypatch):
    seen: list[str] = []

    async def fake_by_ids(conn, business_id, ids, **kwargs):
        seen.extend(ids)
        return [_product("p2", "NudeLab Soft Blush")]

    monkeypatch.setattr(det, "get_products_by_ids", fake_by_ids)
    refs = [
        ProductRef("p1", "Solora Shield Cremă SPF 50", 54.99),
        ProductRef("p2", "NudeLab Soft Blush", 22.74),
    ]
    ctx = _ctx("a doua", refs=refs)
    ctx.state.pending_question = {"field": "product_for_reviews", "attempts": 1}

    assert await det.try_pre_intents(ctx, SimpleNamespace(conn=object())) is True
    assert seen == ["p2"]


async def test_pending_review_question_does_not_capture_a_new_intent():
    refs = [
        ProductRef("p1", "Solora Shield Cremă SPF 50", 54.99),
        ProductRef("p2", "NudeLab Soft Blush", 22.74),
    ]
    ctx = _ctx("nu, vreau ceva mai ieftin", refs=refs)
    ctx.state.pending_question = {"field": "product_for_reviews", "attempts": 1}

    assert await det.try_pre_intents(ctx, SimpleNamespace(conn=object())) is False
    assert ctx.reply is None


async def test_product_name_resolves_displayed_product(monkeypatch):
    seen: list[str] = []

    async def fake_by_ids(conn, business_id, ids, **kwargs):
        seen.extend(ids)
        return [_product("p2", "NudeLab Soft Blush")]

    monkeypatch.setattr(det, "get_products_by_ids", fake_by_ids)
    refs = [
        ProductRef("p1", "Solora Shield Cremă SPF 50", 54.99),
        ProductRef("p2", "NudeLab Soft Blush", 22.74),
    ]
    ctx = _ctx("Recenzii NudeLab Soft Blush", refs=refs)

    assert await det.try_pre_intents(ctx, SimpleNamespace(conn=object())) is True
    assert seen == ["p2"]


async def test_missing_review_data_is_honest_and_never_silent(monkeypatch):
    async def fake_by_ids(conn, business_id, ids, **kwargs):
        product = _product()
        product.update(review_summary=None, top_pros=[], top_cons=[], rating=None, review_count=0)
        return [product]

    monkeypatch.setattr(det, "get_products_by_ids", fake_by_ids)
    ctx = _ctx("Ce păreri are?")

    assert await det.try_pre_intents(ctx, SimpleNamespace(conn=object())) is True
    assert ctx.reply is not None
    assert "Nu am încă suficiente date" in ctx.reply.text


async def test_unsafe_review_claim_is_not_repeated(monkeypatch):
    async def fake_by_ids(conn, business_id, ids, **kwargs):
        product = _product()
        product.update(
            review_summary="Tratează acneea garantat",
            top_pros=[],
            top_cons=[],
            rating=None,
            review_count=0,
        )
        return [product]

    monkeypatch.setattr(det, "get_products_by_ids", fake_by_ids)
    ctx = _ctx("Vezi recenzii")

    assert await det.try_pre_intents(ctx, SimpleNamespace(conn=object())) is True
    assert "Tratează acneea" not in ctx.reply.text
    assert "Nu am încă suficiente date" in ctx.reply.text


async def test_no_displayed_product_falls_through():
    ctx = _ctx("Vezi recenzii")
    ctx.state.displayed_products = []
    assert await det.try_pre_intents(ctx, SimpleNamespace(conn=object())) is False
    assert ctx.reply is None


async def test_review_intent_kill_switch_falls_through(monkeypatch):
    monkeypatch.setattr(get_settings(), "review_intent_enabled", False)
    ctx = _ctx("Vezi recenzii")
    assert await det.try_pre_intents(ctx, SimpleNamespace(conn=object())) is False
    assert ctx.reply is None
