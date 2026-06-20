"""NX-78/NX-103 cost obs — captarea usage-ului LLM (tokeni + cached + cost) + emiterea `llm_usage`.

Pricing pur + acumulator + record_* defensiv + integrare runner/adaptor + defalcare pe stagiu/model
+ atașarea pe rândul `messages` + override de tarife. ZERO OpenAI real, ZERO DB real.
"""

from dataclasses import dataclass

from src.agent import pricing, usage
from src.agent.llm import LLMClient
from src.agent.pricing import cost_for, rates_for, savings_for
from src.config import get_settings
from src.models import BusinessConfig, Contact, InboundMessage, TurnContext, TurnUsage
from src.worker.processor import _message_usage_kwargs, _usage_event_props
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


def test_savings_for_matches_discount():
    r = rates_for("gpt-5.4-mini")
    s = savings_for("gpt-5.4-mini", 1000)
    assert abs(s - 1000 * (r.input - r.cached_input) / 1_000_000) < 1e-12
    assert s > 0


def test_savings_for_zero_when_no_caching_discount():
    # embeddings: input == cached_input → zero economie
    assert savings_for("text-embedding-3-small", 1000) == 0.0


def test_pricing_override_from_settings(monkeypatch):
    monkeypatch.setenv("LLM_PRICING_JSON", '{"gpt-5.4-mini": {"input": 9.0}}')
    get_settings.cache_clear()
    pricing._reset_pricing_cache()
    try:
        assert rates_for("gpt-5.4-mini").input == 9.0
        # câmpurile neacoperite rămân la implicit
        assert rates_for("gpt-5.4-mini").output == 2.00
    finally:
        monkeypatch.delenv("LLM_PRICING_JSON", raising=False)
        get_settings.cache_clear()
        pricing._reset_pricing_cache()


def test_pricing_override_invalid_json_falls_back(monkeypatch):
    monkeypatch.setenv("LLM_PRICING_JSON", "not json {{{")
    get_settings.cache_clear()
    pricing._reset_pricing_cache()
    try:
        assert rates_for("gpt-5.4-mini").input == 0.25  # implicit, nu crapă
    finally:
        monkeypatch.delenv("LLM_PRICING_JSON", raising=False)
        get_settings.cache_clear()
        pricing._reset_pricing_cache()


def test_pricing_override_bad_value_type_falls_back(monkeypatch):
    # JSON VALID dar valoare ne-numerică → float() ar arunca pe hot path. Fallback, nu crash
    # (vezi _apply_overrides: parsarea + coerciția numerică sunt sub același try).
    monkeypatch.setenv("LLM_PRICING_JSON", '{"gpt-5.4-mini": {"input": "cheap"}}')
    get_settings.cache_clear()
    pricing._reset_pricing_cache()
    try:
        assert rates_for("gpt-5.4-mini").input == 0.25  # implicit
        # și cost_for (hot path) NU aruncă
        assert cost_for("gpt-5.4-mini", 100, 0, 10) >= 0
    finally:
        monkeypatch.delenv("LLM_PRICING_JSON", raising=False)
        get_settings.cache_clear()
        pricing._reset_pricing_cache()


# --- acumulator + record -----------------------------------------------------


def test_accumulator_add():
    acc = usage.UsageAccumulator()
    acc.add("gpt-5.4-nano", 100, 50, 0)
    acc.add("gpt-5.4-mini", 200, 80, 150)
    assert acc.calls == 2
    assert acc.tokens_in == 300 and acc.tokens_out == 130 and acc.cached_tokens == 150
    assert acc.cost_usd > 0


def test_accumulator_by_model_breakdown():
    acc = usage.UsageAccumulator()
    acc.add("gpt-5.4-nano", 100, 50, 0)
    acc.add("gpt-5.4-nano", 200, 20, 0)
    acc.add("gpt-5.4-mini", 300, 90, 256)
    assert set(acc.by_model) == {"gpt-5.4-nano", "gpt-5.4-mini"}
    assert acc.by_model["gpt-5.4-nano"]["calls"] == 2
    assert acc.by_model["gpt-5.4-nano"]["tokens_in"] == 300
    assert acc.by_model["gpt-5.4-mini"]["cached_tokens"] == 256
    assert acc.by_model["gpt-5.4-mini"]["cost_usd"] > 0


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


# --- integrare runner (emite llm_usage + ctx.usage + by_stage) ---------------


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
    assert p["phase"] == "turn" and p["savings_usd"] > 0


async def test_runner_sets_ctx_usage_and_by_stage():
    async def free_stage(ctx, deps):
        pass  # zero LLM → nu apare în by_stage

    async def llm_stage(ctx, deps):
        usage.record_chat(_Resp(_Usage(500, 40, _Details(cached_tokens=128))), "gpt-5.4-nano")
        ctx.set_reply("gata")

    ctx = _ctx()
    await run_pipeline(ctx, PipelineDeps(conn=None), [free_stage, llm_stage])
    assert ctx.usage is not None
    assert ctx.usage.tokens_in == 500 and ctx.usage.calls == 1
    assert ctx.usage.models == ["gpt-5.4-nano"]
    assert ctx.usage.latency_ms >= 0
    # defalcarea pe stagiu: doar llm_stage (free_stage n-a atins LLM-ul)
    assert "llm_stage" in ctx.usage.by_stage and "free_stage" not in ctx.usage.by_stage
    assert ctx.usage.by_stage["llm_stage"]["tokens_in"] == 500


async def test_runner_no_llm_usage_when_no_calls():
    async def noop_stage(ctx, deps):
        pass

    ctx = _ctx()
    await run_pipeline(ctx, PipelineDeps(conn=None), [noop_stage])
    assert not any(e.type == "llm_usage" for e in ctx.events)
    assert ctx.usage is None


# --- atașarea pe mesaj + props post-tur (processor) --------------------------


def test_message_usage_kwargs_maps_fields():
    tu = TurnUsage(
        tokens_in=500,
        tokens_out=40,
        cost_usd=0.000123,
        calls=2,
        latency_ms=1234.5,
        models=["gpt-5.4-mini", "gpt-5.4-nano"],
    )
    kw = _message_usage_kwargs(tu)
    assert kw["tokens_in"] == 500 and kw["tokens_out"] == 40
    assert kw["model_route"] == "gpt-5.4-mini,gpt-5.4-nano"
    assert kw["latency_ms"] == 1234  # int-rotunjit
    assert kw["cost_usd"] == round(0.000123, 6)


def test_message_usage_kwargs_empty_when_no_llm():
    assert _message_usage_kwargs(None) == {}
    assert _message_usage_kwargs(TurnUsage()) == {}  # calls == 0 → mesaj fără cost


def test_usage_event_props_post_turn_phase():
    acc = usage.UsageAccumulator()
    acc.add("gpt-5.4-nano", 120, 30, 0)
    props = _usage_event_props(acc, phase="post_turn")
    assert props["phase"] == "post_turn"
    assert props["tokens_in"] == 120 and props["llm_calls"] == 1
    assert props["cost_usd"] > 0 and "by_model" in props
