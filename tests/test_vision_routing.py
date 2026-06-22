"""NX-76 — media routing Vision (poză → descriere → text de căutare) în Gates.

ZERO apeluri reale: `LLMClient.describe_image` cu AsyncOpenAI fake, `MetaClient.fetch_media` cu
httpx.MockTransport, `gates_stage`/`_route_image` cu fetcher+llm fake. Acoperă: adaptor Vision,
download 2-hop, îmbogățirea body-ului, sentinel non-produs, păstrarea caption-ului la fail-soft,
cost guard, skip clarify pe poză, text neatins, P12.
"""

import logging

import httpx
import pytest

import src.config as config
from src.agent.llm import LLMClient, ModerationResult
from src.meta_client import MetaClient
from src.models import BusinessConfig, Contact, InboundMessage, TurnContext
from src.worker.runner import PipelineDeps
from src.worker.stages import gates
from src.worker.stages.gates import IMG_FALLBACK_BODY


@pytest.fixture(autouse=True)
def _fresh_settings():
    """Fiecare test pornește cu settings proaspete (unele setează env Vision)."""
    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()


# --- adaptor describe_image (AsyncOpenAI fake) -------------------------------


class _FakeCompletions:
    def __init__(self, content, captured):
        self._content = content
        self._captured = captured

    async def create(self, **kwargs):
        self._captured.update(kwargs)
        msg = type("M", (), {"content": self._content})()
        return type("R", (), {"choices": [type("C", (), {"message": msg})()]})()


class _FakeOpenAI:
    def __init__(self, content, captured):
        self.chat = type("Chat", (), {"completions": _FakeCompletions(content, captured)})()


def _llm(content="ser hidratant Cerave ambalaj alb"):
    captured: dict = {}
    client = LLMClient(
        _FakeOpenAI(content, captured),
        model_triage="nano",
        model_agent="mini",
        model_vision="gpt-5.4-mini",
    )
    return client, captured


async def test_describe_image_returns_text_low_detail_bounded():
    client, captured = _llm("ser hidratant alb/albastru")
    out = await client.describe_image("Ym9keQ==", "image/jpeg")
    assert out == "ser hidratant alb/albastru"
    assert captured["model"] == "gpt-5.4-mini"
    assert captured["max_tokens"] == 120  # cost bornat
    # payload conține image_url cu detail:"low" + data URL cu mime-ul
    user_msg = captured["messages"][1]["content"]
    img = next(p for p in user_msg if p["type"] == "image_url")["image_url"]
    assert img["detail"] == "low"
    assert img["url"].startswith("data:image/jpeg;base64,")


# --- MetaClient.fetch_media (httpx MockTransport, 2 hop-uri) -----------------


async def test_fetch_media_two_hops_returns_bytes_and_mime():
    seen = {"auth": []}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"].append(request.headers.get("Authorization"))
        if request.url.path.endswith("/MID123"):
            return httpx.Response(
                200, json={"url": "https://look.test/blob", "mime_type": "image/png"}
            )
        return httpx.Response(200, content=b"\x89PNG\r\n")

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    meta = MetaClient(http, "tok-x", base_url="https://graph.test", version="v21.0")
    blob, mime = await meta.fetch_media("PNID", "MID123")

    assert blob == b"\x89PNG\r\n"
    assert mime == "image/png"
    assert seen["auth"] == ["Bearer tok-x", "Bearer tok-x"]  # Bearer pe AMBELE hop-uri


