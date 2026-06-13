"""Teste integration pentru dispatcher (DB real, Meta fake). Rollback per test.

dispatch_row e unitatea testabilă: primește un conn tenant-scoped + un client Meta
fake (duck-typed) + un rând revendicat. Bucla dispatch_due (admin + iterare tenant)
e orchestrare — acoperită indirect; aici testăm comportamentul pe rând.
"""

from contextlib import asynccontextmanager
from uuid import uuid4

import httpx
import pytest

from src.channels.base import ChannelSenderRegistry
from src.db.connection import close_pool, get_pool
from src.db.queries.businesses import load_business
from src.db.queries.outbox import business_ids_with_due_outbox, claim_due
from src.worker.dispatcher import dispatch_row
from src.worker.processor import handle_turn

pytestmark = pytest.mark.integration

DEMO_BIZ = "6098812a-50fc-44bd-a1ba-bc77e6399158"


@pytest.fixture
async def pool():
    p = await get_pool()
    yield p
    await close_pool()


@asynccontextmanager
async def tenant_tx(pool, business_id=DEMO_BIZ):
    async with pool.acquire() as conn:
        tr = conn.transaction()
        await tr.start()
        try:
            channel_id = await conn.fetchval(
                "insert into channels (business_id, kind, provider_account_id) "
                "values ($1, 'whatsapp', $2) returning id::text",
                business_id,
                f"PN-{uuid4().hex[:10]}",
            )
            await conn.execute("set local role bot_runtime")
            await conn.execute("select set_config('app.business_id', $1, true)", business_id)
            yield conn, channel_id
        finally:
            await tr.rollback()


class FakeMeta:
    """Client Meta fals — înregistrează apelurile, întoarce un wamid sau ridică."""

    def __init__(self, *, fail: Exception | None = None):
        self.calls: list[tuple] = []
        self._fail = fail

    async def send_text(self, account_id, to, text) -> str:
        self.calls.append((account_id, to, text))
        if self._fail:
            raise self._fail
        return f"wamid.{uuid4().hex[:8]}"


def _event(body="salut"):
    return {
        "channel_kind": "whatsapp",
        "channel_account_id": "PNID-demo",
        "sender_external_id": f"+40{uuid4().hex[:9]}",
        "provider_msg_id": f"wamid.{uuid4().hex[:10]}",
        "content_type": "text",
        "body": body,
        "media_id": None,
        "sender_name": "Ana",
    }


def _registry(sender, kind="whatsapp") -> ChannelSenderRegistry:
    r = ChannelSenderRegistry()
    r.register(kind, sender)
    return r


async def _enqueue_via_turn(conn, channel_id):
    """Produce un rând de outbox prin fluxul real (handle_turn echo)."""
    biz = await load_business(conn, DEMO_BIZ)
    result = await handle_turn(conn, biz, channel_id, _event())
    return result


# --------------------------------------------------------------------------- #
# claim_due — întoarce channel_kind + channel_account_id (expeditor) din join
# --------------------------------------------------------------------------- #


async def test_claim_returns_sender_channel(pool):
    async with tenant_tx(pool) as (conn, channel_id):
        pnid = await conn.fetchval(
            "select provider_account_id from channels where id = $1", channel_id
        )
        await _enqueue_via_turn(conn, channel_id)
        rows = await claim_due(conn, DEMO_BIZ)
        assert len(rows) == 1
        assert rows[0]["channel_kind"] == "whatsapp"
        assert rows[0]["channel_account_id"] == pnid
        assert rows[0]["payload"]["type"] == "text"


async def test_business_ids_with_due_outbox(pool):
    async with tenant_tx(pool) as (conn, channel_id):
        await _enqueue_via_turn(conn, channel_id)
        # sub RLS, query-ul vede doar tenantul curent — validează SQL-ul
        ids = await business_ids_with_due_outbox(conn)
        assert DEMO_BIZ in ids


# --------------------------------------------------------------------------- #
# dispatch_row — succes & eșec
# --------------------------------------------------------------------------- #


async def test_dispatch_row_success_marks_sent_and_links_wamid(pool):
    async with tenant_tx(pool) as (conn, channel_id):
        turn = await _enqueue_via_turn(conn, channel_id)
        [row] = await claim_due(conn, DEMO_BIZ)

        meta = FakeMeta()
        status = await dispatch_row(conn, DEMO_BIZ, _registry(meta), row)

        assert status == "sent"
        assert len(meta.calls) == 1  # un singur apel pe canal
        assert meta.calls[0][0] == row["channel_account_id"]  # account expeditor corect

        ob = await conn.fetchrow(
            "select status, sent_message_id::text from outbox where id = $1", row["id"]
        )
        assert ob["status"] == "sent"
        # mesajul outbound a primit wamid + status sent
        msg = await conn.fetchrow(
            "select provider_msg_id, status from messages "
            "where conversation_id = $1 and direction = 'outbound'",
            turn.conversation_id,
        )
        assert msg["provider_msg_id"] is not None
        assert msg["status"] == "sent"


async def test_dispatch_row_failure_marks_failed_with_backoff(pool):
    async with tenant_tx(pool) as (conn, channel_id):
        await _enqueue_via_turn(conn, channel_id)
        [row] = await claim_due(conn, DEMO_BIZ)

        meta = FakeMeta(fail=httpx.ConnectError("boom"))
        status = await dispatch_row(conn, DEMO_BIZ, _registry(meta), row)

        assert status == "failed"
        ob = await conn.fetchrow(
            "select status, last_error, next_attempt_at > now() as scheduled "
            "from outbox where id = $1",
            row["id"],
        )
        assert ob["status"] == "failed"
        assert "boom" in ob["last_error"]
        assert ob["scheduled"] is True  # programat pentru retry


async def test_dispatch_row_unknown_channel_is_dead(pool):
    """channel_kind fără sender înregistrat → 'dead' cu log, fără crash."""
    async with tenant_tx(pool) as (conn, channel_id):
        await _enqueue_via_turn(conn, channel_id)
        [row] = await claim_due(conn, DEMO_BIZ)
        empty_registry = ChannelSenderRegistry()  # niciun sender
        status = await dispatch_row(conn, DEMO_BIZ, empty_registry, row)
        assert status == "dead"


async def test_claim_visibility_timeout_hides_then_reclaims(pool):
    """Reaper implicit: un rând 'dispatching' rămas (dispatcher mort) redevine
    scadent după visibility timeout și e re-revendicat. now() e constant în
    tranzacție, deci simulăm expirarea împingând next_attempt_at în trecut."""
    async with tenant_tx(pool) as (conn, channel_id):
        await _enqueue_via_turn(conn, channel_id)
        first = await claim_due(conn, DEMO_BIZ, visibility_timeout_s=120)
        assert len(first) == 1
        oid = first[0]["id"]

        # imediat după claim: 'dispatching' cu next_attempt_at în viitor → ascuns
        assert await claim_due(conn, DEMO_BIZ) == []

        # dispatcher „mort": visibility timeout expirat (next_attempt_at în trecut)
        await conn.execute(
            "update outbox set next_attempt_at = now() - interval '1 second' where id = $1",
            oid,
        )
        reclaimed = await claim_due(conn, DEMO_BIZ)
        assert len(reclaimed) == 1
        assert reclaimed[0]["id"] == oid  # același rând, re-revendicat
