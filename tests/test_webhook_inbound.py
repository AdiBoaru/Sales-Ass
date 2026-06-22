"""Teste pentru ingestia webhook (POST /webhook) + helperele ei.

Fără servicii reale (fakeredis async + payload-uri sintetice semnate) → rulează
în CI. Acoperă: verificarea semnăturii, parserul Meta, și fluxul POST cap-coadă
(semnătură → dedupe → enqueue → ACK).
"""

import hashlib
import hmac
import json

import fakeredis.aioredis
import pytest
from httpx import ASGITransport, AsyncClient

from src.redis_bus import STREAM_INBOUND
from src.webhook.app import app, get_app_secret, redis_dep
from src.webhook.meta import parse_statuses, parse_webhook
from src.webhook.signature import verify_meta_signature

SECRET = "test-app-secret"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _text_payload(wamid: str = "wamid.AAA", body: str = "salut") -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WABA",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "40123",
                                "phone_number_id": "PNID1",
                            },
                            "contacts": [{"profile": {"name": "Ana"}, "wa_id": "40712345678"}],
                            "messages": [
                                {
                                    "from": "40712345678",
                                    "id": wamid,
                                    "timestamp": "1700000000",
                                    "type": "text",
                                    "text": {"body": body},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


def _statuses_payload() -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WABA",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {"phone_number_id": "PNID1"},
                            "statuses": [
                                {"id": "wamid.X", "status": "delivered", "timestamp": "1700"}
                            ],
                        },
                    }
                ],
            }
        ],
    }


# --------------------------------------------------------------------------- #
# signature.py
# --------------------------------------------------------------------------- #


def test_signature_valid():
    body = b'{"a":1}'
    assert verify_meta_signature(SECRET, body, _sign(SECRET, body)) is True


def test_signature_wrong_secret():
    body = b'{"a":1}'
    assert verify_meta_signature(SECRET, body, _sign("other", body)) is False


def test_signature_missing_or_malformed():
    body = b'{"a":1}'
    assert verify_meta_signature(SECRET, body, None) is False
    assert verify_meta_signature(SECRET, body, "md5=deadbeef") is False
    # secret gol → fail-closed
    assert verify_meta_signature("", body, _sign("", body)) is False


# --------------------------------------------------------------------------- #
# meta.py parser
# --------------------------------------------------------------------------- #


def test_parse_text_message():
    events = parse_webhook(_text_payload(body="ce preț are X?"))
    assert len(events) == 1
    ev = events[0]
    assert ev.channel_kind == "whatsapp"
    assert ev.channel_account_id == "PNID1"
    assert ev.sender_external_id == "40712345678"
    assert ev.provider_msg_id == "wamid.AAA"
    assert ev.content_type == "text"
    assert ev.body == "ce preț are X?"
    assert ev.sender_name == "Ana"


def test_parse_clamps_long_body():
    # NX-121: corpul inbound WA > 2000 → trunchiat la margine (paritate cu webul, P6 trunchiere)
    from src.config import INBOUND_BODY_MAX

    events = parse_webhook(_text_payload(body="x" * 5000))
    assert len(events) == 1 and len(events[0].body) == INBOUND_BODY_MAX


def test_parse_statuses_only_is_empty():
    assert parse_webhook(_statuses_payload()) == []


def test_parse_statuses_extracts_delivery():
    events = parse_statuses(_statuses_payload())
    assert len(events) == 1
    st = events[0]
    assert st.channel_kind == "whatsapp"
    assert st.channel_account_id == "PNID1"
    assert st.provider_msg_id == "wamid.X"
    assert st.status == "delivered"
    assert st.to_dict()["kind"] == "status"


def test_parse_statuses_on_message_payload_is_empty():
    assert parse_statuses(_text_payload()) == []


