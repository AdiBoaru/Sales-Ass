"""NX/R2 — handler-ul de navigare a caruselului (parse + bounds + payload).

Unit, fără DB: monkeypatch-uim query-urile (contact/conversație/outbox/analytics)
ca să testăm LOGICA deterministă (parsare callback, limite, forma payload-ului de
edit, idempotency). ZERO apeluri LLM/DB/Telegram.
"""

from src.models import BusinessConfig
from src.worker import callback as cb
from src.worker.callback import parse_nav


def test_parse_nav():
    assert parse_nav("car:nav:0") == 0
    assert parse_nav("car:nav:5") == 5
    assert parse_nav("car:nav:") is None
    assert parse_nav("car:cart:1") is None
    assert parse_nav("foo") is None
    assert parse_nav(None) is None


def _biz() -> BusinessConfig:
    return BusinessConfig(
        id="biz-1",
        slug="s",
        name="n",
        vertical="ecommerce",
        default_locale="ro",
        supported_locales=["ro"],
        timezone="Europe/Bucharest",
        settings={},
        daily_cost_cap_usd=5.0,
    )


def _products(n: int = 3) -> list[dict]:
    return [
        {"product_id": f"p{i}", "name": f"Prod {i}", "price": 10.0 + i, "url": f"http://x/{i}"}
        for i in range(n)
    ]


def _patch(monkeypatch, state: dict) -> dict:
    """Monkeypatch query-urile din handler; întoarce un dict ce captează apelul de outbox."""
    calls: dict = {}

    async def fake_contact(*a, **k):
        return type("C", (), {"id": "contact-1"})()

    async def fake_conv(*a, **k):
        return {"id": "conv-1", "state": state, "state_version": 0}

    async def fake_enqueue(conn, bid, conv_id, key, payload, **k):
        calls["key"] = key
        calls["payload"] = payload
        calls["conv_id"] = conv_id
        return "outbox-1"

    async def fake_events(*a, **k):
        calls["events"] = True
        return 1

    monkeypatch.setattr(cb, "get_or_create_contact", fake_contact)
    monkeypatch.setattr(cb, "get_or_create_conversation", fake_conv)
    monkeypatch.setattr(cb, "enqueue_outbox", fake_enqueue)
    monkeypatch.setattr(cb, "insert_events", fake_events)
    return calls


def _event(data: str, cb_id: str = "cbid-1") -> dict:
    return {
        "data": data,
        "sender_external_id": "chat-9",
        "card_message_id": "55",
        "provider_msg_id": cb_id,
        "channel_kind": "telegram",
    }


async def test_nav_enqueues_edit_media(monkeypatch):
    calls = _patch(monkeypatch, {"displayed_products": _products(3)})
    out = await cb.handle_callback(None, _biz(), "chan-1", _event("car:nav:1"))

    assert out == "outbox-1"
    assert calls["payload"]["type"] == "edit_media"
    assert calls["payload"]["index"] == 1
    assert calls["payload"]["card_message_id"] == "55"
    assert len(calls["payload"]["products"]) == 3
    assert calls["key"] == "cbid-1" or calls["key"].endswith("cbid-1")  # idempotency pe callback.id


async def test_idempotency_key_is_callback_id(monkeypatch):
    calls = _patch(monkeypatch, {"displayed_products": _products(3)})
    await cb.handle_callback(None, _biz(), "chan-1", _event("car:nav:2", cb_id="press-42"))
    assert calls["key"] == "cb:press-42"


async def test_out_of_bounds_is_noop(monkeypatch):
    calls = _patch(monkeypatch, {"displayed_products": _products(3)})
    out = await cb.handle_callback(None, _biz(), "chan-1", _event("car:nav:9"))
    assert out is None
    assert "payload" not in calls  # niciun outbox enqueued


async def test_empty_state_is_noop(monkeypatch):
    calls = _patch(monkeypatch, {})  # fără displayed_products (card expirat)
    out = await cb.handle_callback(None, _biz(), "chan-1", _event("car:nav:0"))
    assert out is None
    assert "payload" not in calls


async def test_unknown_callback_is_noop(monkeypatch):
    calls = _patch(monkeypatch, {"displayed_products": _products(3)})
    out = await cb.handle_callback(None, _biz(), "chan-1", _event("car:cart:1"))
    assert out is None
    assert "payload" not in calls
