"""G5a — Gates (stagiul 3): detect_risk + porțile bot_active / handoff / risc.

Unit, fără DB/LLM: `detect_risk` e pur; `gates_stage` cu `set_handoff`
monkeypatch-uit. Verifică tăcerea intenționată (halt) și escaladarea la om.
"""

from datetime import UTC, datetime, timedelta

from src.agent.llm import LLMClient, ModerationResult
from src.models import BusinessConfig, Contact, InboundMessage, TurnContext
from src.worker.runner import PipelineDeps
from src.worker.stages import gates
from src.worker.stages.gates import NEUTRAL_MSG, detect_risk

# --- detect_risk (pur, fără LLM) ---------------------------------------------


def test_detect_risk_human_request():
    assert detect_risk("vreau sa vorbesc cu un om") == "human_request"
    # diacritice + uppercase normalizate
    assert detect_risk("Vreau să vorbesc cu un OM") == "human_request"
    assert detect_risk("dați-mi un operator uman") == "human_request"


def test_detect_risk_legal_complaint():
    assert detect_risk("chem avocatul") == "legal_complaint"
    assert detect_risk("fac reclamație la ANAF") == "legal_complaint"
    assert detect_risk("vă dau în judecată") == "legal_complaint"


def test_detect_risk_negative():
    assert detect_risk("caut o cremă pentru ten uscat") is None
    assert detect_risk("") is None
    assert detect_risk(None) is None


# --- gates_stage -------------------------------------------------------------


def _ctx(
    body: str = "salut",
    *,
    bot_active: bool = True,
    handoff_until=None,
    is_blocked: bool = False,
) -> TurnContext:
    return TurnContext(
        turn_id="t1",
        business=BusinessConfig(id="biz-1", slug="s", name="n"),
        contact=Contact(id="c1", business_id="biz-1", is_blocked=is_blocked),
        message=InboundMessage(provider_msg_id="m1", body=body),
        conversation_id="conv-1",
        bot_active=bot_active,
        handoff_until=handoff_until,
    )


async def test_bot_inactive_halts_silent():
    ctx = _ctx(bot_active=False)
    await gates.gates_stage(ctx, PipelineDeps(conn=None))
    assert ctx.halt is True
    assert ctx.reply is None
    assert any(
        e.type == "gate_halt" and e.properties["reason"] == "bot_inactive" for e in ctx.events
    )


async def test_handoff_active_halts_silent():
    ctx = _ctx(handoff_until=datetime.now(UTC) + timedelta(minutes=10))
    await gates.gates_stage(ctx, PipelineDeps(conn=None))
    assert ctx.halt is True
    assert ctx.reply is None


async def test_handoff_expired_does_not_halt():
    ctx = _ctx(handoff_until=datetime.now(UTC) - timedelta(minutes=1))
    await gates.gates_stage(ctx, PipelineDeps(conn=None))
    assert ctx.halt is False
    assert ctx.reply is None  # continuă la triaj


async def test_risk_escalates_and_replies(monkeypatch):
    calls = {}

    async def fake_set_handoff(conn, bid, conv_id, *, window_minutes, risk_flag, **kw):
        calls["risk_flag"] = risk_flag
        calls["window"] = window_minutes
        calls["business_id"] = bid

    monkeypatch.setattr(gates, "set_handoff", fake_set_handoff)

    ctx = _ctx(body="vreau sa vorbesc cu un om")
    await gates.gates_stage(ctx, PipelineDeps(conn=None))

    assert calls["risk_flag"] == "human_request"
    assert calls["business_id"] == "biz-1"
    assert calls["window"] > 0
    assert ctx.reply is not None
    assert "coleg" in ctx.reply.text.lower()
    assert ctx.reply.cacheable is False  # NX-126: escaladarea NU se cache-uiește (cache-poison)
    assert ctx.halt is False  # are reply (tranziție), nu halt
    assert any(
        e.type == "handoff_requested" and e.properties["reason"] == "human_request"
        for e in ctx.events
    )


async def test_normal_message_passes(monkeypatch):
    # set_handoff NU trebuie atins pe un mesaj normal
    async def boom(*a, **k):
        raise AssertionError("set_handoff nu trebuie apelat pe mesaj normal")

    monkeypatch.setattr(gates, "set_handoff", boom)
    ctx = _ctx(body="caut o cremă")
    await gates.gates_stage(ctx, PipelineDeps(conn=None))
    assert ctx.halt is False
    assert ctx.reply is None


# --- moderation gate (NX-15) -------------------------------------------------


class _FakeLLM:
    def __init__(self, result=None, raise_exc=None):
        self._result = result
        self._raise = raise_exc
        self.called = False

    async def moderate(self, text):
        self.called = True
        if self._raise is not None:
            raise self._raise
        return self._result


class _FakeRedis:
    def __init__(self, count=1):
        self._count = count
        self.expired = None

    async def incr(self, key):
        return self._count

    async def expire(self, key, ttl):
        self.expired = (key, ttl)


async def test_moderation_flagged_replies_neutral():
    llm = _FakeLLM(ModerationResult(flagged=True, categories=["harassment"]))
    ctx = _ctx(body="mesaj abuziv")
    await gates.gates_stage(ctx, PipelineDeps(conn=None, redis=None, llm=llm))

    assert llm.called is True
    assert ctx.reply is not None and ctx.reply.text == NEUTRAL_MSG
    assert ctx.reply.cacheable is False
    assert ctx.halt is False
    assert any(
        e.type == "message_moderated" and e.properties["categories"] == ["harassment"]
        for e in ctx.events
    )


