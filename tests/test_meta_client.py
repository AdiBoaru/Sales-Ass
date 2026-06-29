"""Teste unit pentru MetaClient (httpx MockTransport → zero apeluri reale, CI)."""

import httpx
import pytest

from src.meta_client import MetaClient, MetaSendError


def _client(handler) -> MetaClient:
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    return MetaClient(http, "tok-test", base_url="https://graph.test", version="v21.0")


async def test_send_text_returns_wamid():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("Authorization")
        import json

        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"messages": [{"id": "wamid.OUT123"}]})

    meta = _client(handler)
    wamid = await meta.send_text("PNID-send", "40712345678", "salut")

    assert wamid == "wamid.OUT123"
    assert captured["url"] == "https://graph.test/v21.0/PNID-send/messages"
    assert captured["auth"] == "Bearer tok-test"
    assert captured["body"]["to"] == "40712345678"
    assert captured["body"]["text"]["body"] == "salut"


async def test_send_text_raises_on_http_error():
    def handler(request):
        return httpx.Response(401, json={"error": {"message": "bad token"}})

    meta = _client(handler)
    with pytest.raises(httpx.HTTPStatusError):
        await meta.send_text("PNID", "40712", "x")


async def test_send_text_raises_on_unexpected_payload():
    def handler(request):
        return httpx.Response(200, json={"weird": True})

    meta = _client(handler)
    with pytest.raises(MetaSendError):
        await meta.send_text("PNID", "40712", "x")


async def test_send_template_builds_meta_payload():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"messages": [{"id": "wamid.TMPL1"}]})

    meta = _client(handler)
    wamid = await meta.send_template("PNID-send", "40712345678", "awb_update", "ro", ["123", "FAN"])

    assert wamid == "wamid.TMPL1"
    assert captured["url"] == "https://graph.test/v21.0/PNID-send/messages"
    body = captured["body"]
    assert body["type"] == "template"
    assert body["template"]["name"] == "awb_update"
    assert body["template"]["language"] == {"code": "ro"}
    # parametrii poziționali ({{1}},{{2}}) în componenta body, în ordine
    [comp] = body["template"]["components"]
    assert comp["type"] == "body"
    assert comp["parameters"] == [
        {"type": "text", "text": "123"},
        {"type": "text", "text": "FAN"},
    ]


async def test_send_template_omits_components_without_params():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"messages": [{"id": "wamid.TMPL2"}]})

    meta = _client(handler)
    await meta.send_template("PNID", "40712", "promo_simple", "en", [])
    assert "components" not in captured["body"]["template"]


async def test_send_template_raises_on_unexpected_payload():
    def handler(request):
        return httpx.Response(200, json={"weird": True})

    meta = _client(handler)
    with pytest.raises(MetaSendError):
        await meta.send_template("PNID", "40712", "awb_update", "ro", ["x"])


async def test_mark_typing_sends_read_and_typing():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"success": True})

    meta = _client(handler)
    await meta.mark_typing("PNID-x", "40712345678", "wamid.IN123")

    assert captured["url"] == "https://graph.test/v21.0/PNID-x/messages"
    assert captured["body"]["status"] == "read"
    assert captured["body"]["message_id"] == "wamid.IN123"
    assert captured["body"]["typing_indicator"] == {"type": "text"}


async def test_mark_typing_noop_without_wamid():
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={})

    meta = _client(handler)
    await meta.mark_typing("PNID-x", "40712345678", None)  # fără wamid → no-op
    assert called is False
