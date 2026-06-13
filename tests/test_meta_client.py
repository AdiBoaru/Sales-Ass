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
