"""G5a — Gates (stagiul 3): detect_risk + porțile bot_active / handoff / risc.

Unit, fără DB/LLM: `detect_risk` e pur; `gates_stage` cu `set_handoff`
monkeypatch-uit. Verifică tăcerea intenționată (halt) și escaladarea la om.
"""

from datetime import UTC, datetime, timedelta

from src.models import BusinessConfig, Contact, InboundMessage, TurnContext
from src.worker.runner import PipelineDeps
from src.worker.stages import gates
from src.worker.stages.gates import detect_risk

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


def _ctx(body: str = "salut", *, bot_active: bool = True, handoff_until=None) -> TurnContext:
    return TurnContext(
        turn_id="t1",
        business=BusinessConfig(id="biz-1", slug="s", name="n"),
        contact=Contact(id="c1", business_id="biz-1"),
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
