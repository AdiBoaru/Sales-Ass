"""NX-357 — cât așteptăm la cererea furnizorului.

Episodul real (2026-09-29, `sole-ro`, conversația `1748f988`, turele 3 și 4): moderarea a primit un
5xx cu `Retry-After: 120`, iar `_with_retry` a dormit 120 s. Logul din producție:
`InternalServerError tranzitoriu (llm_retry_status) — retry 1/2 în 120.02s`. Poarta e fail-open,
deci clientul a așteptat 136 s un verdict care oricum îl lăsa să treacă.

Testele pe moderare rulează pe un server HTTP LOCAL real (socket), fiindcă timeouturile httpx nu
există pe un transport fals: `httpx.MockTransport` nu le aplică, deci un test pe el ar trece și cu
plafonul stricat. Zero apeluri la OpenAI."""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import httpx
import openai
import pytest
from openai import AsyncOpenAI

from src.agent import llm
from src.agent.llm import LLMClient, failure_cause
from src.config import get_settings
from src.observability import turn_latency
from src.worker.stages import gates

_REAL_SLEEP = asyncio.sleep
_OK = json.dumps(
    {
        "id": "modr-1",
        "model": "omni-moderation-latest",
        "results": [{"flagged": False, "categories": {}, "category_scores": {}}],
    }
).encode()
_ERR = b'{"error": {"message": "The server had an error", "type": "server_error"}}'


def _settings(**over):
    return get_settings().model_copy(update=over)


@pytest.fixture
def slept(monkeypatch):
    """Somnul DINTRE încercări se înregistrează, nu se doarme. `llm.asyncio` e modulul global,
    deci serverul de mai jos folosește `_REAL_SLEEP`, capturat la import."""
    calls: list[float] = []

    async def _record(s, *a, **k):
        if s > 0:  # httpx/anyio cedează bucla cu `sleep(0)`; doar somnul real contează
            calls.append(s)
        else:
            await _REAL_SLEEP(0)

    monkeypatch.setattr(llm.asyncio, "sleep", _record)
    return calls


@pytest.fixture
def server():
    """Server HTTP local. Fiecare cerere consumă un pas din `plan`: (întârziere, cod, headere)."""
    plan: list[tuple[float, int, dict[str, str]]] = []
    seen: list[float] = []

    async def handle(reader, writer):
        try:
            await reader.readuntil(b"\r\n\r\n")
        except Exception:  # noqa: BLE001
            writer.close()
            return
        seen.append(time.perf_counter())
        delay, code, headers = plan.pop(0)
        await _REAL_SLEEP(delay)
        body = _OK if code == 200 else _ERR
        extra = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
        head = (
            f"HTTP/1.1 {code} X\r\ncontent-type: application/json\r\n"
            f"content-length: {len(body)}\r\n{extra}\r\n"
        )
        try:
            writer.write(head.encode() + body)
            await writer.drain()
        except Exception:  # noqa: BLE001 — clientul a închis (timeout), exact ce testăm
            pass
        writer.close()

    state = SimpleNamespace(plan=plan, seen=seen, handle=handle, srv=None, url=None)
    return state


async def _start(state):
    state.srv = await asyncio.start_server(state.handle, "127.0.0.1", 0)
    port = state.srv.sockets[0].getsockname()[1]
    state.url = f"http://127.0.0.1:{port}/v1"


def _mod_client(url, *, timeout=30.0):
    return LLMClient(
        AsyncOpenAI(api_key="sk-test", base_url=url, timeout=timeout, max_retries=0),
        model_agent="gpt-6-luna",
    )


# --- episodul real: 5xx cu Retry-After: 120 -------------------------------------------------


async def test_incident_5xx_with_retry_after_120_no_longer_waits(server, slept, monkeypatch):
    monkeypatch.setattr(llm, "get_settings", lambda: _settings(llm_wait_bounded_enabled=True))
    await _start(server)
    server.plan[:] = [(0.05, 500, {"retry-after": "120"}), (0.05, 200, {})]
    async with server.srv:
        t = time.perf_counter()
        with pytest.raises(openai.InternalServerError):
            await _mod_client(server.url).moderate("sub 100 de lei sa vad")
        wall = time.perf_counter() - t
    assert slept == []  # nicio așteptare pe cele 120 s cerute
    assert len(server.seen) == 1  # nicio a doua încercare după un Retry-After peste tavan
    assert wall < 2.0