async def test_moderation_clean_passes():
    llm = _FakeLLM(ModerationResult(flagged=False, categories=[]))
    ctx = _ctx(body="caut o cremă")
    await gates.gates_stage(ctx, PipelineDeps(conn=None, redis=None, llm=llm))

    assert llm.called is True
    assert ctx.reply is None and ctx.halt is False


async def test_moderation_threshold_blocks(monkeypatch):
    blocked = {}

    async def fake_block(conn, business_id, contact_id):
        blocked["args"] = (business_id, contact_id)

    monkeypatch.setattr(gates, "block_contact", fake_block)

    llm = _FakeLLM(ModerationResult(flagged=True, categories=["hate"]))
    redis = _FakeRedis(count=3)  # al 3-lea flag = prag implicit
    ctx = _ctx(body="iar mizerii")
    await gates.gates_stage(ctx, PipelineDeps(conn=None, redis=redis, llm=llm))

    assert blocked["args"] == ("biz-1", "c1")
    assert any(e.type == "contact_blocked" and e.properties["flag_count"] == 3 for e in ctx.events)
    assert ctx.reply.text == NEUTRAL_MSG  # tot primește răspuns neutru


async def test_blocked_contact_halts_before_moderation():
    # contact blocat → halt înainte de orice apel de moderation
    llm = _FakeLLM(raise_exc=AssertionError("moderation NU trebuie apelat pe contact blocat"))
    ctx = _ctx(body="orice", is_blocked=True)
    await gates.gates_stage(ctx, PipelineDeps(conn=None, redis=None, llm=llm))

    assert ctx.halt is True
    assert ctx.reply is None
    assert llm.called is False
    assert any(
        e.type == "gate_halt" and e.properties["reason"] == "contact_blocked" for e in ctx.events
    )


async def test_moderation_failopen_on_error(monkeypatch):
    # API jos → fail-open: mesajul trece (fără reply de moderation), risc încă evaluat
    async def boom(*a, **k):
        raise AssertionError("mesaj curat → fără handoff")

    monkeypatch.setattr(gates, "set_handoff", boom)
    llm = _FakeLLM(raise_exc=RuntimeError("moderation API down"))
    ctx = _ctx(body="caut un parfum")
    await gates.gates_stage(ctx, PipelineDeps(conn=None, redis=None, llm=llm))

    assert ctx.reply is None  # fail-open: nu setează neutral
    assert ctx.halt is False


async def test_moderation_skipped_when_no_llm():
    # fără cheie (llm=None) → moderation sărit; mesajul normal trece ca înainte
    ctx = _ctx(body="caut o cremă")
    await gates.gates_stage(ctx, PipelineDeps(conn=None, redis=None, llm=None))
    assert ctx.reply is None and ctx.halt is False


# --- adaptor moderate() ------------------------------------------------------


class _FakeCategories:
    def model_dump(self):
        return {"harassment": True, "hate": False, "violence": True}


class _FakeModResult:
    flagged = True
    categories = _FakeCategories()


class _FakeModResp:
    results = [_FakeModResult()]


class _FakeModerations:
    async def create(self, *, model, input):
        return _FakeModResp()


class _FakeOpenAI:
    moderations = _FakeModerations()


async def test_adapter_moderate_parses_flagged_categories():
    llm = LLMClient(_FakeOpenAI(), model_triage="t", model_agent="a")
    res = await llm.moderate("ceva")
    assert res.flagged is True
    assert res.categories == ["harassment", "violence"]  # sortate, doar True


# --- rate limit (G2c) --------------------------------------------------------


async def test_rate_limit_under_threshold_passes(monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("mesaj normal sub prag → fără handoff")

    monkeypatch.setattr(gates, "set_handoff", boom)
    ctx = _ctx(body="salut")
    # count=5 ≤ max(20) → trece (llm=None → moderation sărit)
    await gates.gates_stage(ctx, PipelineDeps(conn=None, redis=_FakeRedis(count=5), llm=None))
    assert ctx.reply is None and ctx.halt is False


async def test_rate_limit_crossing_sends_throttle():
    ctx = _ctx(body="spam spam spam")
    # count == max+1 (21) → un singur mesaj de throttle
    await gates.gates_stage(ctx, PipelineDeps(conn=None, redis=_FakeRedis(count=21), llm=None))
    assert ctx.reply is not None and "multe mesaje" in ctx.reply.text.lower()
    assert ctx.reply.cacheable is False
    assert any(e.type == "rate_limited" and e.properties["count"] == 21 for e in ctx.events)


async def test_rate_limit_already_over_halts_silent():
    ctx = _ctx(body="spam")
    # count > max+1 (25) → tăcere (nu re-trimite throttle)
    await gates.gates_stage(ctx, PipelineDeps(conn=None, redis=_FakeRedis(count=25), llm=None))
    assert ctx.halt is True and ctx.reply is None
    assert any(e.type == "rate_limited" for e in ctx.events)


async def test_rate_limit_no_redis_is_noop(monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("fără redis → rate limit no-op")

    monkeypatch.setattr(gates, "set_handoff", boom)
    ctx = _ctx(body="salut")
    await gates.gates_stage(ctx, PipelineDeps(conn=None, redis=None, llm=None))
    assert ctx.reply is None and ctx.halt is False