async def test_fetch_media_raises_on_http_error():
    def handler(request):
        return httpx.Response(404, json={"error": "gone"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    meta = MetaClient(http, "tok", base_url="https://graph.test")
    with pytest.raises(httpx.HTTPStatusError):
        await meta.fetch_media("PNID", "MID")


# --- gate _route_image -------------------------------------------------------


class _FakeFetcher:
    def __init__(self, result=(b"\xff\xd8\xff", "image/jpeg"), raise_exc=None):
        self._result = result
        self._raise = raise_exc
        self.calls: list = []

    async def fetch_media(self, account_id, media_id, *, max_bytes=None):
        self.calls.append((account_id, media_id, max_bytes))
        if self._raise is not None:
            raise self._raise
        return self._result


class _Registry:
    def __init__(self, fetcher):
        self._f = fetcher

    def get(self, kind):
        return self._f


class _VisionLLM:
    def __init__(self, desc="ser hidratant Cerave alb", raise_exc=None):
        self._desc = desc
        self._raise = raise_exc
        self.called = False

    async def describe_image(self, b64, mime):
        self.called = True
        if self._raise is not None:
            raise self._raise
        return self._desc

    async def moderate(self, text):  # gates_stage poate apela moderate pe un caption
        return ModerationResult(flagged=False, categories=[])


def _img_ctx(*, body=None, media_ref="m1", content_type="image") -> TurnContext:
    return TurnContext(
        turn_id="t1",
        business=BusinessConfig(id="biz-1", slug="s", name="n"),
        contact=Contact(id="c1", business_id="biz-1"),
        message=InboundMessage(
            provider_msg_id="m1",
            content_type=content_type,
            body=body,
            media_ref=media_ref,
            channel_kind="whatsapp",
            channel_account_id="PNID",
        ),
        conversation_id="conv-1",
    )


def _deps(llm, fetcher):
    return PipelineDeps(conn=None, redis=None, llm=llm, media=_Registry(fetcher))


async def test_route_image_enriches_body_no_caption():
    ctx = _img_ctx(body=None)
    fetcher = _FakeFetcher()
    await gates._route_image(ctx, _deps(_VisionLLM("ser Cerave alb"), fetcher))

    assert ctx.message.body == "[poză client] ser Cerave alb"
    assert ctx.reply is None and ctx.halt is False
    assert fetcher.calls == [("PNID", "m1", 5_000_000)]  # cap propagat la fetcher (pre-download)
    assert any(
        e.type == "image_routed"
        and e.properties == {"chars": len("ser Cerave alb"), "turn_id": "t1"}
        for e in ctx.events
    )


async def test_route_image_appends_caption():
    ctx = _img_ctx(body="  mai aveți? ")
    await gates._route_image(ctx, _deps(_VisionLLM("ser Cerave"), _FakeFetcher()))
    assert ctx.message.body == "[poză client] ser Cerave — text client: mai aveți?"


async def test_route_image_through_gates_stage_no_early_exit():
    ctx = _img_ctx(body=None)
    await gates.gates_stage(ctx, _deps(_VisionLLM("cremă hidratantă"), _FakeFetcher()))
    # gates NU oprește pipeline-ul: triajul ar vedea body-ul derivat și ar ruta SALES.
    assert ctx.message.body.startswith("[poză client] ")
    assert ctx.reply is None and ctx.halt is False


async def test_text_message_skips_vision():
    ctx = _img_ctx(body="caut o cremă", media_ref=None, content_type="text")
    fetcher = _FakeFetcher()
    await gates.gates_stage(ctx, _deps(_VisionLLM(), fetcher))
    assert ctx.message.body == "caut o cremă"  # neatins
    assert fetcher.calls == []  # zero download
    assert not any(e.type.startswith("image_") for e in ctx.events)


# --- fail-soft (P6) ----------------------------------------------------------


async def test_failsoft_no_media_ref():
    ctx = _img_ctx(media_ref=None)
    await gates._route_image(ctx, _deps(_VisionLLM(), _FakeFetcher()))
    assert ctx.message.body == IMG_FALLBACK_BODY
    assert _failed_reason(ctx) == "no_media"


async def test_failsoft_no_fetcher_for_channel():
    ctx = _img_ctx()
    await gates._route_image(ctx, PipelineDeps(conn=None, llm=_VisionLLM(), media=_Registry(None)))
    assert ctx.message.body == IMG_FALLBACK_BODY
    assert _failed_reason(ctx) == "no_downloader"


async def test_failsoft_no_media_registry():
    ctx = _img_ctx()
    await gates._route_image(ctx, PipelineDeps(conn=None, llm=_VisionLLM(), media=None))
    assert ctx.message.body == IMG_FALLBACK_BODY
    assert _failed_reason(ctx) == "disabled"


async def test_failsoft_no_llm():
    ctx = _img_ctx()
    deps = PipelineDeps(conn=None, llm=None, media=_Registry(_FakeFetcher()))
    await gates._route_image(ctx, deps)
    assert ctx.message.body == IMG_FALLBACK_BODY
    assert _failed_reason(ctx) == "disabled"


async def test_failsoft_download_raises():
    ctx = _img_ctx()
    fetcher = _FakeFetcher(raise_exc=httpx.ConnectError("boom"))
    await gates._route_image(ctx, _deps(_VisionLLM(), fetcher))
    assert ctx.message.body == IMG_FALLBACK_BODY
    assert _failed_reason(ctx) == "vision_error"


async def test_failsoft_describe_raises():
    ctx = _img_ctx()
    llm = _VisionLLM(raise_exc=RuntimeError("openai down"))
    await gates._route_image(ctx, _deps(llm, _FakeFetcher()))
    assert ctx.message.body == IMG_FALLBACK_BODY
    assert _failed_reason(ctx) == "vision_error"


async def test_failsoft_empty_description():
    ctx = _img_ctx()
    await gates._route_image(ctx, _deps(_VisionLLM(desc="  "), _FakeFetcher()))
    assert ctx.message.body == IMG_FALLBACK_BODY
    assert _failed_reason(ctx) == "empty_desc"


async def test_failsoft_too_large(monkeypatch):
    monkeypatch.setenv("VISION_MAX_BYTES", "2")
    config.get_settings.cache_clear()
    ctx = _img_ctx()
    fetcher = _FakeFetcher(result=(b"abcdef", "image/jpeg"))  # 6 bytes > 2
    await gates._route_image(ctx, _deps(_VisionLLM(), fetcher))
    assert ctx.message.body == IMG_FALLBACK_BODY
    assert _failed_reason(ctx) == "too_large"


async def test_failsoft_vision_disabled(monkeypatch):
    monkeypatch.setenv("VISION_ENABLED", "false")
    config.get_settings.cache_clear()
    ctx = _img_ctx()
    llm = _VisionLLM()
    await gates._route_image(ctx, _deps(llm, _FakeFetcher()))
    assert ctx.message.body == IMG_FALLBACK_BODY
    assert llm.called is False  # nu cheltuim Vision când e dezactivat
    assert _failed_reason(ctx) == "disabled"


# --- P12: fără bytes/base64/url în loguri sau evenimente ---------------------


async def test_no_pii_in_logs_or_events(caplog):
    ctx = _img_ctx(body="poza mea")
    raw = b"\xff\xd8\xffSECRETBYTES"
    fetcher = _FakeFetcher(result=(raw, "image/jpeg"))
    with caplog.at_level(logging.DEBUG):
        await gates._route_image(ctx, _deps(_VisionLLM("ser X"), fetcher))
    blob = "\n".join(r.getMessage() for r in caplog.records)
    assert "SECRETBYTES" not in blob
    import base64

    assert base64.b64encode(raw).decode() not in blob
    # evenimentul image_routed poartă DOAR lungimea descrierii (+ turn_id de corelare, NX-122)
    ev = next(e for e in ctx.events if e.type == "image_routed")
    assert set(ev.properties) == {"chars", "turn_id"}


async def test_not_a_product_sentinel_falls_soft():
    # promptul cere modelului „nu pare un produs" la selfie/screenshot → NU căutăm pe text mort
    ctx = _img_ctx(body=None)
    await gates._route_image(ctx, _deps(_VisionLLM(desc="Nu pare un produs."), _FakeFetcher()))
    assert ctx.message.body == IMG_FALLBACK_BODY
    assert _failed_reason(ctx) == "not_a_product"
    assert not any(e.type == "image_routed" for e in ctx.events)


async def test_failsoft_preserves_caption_for_search():
    # poză cu intenție de cumpărare în caption + Vision pică → NU aruncăm caption-ul (lead cald)
    ctx = _img_ctx(body="mai aveți crema Cerave asta?")
    llm = _VisionLLM(raise_exc=RuntimeError("openai down"))
    await gates._route_image(ctx, _deps(llm, _FakeFetcher()))
    assert ctx.message.body == "mai aveți crema Cerave asta?"  # caption păstrat ca text de căutare
    assert _failed_reason(ctx) == "vision_error"


async def test_clarify_skips_image_turn():
    # slot în așteptare + client răspunde cu POZĂ → slotul NU se umple cu textul derivat
    from src.worker.stages.clarify import clarify_resume_stage

    ctx = _img_ctx(body="[poză client] ser hidratant Cerave alb")
    ctx.state.pending_question = {"field": "budget", "resume_route": "sales"}
    await clarify_resume_stage(ctx, PipelineDeps(conn=None))
    assert "budget" not in ctx.state.constraints  # slotul rămâne în așteptare
    assert ctx.route is None  # nu rutăm pe slot otrăvit


# --- cost guard (G2c) pe apelul Vision ---------------------------------------


async def test_vision_cost_charged_on_success(monkeypatch):
    charged = []

    async def fake_cost_add(redis, business_id, amount):
        charged.append((business_id, amount))

    monkeypatch.setattr(gates, "cost_add", fake_cost_add)
    ctx = _img_ctx(body=None)
    deps = PipelineDeps(
        conn=None, redis=object(), llm=_VisionLLM("ser X"), media=_Registry(_FakeFetcher())
    )
    await gates._route_image(ctx, deps)
    assert charged == [("biz-1", 0.003)]  # cost_vision_usd implicit


async def test_vision_cost_not_charged_on_failsoft(monkeypatch):
    charged = []

    async def fake_cost_add(redis, business_id, amount):
        charged.append(amount)

    monkeypatch.setattr(gates, "cost_add", fake_cost_add)
    ctx = _img_ctx(media_ref=None)  # no_media → niciun apel Vision
    deps = PipelineDeps(
        conn=None, redis=object(), llm=_VisionLLM(), media=_Registry(_FakeFetcher())
    )
    await gates._route_image(ctx, deps)
    assert charged == []  # nu se contează cost când Vision NU s-a apelat


async def test_vision_cost_add_failure_does_not_break_turn(monkeypatch):
    async def boom(redis, business_id, amount):
        raise RuntimeError("redis down")

    monkeypatch.setattr(gates, "cost_add", boom)
    ctx = _img_ctx(body=None)
    deps = PipelineDeps(
        conn=None, redis=object(), llm=_VisionLLM("ser X"), media=_Registry(_FakeFetcher())
    )
    await gates._route_image(ctx, deps)  # nu propagă excepția
    assert ctx.message.body == "[poză client] ser X"  # body tot îmbogățit (best-effort)


def _failed_reason(ctx) -> str:
    ev = next(e for e in ctx.events if e.type == "image_route_failed")
    return ev.properties["reason"]
