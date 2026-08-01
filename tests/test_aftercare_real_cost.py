from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from src.agent import usage
from src.models import BusinessConfig, Contact, InboundMessage, TurnContext
from src.worker import aftercare


class _NoopDb:
    @asynccontextmanager
    async def __call__(self):
        yield object()


@pytest.mark.asyncio
async def test_run_aftercare_records_real_accumulated_cost_once(monkeypatch):
    captured = []

    async def fake_cache(*args, **kwargs):
        usage.record_embeddings(
            SimpleNamespace(usage=SimpleNamespace(prompt_tokens=50_000)),
            "text-embedding-3-small",
        )

    async def noop(*args, **kwargs):
        return None

    async def fake_add(redis, business_id, amount):
        captured.append((redis, business_id, amount))
        return amount

    monkeypatch.setattr(aftercare, "_cache_writeback", fake_cache)
    monkeypatch.setattr(aftercare, "_summarize_if_needed", noop)
    monkeypatch.setattr(aftercare, "_extract_profile_and_score", noop)
    monkeypatch.setattr(aftercare, "_persist_events", noop)
    monkeypatch.setattr(aftercare, "cost_add_and_total", fake_add)
    monkeypatch.setattr(
        aftercare,
        "get_settings",
        lambda: SimpleNamespace(cost_guard_enabled=True, daily_cost_cap_usd=5.0),
    )
    ctx = TurnContext(
        turn_id="turn-1",
        business=BusinessConfig(id="business-1", slug="demo", name="Demo"),
        contact=Contact(id="contact-1", business_id="business-1"),
        message=InboundMessage(provider_msg_id="message-1", body="ser"),
        conversation_id="conversation-1",
    )
    work = aftercare.AftercareWork(
        business=ctx.business,
        conversation_id=ctx.conversation_id,
        contact_id=ctx.contact.id,
        ctx=ctx,
        inbound_msg_id=ctx.message.provider_msg_id,
        shadow_mode=False,
        llm=object(),
        language="ro",
    )
    redis = object()

    cost_usd = await aftercare.run_aftercare(_NoopDb(), redis, work)

    assert cost_usd > 0
    assert captured == [(redis, "business-1", cost_usd)]
    assert [event.type for event in ctx.events] == ["llm_usage"]
