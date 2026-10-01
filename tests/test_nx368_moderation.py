"""NX-368 — un flag de moderare nu mai reduce la tăcere o întrebare despre corp sau produse.

Conversația c13 din 2026-10-01: „se descuamează în jurul nasului și mă mănâncă" a ieșit `violence`
la `omni-moderation-latest`, iar clientul a primit „Hai să păstrăm conversația respectuoasă". Al
treilea flag în 24 h l-ar fi blocat definitiv. Acum acțiunea depinde de categorie, iar nimeni nu e
blocat automat de un clasificator.
"""

from __future__ import annotations

import pytest

from src.agent.llm import ModerationResult
from src.config import get_settings
from src.models import BusinessConfig, Contact, InboundMessage, TurnContext
from src.worker.runner import PipelineDeps
from src.worker.stages import gates
from src.worker.stages.gates import NEUTRAL_MSG


class _LLM:
    def __init__(self, categories):
        self.result = ModerationResult(flagged=True, categories=categories)

    async def moderate(self, text):
        return self.result


class _Redis:
    """Contorul ar spune „al treilea flag": nimic nu trebuie blocat."""

    async def incr(self, key):
        return 99

    async def expire(self, key, ttl):
        return None


def _ctx(body: str, language: str = "ro") -> TurnContext:
    return TurnContext(
        turn_id="t1",
        business=BusinessConfig(id="biz-1", slug="b", name="B"),
        contact=Contact(id="c1", business_id="biz-1"),
        message=InboundMessage(provider_msg_id="m1", body=body),
        conversation_id="conv-1",
        language=language,
    )


@pytest.fixture
def no_block(monkeypatch):
    calls = []

    async def fake_block(conn, business_id, contact_id):
        calls.append((business_id, contact_id))

    monkeypatch.setattr(gates, "block_contact", fake_block)
    # Contorul falsului Redis (99) ar declanșa limitarea de rată, care rulează înaintea moderării.
    monkeypatch.setattr(get_settings(), "rate_limit_enabled", False)
    assert get_settings().moderation_flag_telemetry_enabled is True  # implicitul de cod
    return calls


async def _run(body, categories, language="ro"):
    ctx = _ctx(body, language)
    await gates.gates_stage(ctx, PipelineDeps(conn=None, redis=_Redis(), llm=_LLM(categories)))
    return ctx


def _moderated(ctx):
    return [e.properties for e in ctx.events if e.type == "message_moderated"]


@pytest.mark.parametrize("category", ["violence", "violence_graphic", "sexual", "illicit"])
async def test_body_and_product_talk_flags_are_telemetry_only(category, no_block):
    ctx = await _run("se descuameaza in jurul nasului si ma mananca", [category])
    assert ctx.reply is None and ctx.halt is False  # turul continuă la agent
    assert _moderated(ctx) == [{"categories": [category], "action": "observed", "turn_id": "t1"}]
    assert no_block == []


async def test_self_harm_gets_support_not_a_reprimand(no_block):
    ctx = await _run("x", ["self_harm_intent"])
    assert ctx.reply is not None and "112" in ctx.reply.text
    assert ctx.reply.text != NEUTRAL_MSG and ctx.reply.cacheable is False
    assert _moderated(ctx)[0]["action"] == "support"
    assert no_block == []


async def test_self_harm_support_is_localized(no_block):
    ctx = await _run("x", ["self_harm"], language="en")
    assert ctx.reply.text.startswith("I'm really sorry")


async def test_minors_and_abuse_get_the_neutral_reply_without_blocking(no_block):
    for categories, action in ((["sexual_minors"], "refused"), (["harassment"], "neutral")):
        ctx = await _run("x", categories)
        assert ctx.reply.text == NEUTRAL_MSG
        assert _moderated(ctx)[0]["action"] == action
    assert no_block == []  # nici abuzul nu mai blochează automat


async def test_self_harm_wins_over_other_categories(no_block):
    ctx = await _run("x", ["violence", "self_harm"])
    assert _moderated(ctx)[0]["action"] == "support"


async def test_kill_switch_restores_the_old_path(no_block, monkeypatch):
    monkeypatch.setattr(get_settings(), "moderation_flag_telemetry_enabled", False)
    ctx = await _run("se descuameaza si ma mananca", ["violence"])
    assert ctx.reply.text == NEUTRAL_MSG
    assert no_block == [("biz-1", "c1")]
