"""NX-78 cost obs — captarea usage-ului LLM (tokeni + cached + cost) + emiterea `llm_usage`.

Pricing pur + acumulator + record_* defensiv + integrare runner/adaptor. ZERO OpenAI real.
"""

from dataclasses import dataclass

from src.agent import usage
from src.agent.llm import LLMClient
from src.agent.pricing import cost_for, rates_for
from src.models import BusinessConfig, Contact, InboundMessage, TurnContext
from src.worker.runner import PipelineDeps, run_pipeline

# --- fake-uri usage OpenAI ---------------------------------------------------


@dataclass
class _Details:
    cached_tokens: int


@dataclass
class _Usage:
    prompt_tokens: int
    completion_tokens: int
    prompt_tokens_details: _Details | None = None


class _Msg:
    content = "ok"
    tool_calls = None


class _Choice:
    message = _Msg()


class _Resp:
    def __init__(self, usage=None):
        self.usage = usage
        self.choices = [_Choice()]


# --- pricing -----------------------------------------------------------------


def test_cost_for_separates_cached():
    # mini: input 0.25, cached 0.025, output 2.0 /1M
    r = rates_for("gpt-5.4-mini")
    full = cost_for("gpt-5.4-mini", prompt_tokens=1000, cached_tokens=0, completion_tokens=100)
    cached = cost_for("gpt-5.4-mini", prompt_tokens=1000, cached_tokens=800, completion_tokens=100)
    assert cached < full  # tokenii cached sunt mai ieftini → economie
    expected = (200 * r.input + 800 * r.cached_input + 100 * r.output) / 1_000_000
    assert abs(cached - expected) < 1e-12


def test_cost_for_unknown_model_uses_default():
    assert cost_for("model-x", 1000, 0, 0) == cost_for("gpt-5.4-mini", 1000, 0, 0)


def test_cost_for_clamps_cached_over_prompt():
    # cached > prompt (date corupte) → clamp la prompt, fără cost negativ
    c = cost_for("gpt-5.4-nano", prompt_tokens=100, cached_tokens=999, completion_tokens=0)
    assert c >= 0


# --- acumulator + record -----------------------------------------------------


def test_accumulator_add():
    acc = usage.UsageAccumulator()
    acc.add("gpt-5.4-nano", 100, 50, 0)
    acc.add("gpt-5.4-mini", 200, 80, 150)
    assert acc.calls == 2
    assert acc.tokens_in == 300 and acc.tokens_out == 130 and acc.cached_tokens == 150
    assert acc.cost_usd > 0


def test_record_chat_into_active_accumulator():
    acc, token = usage.push()
    try:
        resp = _Resp(
            _Usage(
                prompt_tokens=1000,
                completion_tokens=200,
                prompt_tokens_details=_Details(cached_tokens=768),
            )
        )
        usage.record_chat(resp, "gpt-5.4-mini")
    finally:
        usage.pop(token)
    assert acc.tokens_in == 1000 and acc.tokens_out == 200 and acc.cached_tokens == 768
    assert acc.calls == 1


def test_record_chat_handles_dict_usage():
    acc, token = usage.push()
    try:
        resp = _Resp(
            {
                "prompt_tokens": 50,
                "completion_tokens": 10,
                "prompt_tokens_details": {"cached_tokens": 32},
            }
        )
        usage.record_chat(resp, "gpt-5.4-nano")
    finally:
        usage.pop(token)
    assert acc.tokens_in == 50 and acc.cached_tokens == 32


def test_record_chat_missing_usage_is_noop():
    acc, token = usage.push()
    try:
        usage.record_chat(_Resp(usage=None), "gpt-5.4-mini")  # fake fără usage (ca testele vechi)
    finally:
        usage.pop(token)
    assert acc.calls == 0


def test_record_without_accumulator_is_noop():
    # fără push() activ → record e no-op, nu aruncă
    usage.record_chat(_Resp(_Usage(10, 5)), "gpt-5.4-mini")


def test_record_embeddings_counts_prompt_only():
    acc, token = usage.push()
    try:
        usage.record_embeddings(_Resp({"prompt_tokens": 40}), "text-embedding-3-small")
    finally:
        usage.pop(token)
    assert acc.tokens_in == 40 and acc.tokens_out == 0 and acc.cached_tokens == 0


# --- integrare adaptor -------------------------------------------------------


class _Completions:
    async def create(self, **kwargs):
        return _Resp(
            _Usage(
                prompt_tokens=900,
                completion_tokens=120,
                prompt_tokens_details=_Details(cached_tokens=512),
            )
        )


class _FakeOpenAI:
    def __init__(self):
        self.chat = type("Chat", (), {"completions": _Completions()})()


async def test_adapter_complete_records_usage():
    llm = LLMClient(_FakeOpenAI(), model_triage="gpt-5.4-nano", model_agent="gpt-5.4-mini")
    acc, token = usage.push()
    try:
        await llm.complete("sys", "user")
    finally:
        usage.pop(token)
    assert acc.tokens_in == 900 and acc.cached_tokens == 512 and acc.calls == 1


# --- integrare runner (emite llm_usage) --------------------------------------


def _ctx() -> TurnContext:
    return TurnContext(
        turn_id="t",
        business=BusinessConfig(id="b", slug="d", name="D"),
        contact=Contact(id="c", business_id="b"),
        message=InboundMessage(provider_msg_id="m", body="x"),
        conversation_id="conv",
    )


async def test_runner_emits_llm_usage_event():
    async def llm_stage(ctx, deps):
        # un stagiu care folosește LLM → usage înregistrat în acumulatorul turului
        usage.record_chat(_Resp(_Usage(700, 90, _Details(cached_tokens=640))), "gpt-5.4-mini")

    ctx = _ctx()
    await run_pipeline(ctx, PipelineDeps(conn=None), [llm_stage])
    ev = [e for e in ctx.events if e.type == "llm_usage"]
    assert len(ev) == 1
    p = ev[0].properties
    assert p["tokens_in"] == 700 and p["tokens_out"] == 90 and p["cached_tokens"] == 640
    assert p["cost_usd"] > 0 and p["llm_calls"] == 1


async def test_runner_no_llm_usage_when_no_calls():
    async def noop_stage(ctx, deps):
        pass

    ctx = _ctx()
    await run_pipeline(ctx, PipelineDeps(conn=None), [noop_stage])
    assert not any(e.type == "llm_usage" for e in ctx.events)
