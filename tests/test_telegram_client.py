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


# --- R2: carusel -------------------------------------------------------------


def _car_products() -> list[dict]:
    return [
        {
            "product_id": "p1",
            "name": "Crema A",
            "price": 49.9,
            "url": "http://x/p1",
            "image": "http://x/p1.png",
        },
        {
            "product_id": "p2",
            "name": "Crema B",
            "price": 79.5,
            "url": "http://x/p2",
            "image": "http://x/p2.png",
        },
        {
            "product_id": "p3",
            "name": "Crema C",
            "price": 99.0,
            "url": "http://x/p3",
            "image": "http://x/p3.png",
        },
    ]


def _capture(message_id: int = 7):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": message_id}})

    return captured, handler


async def test_send_carousel_card_first_index():
    captured, handler = _capture(7)
    mid = await _client(handler).send_carousel_card("botid", "chat1", _car_products(), 0)

    assert mid == "7"
    assert captured["url"].endswith("/sendPhoto")
    body = captured["body"]
    assert body["chat_id"] == "chat1"
    assert body["photo"] == "http://x/p1.png"
    assert "Crema A" in body["caption"] and "49.90" in body["caption"]
    kb = body["reply_markup"]["inline_keyboard"][0]
    texts = [b["text"] for b in kb]
    assert "◀" not in texts  # primul → fără înapoi
    assert any("🛒" in t for t in texts)
    nxt = next(b for b in kb if b["text"] == "▶")
    assert nxt["callback_data"] == "car:nav:1"


async def test_send_carousel_card_last_index_no_next():
    captured, handler = _capture(1)
    await _client(handler).send_carousel_card("botid", "chat1", _car_products(), 2)

    kb = captured["body"]["reply_markup"]["inline_keyboard"][0]
    texts = [b["text"] for b in kb]
    assert "▶" not in texts  # ultimul → fără înainte
    prev = next(b for b in kb if b["text"] == "◀")
    assert prev["callback_data"] == "car:nav:1"


async def test_edit_message_media_targets_index():
    captured, handler = _capture(55)
    mid = await _client(handler).edit_message_media("botid", "chat1", "55", _car_products(), 1)

    assert mid == "55"
    assert captured["url"].endswith("/editMessageMedia")
    body = captured["body"]
    assert body["message_id"] == 55
    assert body["media"]["media"] == "http://x/p2.png"
    assert "Crema B" in body["media"]["caption"]


async def test_answer_callback_query():
    captured, handler = _capture()
    await _client(handler).answer_callback_query("cbid-9")

    assert captured["url"].endswith("/answerCallbackQuery")
    assert captured["body"] == {"callback_query_id": "cbid-9"}


async def test_carousel_fallback_image_when_missing():
    captured, handler = _capture(1)
    prods = [{"product_id": "p", "name": "X", "price": 10.0, "url": "http://x", "image": None}]
    await _client(handler).send_carousel_card("b", "c", prods, 0)

    assert "placehold" in captured["body"]["photo"]


async def test_mark_typing_sends_chat_action():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True, "result": True})

    client = _client(handler)
    await client.mark_typing("botid", "98765", None)  # provider_msg_id ignorat la Telegram

    assert captured["url"] == "https://tg.test/bot123:ABC/sendChatAction"
    assert captured["body"] == {"chat_id": "98765", "action": "typing"}
