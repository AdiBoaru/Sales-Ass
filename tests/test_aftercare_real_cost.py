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
    persisted: list[list] = []

    async def spy_persist(conn, business_id, conversation_id, contact_id, events):
        persisted.append([e.type for e in events])

    monkeypatch.setattr(aftercare, "_persist_events", spy_persist)
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
    # NX-241 adaugă `aftercare_lag_ms` (cât a durat munca de fundal + outcome). NX-300 adaugă al
    # doilea `turn_latency`, `phase=post_turn` — perechea lui `llm_usage`, pentru timp.
    assert [event.type for event in ctx.events] == [
        "aftercare_lag_ms",
        "llm_usage",
        "turn_latency",
    ]
    # NX-300: invariantul nu mai e ORDINEA, ci acoperirea. Persistarea lua `events[-1]`, deci
    # ultimul emis îl împingea tăcut afară pe celălalt; acum se scrie toată coada, iar testul cere
    # ca AMBELE evenimente post-tur să ajungă în analytics — nu ca unul să fie norocos.
    assert persisted == [["llm_usage", "turn_latency"]]
    post = next(e for e in ctx.events if e.type == "turn_latency")
    assert post.properties["phase"] == "post_turn"  # nu poluează bugetul turului
    assert "aftercare" in post.properties["phases"]  # faza care n-avea producător