def test_parse_media_with_caption():
    payload = _text_payload()
    msg = payload["entry"][0]["changes"][0]["value"]["messages"][0]
    msg.pop("text")
    msg["type"] = "image"
    msg["image"] = {"id": "media-123", "caption": "asta?"}
    events = parse_webhook(payload)
    assert events[0].content_type == "image"
    assert events[0].media_id == "media-123"
    assert events[0].body == "asta?"


def test_parse_skips_message_without_id():
    payload = _text_payload()
    del payload["entry"][0]["changes"][0]["value"]["messages"][0]["id"]
    assert parse_webhook(payload) == []


def test_parse_empty_payload():
    assert parse_webhook({}) == []


# --------------------------------------------------------------------------- #
# POST /webhook (end-to-end cu fakeredis)
# --------------------------------------------------------------------------- #


@pytest.fixture
async def client_and_redis():
    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    app.dependency_overrides[get_app_secret] = lambda: SECRET
    app.dependency_overrides[redis_dep] = lambda: fake
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac, fake
    app.dependency_overrides.clear()
    await fake.aclose()


async def _post(ac, payload: dict, *, secret: str = SECRET):
    body = json.dumps(payload).encode()
    return await ac.post(
        "/webhook",
        content=body,
        headers={"X-Hub-Signature-256": _sign(secret, body)},
    )


async def test_post_valid_enqueues(client_and_redis):
    ac, fake = client_and_redis
    resp = await _post(ac, _text_payload())
    assert resp.status_code == 200
    assert await fake.xlen(STREAM_INBOUND) == 1
    entries = await fake.xrange(STREAM_INBOUND)
    data = json.loads(entries[0][1]["data"])
    assert data["provider_msg_id"] == "wamid.AAA"
    assert data["body"] == "salut"


async def test_post_invalid_signature_rejected(client_and_redis):
    ac, fake = client_and_redis
    resp = await _post(ac, _text_payload(), secret="wrong")
    assert resp.status_code == 403
    assert await fake.xlen(STREAM_INBOUND) == 0


async def test_post_duplicate_deduped(client_and_redis):
    ac, fake = client_and_redis
    await _post(ac, _text_payload(wamid="wamid.DUP"))
    await _post(ac, _text_payload(wamid="wamid.DUP"))  # retry Meta
    assert await fake.xlen(STREAM_INBOUND) == 1


async def test_post_statuses_enqueued_with_kind(client_and_redis):
    ac, fake = client_and_redis
    resp = await _post(ac, _statuses_payload())
    assert resp.status_code == 200
    assert await fake.xlen(STREAM_INBOUND) == 1
    entries = await fake.xrange(STREAM_INBOUND)
    data = json.loads(entries[0][1]["data"])
    assert data["kind"] == "status"
    assert data["status"] == "delivered"
    assert data["provider_msg_id"] == "wamid.X"


async def test_post_status_not_deduped(client_and_redis):
    """delivered apoi read pe același wamid → 2 evenimente (dedupe NU se aplică)."""
    ac, fake = client_and_redis
    await _post(ac, _statuses_payload())  # delivered
    payload = _statuses_payload()
    payload["entry"][0]["changes"][0]["value"]["statuses"][0]["status"] = "read"
    await _post(ac, payload)
    assert await fake.xlen(STREAM_INBOUND) == 2


async def test_post_redis_down_returns_503(client_and_redis):
    """Redis indisponibil la enqueue → 503 (Meta reîncearcă), nu 500/pierdere tăcută."""
    from redis.exceptions import ConnectionError as RedisConnError

    ac, fake = client_and_redis

    async def boom(*a, **k):
        raise RedisConnError("redis down")

    fake.set = boom  # seen_before → SET NX eșuează
    resp = await _post(ac, _text_payload())
    assert resp.status_code == 503


async def test_post_bad_json_signed_is_400(client_and_redis):
    ac, fake = client_and_redis
    body = b"not json at all"
    resp = await ac.post(
        "/webhook",
        content=body,
        headers={"X-Hub-Signature-256": _sign(SECRET, body)},
    )
    assert resp.status_code == 400
