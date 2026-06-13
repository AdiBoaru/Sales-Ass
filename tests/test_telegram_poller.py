"""Teste unit pentru poller-ul Telegram (fake client + fakeredis → CI)."""

import json

import fakeredis.aioredis
import pytest

from src.channels.telegram.poller import _offset_key, _to_event, poll_once
from src.redis_bus import STREAM_INBOUND

BOT_ID = "555"


class FakeTgClient:
    """Întoarce loturi de update-uri prefabricate, câte unul per apel get_updates."""

    def __init__(self, batches: list[list[dict]]):
        self._batches = list(batches)
        self.offsets: list[int] = []

    async def get_updates(self, offset, *, timeout=30, limit=100):
        self.offsets.append(offset)
        return self._batches.pop(0) if self._batches else []


@pytest.fixture
async def redis():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield r
    await r.aclose()


def _text_update(update_id: int, text: str = "salut", chat_id: int = 98765) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id * 10,
            "date": 1700000000,
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": chat_id, "first_name": "Ana"},
            "text": text,
        },
    }


def test_to_event_maps_neutral_envelope():
    ev = _to_event(_text_update(7, "ce preț?"), BOT_ID)
    assert ev is not None
    d = ev.to_dict()
    assert d["channel_kind"] == "telegram"
    assert d["channel_account_id"] == BOT_ID
    assert d["sender_external_id"] == "98765"
    assert d["provider_msg_id"] == "70"
    assert d["body"] == "ce preț?"
    assert d["sender_name"] == "Ana"
    assert d["kind"] == "message"


def test_to_event_ignores_non_text():
    assert _to_event({"update_id": 1, "edited_message": {}}, BOT_ID) is None
    assert _to_event({"update_id": 2, "message": {"sticker": {}}}, BOT_ID) is None


async def test_poll_once_enqueues_and_advances_offset(redis):
    client = FakeTgClient([[_text_update(10), _text_update(11)]])
    n = await poll_once(client, redis, BOT_ID, timeout=0)

    assert n == 2
    assert await redis.xlen(STREAM_INBOUND) == 2
    # offset avansează peste ultimul update_id
    assert await redis.get(_offset_key(BOT_ID)) == "12"
    # primul apel a folosit offset 0 (gol)
    assert client.offsets == [0]

    entries = await redis.xrange(STREAM_INBOUND)
    data = json.loads(entries[0][1]["data"])
    assert data["channel_kind"] == "telegram"


async def test_poll_once_advances_offset_past_ignored(redis):
    # un text + un non-text: 1 enqueued, dar offset trece de AMBELE (altfel reluăm)
    client = FakeTgClient([[_text_update(20), {"update_id": 21, "edited_message": {}}]])
    n = await poll_once(client, redis, BOT_ID, timeout=0)

    assert n == 1
    assert await redis.xlen(STREAM_INBOUND) == 1
    assert await redis.get(_offset_key(BOT_ID)) == "22"


async def test_poll_once_empty_no_offset_change(redis):
    await redis.set(_offset_key(BOT_ID), "100")
    client = FakeTgClient([[]])
    n = await poll_once(client, redis, BOT_ID, timeout=0)

    assert n == 0
    assert await redis.get(_offset_key(BOT_ID)) == "100"  # neschimbat
    assert client.offsets == [100]  # a cerut de la offset-ul salvat
