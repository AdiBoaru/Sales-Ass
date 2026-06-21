"""NX-126 — adaptor OpenAI: retry bounded pe tranzitoriu, terminal pe 4xx, timeout pasat la
client, sampling params pe agent/triaj. Client FAKE (zero apeluri reale)."""

from types import SimpleNamespace

import httpx
import openai
import pytest

from src.agent import llm
from src.agent.llm import LLMClient
from src.config import get_settings


def _req():
    return httpx.Request("POST", "https://api.openai.com/v1/chat/completions")


def _rate_limit(retry_after="0"):
    resp = httpx.Response(429, headers={"retry-after": retry_after}, request=_req())
    return openai.RateLimitError("rate limited", response=resp, body=None)


def _bad_request():
    resp = httpx.Response(400, request=_req())
    return openai.BadRequestError("bad request", response=resp, body=None)


class _Msg:
    def __init__(self, content):
        self.content = content
        self.tool_calls = None


class _Resp:
    def __init__(self, content):
        self.choices = [SimpleNamespace(message=_Msg(content))]


class _Completions:
    def __init__(self, behaviors):
        self._behaviors = list(behaviors)
        self.calls: list[dict] = []
        self.last_kwargs: dict | None = None

    async def create(self, **kwargs):
        self.last_kwargs = kwargs
        self.calls.append(kwargs)
        b = self._behaviors.pop(0)
        if isinstance(b, Exception):
            raise b
        return b


def _client(behaviors):
    comp = _Completions(behaviors)
    return SimpleNamespace(chat=SimpleNamespace(completions=comp), _comp=comp)


def _llm_client(behaviors):
    cl = _client(behaviors)
    return LLMClient(cl, model_triage="nano", model_agent="mini"), cl._comp


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    """Sleep no-op (retry rapid) + usage no-op (fără dependență de forma resp)."""

    async def _nosleep(_s):
        return None

    monkeypatch.setattr(llm.asyncio, "sleep", _nosleep)
    monkeypatch.setattr(llm.usage, "record_chat", lambda *a, **k: None)


async def test_retry_on_429_then_success():
    c, comp = _llm_client([_rate_limit(), _Resp('{"route": "simple"}')])
    out = await c.classify_json("sys", "usr")
    assert out == {"route": "simple"}
    assert len(comp.calls) == 2  # 1 eșec tranzitoriu + 1 succes


async def test_retry_on_timeout_then_success():
    c, comp = _llm_client([openai.APITimeoutError(request=_req()), _Resp('{"ok": true}')])
    out = await c.classify_json("sys", "usr")
    assert out == {"ok": True}
    assert len(comp.calls) == 2


async def test_terminal_400_no_retry():
    c, comp = _llm_client([_bad_request(), _Resp("{}")])
    with pytest.raises(openai.BadRequestError):
        await c.classify_json("sys", "usr")
    assert len(comp.calls) == 1  # 4xx terminal → zero retry


async def test_retry_exhausted_raises():
    # llm_retry_max default 2 → 1 inițial + 2 retries = 3 încercări, toate 429 → ridică.
    c, comp = _llm_client([_rate_limit(), _rate_limit(), _rate_limit()])
    with pytest.raises(openai.RateLimitError):
        await c.classify_json("sys", "usr")
    assert len(comp.calls) == 3


async def test_agent_call_includes_sampling_params():
    c, comp = _llm_client([_Resp("raspuns")])
    await c.complete("sys", "usr")
    assert comp.last_kwargs["temperature"] == get_settings().llm_temperature
    assert comp.last_kwargs["max_tokens"] == get_settings().llm_max_tokens_agent


async def test_triage_has_temperature_but_no_max_tokens():
    c, comp = _llm_client([_Resp("{}")])
    await c.classify_json("sys", "usr")
    assert "temperature" in comp.last_kwargs
    assert "max_tokens" not in comp.last_kwargs  # JSON triaj nu primește max_tokens


async def test_sampling_disabled_kill_switch(monkeypatch):
    monkeypatch.setattr(
        llm,
        "get_settings",
        lambda: SimpleNamespace(llm_sampling_enabled=False, llm_retry_max=2),
    )
    c, comp = _llm_client([_Resp("x")])
    await c.complete("sys", "usr")
    assert "temperature" not in comp.last_kwargs and "max_tokens" not in comp.last_kwargs


def test_get_llm_builds_client_with_timeout_and_no_sdk_retry(monkeypatch):
    captured: dict = {}

    class _FakeAsyncOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(llm, "AsyncOpenAI", _FakeAsyncOpenAI)
    monkeypatch.setattr(llm, "_llm", None)
    monkeypatch.setattr(
        llm,
        "get_settings",
        lambda: SimpleNamespace(
            openai_api_key="k",
            model_triage="nano",
            model_agent="mini",
            model_embed="emb",
            model_moderation="mod",
            model_vision="vis",
            llm_timeout_s=30.0,
        ),
    )
    assert llm.get_llm() is not None
    assert captured["timeout"] == 30.0
    assert captured["max_retries"] == 0  # SDK retry off (folosim _with_retry)