async def test_incident_path_when_flag_off_sleeps_the_full_retry_after(server, slept, monkeypatch):
    """Stins = comportamentul din producție de pe 2026-09-29, pinuit: 120 s de somn."""
    monkeypatch.setattr(llm, "get_settings", lambda: _settings(llm_wait_bounded_enabled=False))
    await _start(server)
    server.plan[:] = [(0.05, 500, {"retry-after": "120"}), (0.05, 200, {})]
    async with server.srv:
        res = await _mod_client(server.url).moderate("sub 100 de lei sa vad")
    assert res.flagged is False
    assert len(slept) == 1 and 120.0 <= slept[0] <= 120.25


async def test_silent_provider_is_cut_at_the_moderation_budget(server, slept, monkeypatch):
    """Serverul tace. Stins, fiecare încercare ar primi timeoutul constructorului (30 s) × 3."""
    monkeypatch.setattr(
        llm,
        "get_settings",
        lambda: _settings(llm_wait_bounded_enabled=True, moderation_timeout_s=0.4),
    )
    await _start(server)
    server.plan[:] = [(5.0, 200, {})] * 3
    async with server.srv:
        t = time.perf_counter()
        with pytest.raises((TimeoutError, openai.APITimeoutError)) as ei:
            await _mod_client(server.url, timeout=30.0).moderate("orice")
        wall = time.perf_counter() - t
    assert wall < 1.5
    assert failure_cause(ei.value) == "timeout"


async def test_fast_5xx_is_still_retried_inside_the_budget(server, slept, monkeypatch):
    """Un 5xx rapid fără Retry-After se reîncearcă, ca înainte, dacă încape în buget."""
    monkeypatch.setattr(llm, "get_settings", lambda: _settings(llm_wait_bounded_enabled=True))
    await _start(server)
    server.plan[:] = [(0.05, 503, {}), (0.05, 200, {})]
    async with server.srv:
        res = await _mod_client(server.url).moderate("orice")
    assert res.flagged is False
    assert len(server.seen) == 2
    assert len(slept) == 1 and slept[0] < 1.0


async def test_moderation_request_carries_its_own_timeout(monkeypatch):
    monkeypatch.setattr(
        llm,
        "get_settings",
        lambda: _settings(llm_wait_bounded_enabled=True, moderation_timeout_s=3.0),
    )
    seen = {}

    class _Mods:
        async def create(self, **kw):
            seen.update(kw)
            return SimpleNamespace(
                results=[
                    SimpleNamespace(flagged=False, categories=SimpleNamespace(model_dump=dict))
                ]
            )

    await LLMClient(SimpleNamespace(moderations=_Mods()), model_agent="a").moderate("x")
    assert 0 < seen["timeout"] <= 3.0


# --- tavanul Retry-After pe TOATE apelurile ---------------------------------------------------


def _req():
    return httpx.Request("POST", "https://api.openai.com/v1/chat/completions")


def _rate_limit(retry_after):
    resp = httpx.Response(429, headers={"retry-after": retry_after}, request=_req())
    return openai.RateLimitError("rate limited", response=resp, body=None)


class _Resp:
    def __init__(self, content):
        msg = SimpleNamespace(content=content, tool_calls=None)
        self.choices = [SimpleNamespace(message=msg, finish_reason="stop")]


def _chat_client(behaviors):
    calls = []

    class _Completions:
        async def create(self, **kw):
            calls.append(kw)
            b = behaviors.pop(0)
            if isinstance(b, Exception):
                raise b
            return b

    client = SimpleNamespace(chat=SimpleNamespace(completions=_Completions()))
    return LLMClient(client, model_agent="gpt-5.4-mini"), calls


@pytest.fixture
def no_usage(monkeypatch):
    monkeypatch.setattr(llm.usage, "record_chat", lambda *a, **k: None)


