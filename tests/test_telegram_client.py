"""Teste unit pentru TelegramClient (httpx MockTransport → zero apeluri reale, CI)."""

import httpx
import pytest

from src.channels.telegram.client import TelegramClient, TelegramError


def _client(handler) -> TelegramClient:
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    return TelegramClient(http, "123:ABC", base_url="https://tg.test")


async def test_send_text_returns_message_id():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 42}})

    client = _client(handler)
    mid = await client.send_text("botid", "98765", "salut")

    assert mid == "42"
    assert captured["url"] == "https://tg.test/bot123:ABC/sendMessage"
    assert captured["body"] == {"chat_id": "98765", "text": "salut"}


async def test_get_updates_returns_result_list():
    def handler(request):
        return httpx.Response(
            200,
            json={"ok": True, "result": [{"update_id": 1}, {"update_id": 2}]},
        )

    client = _client(handler)
    updates = await client.get_updates(0, timeout=0)
    assert [u["update_id"] for u in updates] == [1, 2]


async def test_get_me_returns_bot_info():
    def handler(request):
        return httpx.Response(200, json={"ok": True, "result": {"id": 555, "username": "demo_bot"}})

    client = _client(handler)
    me = await client.get_me()
    assert me["id"] == 555


async def test_ok_false_raises():
    def handler(request):
        return httpx.Response(200, json={"ok": False, "description": "Unauthorized"})

    client = _client(handler)
    with pytest.raises(TelegramError):
        await client.get_me()


async def test_http_error_raises():
    def handler(request):
        return httpx.Response(401, json={"ok": False})

    client = _client(handler)
    with pytest.raises(httpx.HTTPStatusError):
        await client.send_text("b", "1", "x")


async def test_send_products_compact_list_with_buttons():
    """W1: UN sendMessage = text + un buton-link per produs (produsul fără url e sărit)."""
    import json

    calls: list[dict] = []

    def handler(request):
        assert str(request.url).endswith("/sendMessage")  # un singur mesaj, fără sendPhoto
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 7}})

    client = _client(handler)
    products = [
        {"name": "Crema X", "price": 82.99, "url": "https://shop/p/x"},
        {"name": "Ser Y", "price": 120.5, "url": None},  # fără url → fără buton
    ]
    mid = await client.send_products("botid", "555", "Uite ce-ți recomand:", products)

    assert mid == "7"
    assert len(calls) == 1  # un singur mesaj (compact, nu 3)
    body = calls[0]
    assert body["text"] == "Uite ce-ți recomand:"
    keyboard = body["reply_markup"]["inline_keyboard"]
    assert len(keyboard) == 1  # un buton (Ser Y fără url e sărit)
    btn = keyboard[0][0]
    assert btn["url"] == "https://shop/p/x"
    assert "82.99" in btn["text"]  # prețul EXACT în eticheta butonului (ca în text)