@pytest.mark.parametrize(
    ("retry_after", "calls_expected", "sleeps"),
    [("30", 1, 0), ("10", 2, 1), ("2", 2, 1)],
)
async def test_retry_after_ceiling_on_chat(
    monkeypatch, slept, no_usage, retry_after, calls_expected, sleeps
):
    monkeypatch.setattr(
        llm,
        "get_settings",
        lambda: _settings(llm_wait_bounded_enabled=True, llm_retry_after_max_s=10.0),
    )
    c, calls = _chat_client([_rate_limit(retry_after), _Resp('{"ok": true}')])
    acc, token = turn_latency.push()
    try:
        if calls_expected == 1:
            with pytest.raises(openai.RateLimitError):
                await c.classify_json("sys", "usr")
            assert acc.degradations.get("llm_retry_after_over_cap") == 1
        else:
            assert await c.classify_json("sys", "usr") == {"ok": True}
    finally:
        turn_latency.pop(token)
    assert len(calls) == calls_expected
    assert len(slept) == sleeps


async def test_retry_after_ceiling_off_is_byte_identical(monkeypatch, slept, no_usage):
    monkeypatch.setattr(llm, "get_settings", lambda: _settings(llm_wait_bounded_enabled=False))
    c, calls = _chat_client([_rate_limit("30"), _Resp('{"ok": true}')])
    assert await c.classify_json("sys", "usr") == {"ok": True}
    assert len(calls) == 2 and 30.0 <= slept[0] <= 30.25


# --- Vision și embedding primesc plafonul total NX-311 ------------------------------------------


@pytest.mark.parametrize("enabled", [True, False])
async def test_extraction_calls_get_the_total_cap(monkeypatch, enabled):
    monkeypatch.setattr(
        llm,
        "get_settings",
        lambda: _settings(llm_wait_bounded_enabled=enabled, llm_call_total_cap_s=90.0),
    )
    captured: list[dict] = []

    async def _fake_retry(factory, **kw):
        captured.append(kw)
        raise RuntimeError("stop")

    monkeypatch.setattr(llm, "_with_retry", _fake_retry)
    c = LLMClient(SimpleNamespace(), model_agent="a")
    with pytest.raises(RuntimeError):
        await c.describe_image("aGk=", "image/png")
    with pytest.raises(RuntimeError):
        await c.embed(["x"])
    expected = 90.0 if enabled else None
    assert [kw.get("total_cap_s") for kw in captured] == [expected, expected]


# --- poarta: episodul devine vizibil în analytics ----------------------------------------------


def _ctx(body):
    from tests.test_gates import _ctx as gates_ctx

    return gates_ctx(body=body)


class _FailingLLM:
    def __init__(self, exc):
        self._exc = exc

    async def moderate(self, text):
        raise self._exc


@pytest.mark.parametrize(
    ("exc", "cause"),
    [
        (TimeoutError(), "timeout"),
        (openai.APITimeoutError(request=_req()), "timeout"),
        (openai.APIConnectionError(request=_req()), "connection"),
        (
            openai.InternalServerError(
                "x", response=httpx.Response(500, request=_req()), body=None
            ),
            "status",
        ),
        (RuntimeError("boom"), "error"),
    ],
)
async def test_gate_emits_moderation_unavailable(monkeypatch, exc, cause):
    from src.worker.runner import PipelineDeps

    monkeypatch.setattr(gates, "get_settings", lambda: _settings(llm_wait_bounded_enabled=True))
    ctx = _ctx("sub 100 de lei sa vad")
    await gates.gates_stage(ctx, PipelineDeps(conn=None, redis=None, llm=_FailingLLM(exc)))
    assert ctx.reply is None and ctx.halt is False  # tot fail-open
    events = [e for e in ctx.events if e.type == "moderation_unavailable"]
    assert [e.properties["cause"] for e in events] == [cause]
    assert cause in llm.FAILURE_CAUSES


async def test_gate_event_off_when_flag_off(monkeypatch):
    from src.worker.runner import PipelineDeps

    monkeypatch.setattr(gates, "get_settings", lambda: _settings(llm_wait_bounded_enabled=False))
    ctx = _ctx("orice")
    await gates.gates_stage(
        ctx, PipelineDeps(conn=None, redis=None, llm=_FailingLLM(TimeoutError()))
    )
    assert not [e for e in ctx.events if e.type == "moderation_unavailable"]
