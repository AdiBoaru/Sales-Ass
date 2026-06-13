"""Teste integration pentru statusurile de livrare (record_status_event).

Rollback per test → demo DB curat; rol bot_runtime (RLS de producție).
"""

from contextlib import asynccontextmanager
from uuid import uuid4

import pytest

from src.db.connection import close_pool, get_pool
from src.db.queries.message_status import record_status_event

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


async def _outbound_message(conn, channel_id, provider_msg_id):
    """Creează un contact+conversație+mesaj outbound 'sent' cu un wamid dat."""
    contact_id = await conn.fetchval(
        "insert into contacts (business_id) values ($1) returning id::text", DEMO_BIZ
    )
    conv_id = await conn.fetchval(
        "insert into conversations (business_id, contact_id, channel_id) "
        "values ($1, $2, $3) returning id::text",
        DEMO_BIZ,
        contact_id,
        channel_id,
    )
    await conn.execute(
        "insert into messages (business_id, conversation_id, contact_id, direction, author, "
        "provider_msg_id, status) values ($1, $2, $3, 'outbound', 'bot', $4, 'sent')",
        DEMO_BIZ,
        conv_id,
        contact_id,
        provider_msg_id,
    )
    return conv_id


async def _status(conn, provider_msg_id):
    return await conn.fetchval(
        "select status from messages where business_id = $1 and provider_msg_id = $2",
        DEMO_BIZ,
        provider_msg_id,
    )


async def test_status_advances_sent_to_delivered_to_read(pool):
    async with tenant_tx(pool) as (conn, channel_id):
        wamid = f"wamid.{uuid4().hex[:10]}"
        await _outbound_message(conn, channel_id, wamid)

        assert await record_status_event(conn, DEMO_BIZ, wamid, "delivered") is True
        assert await _status(conn, wamid) == "delivered"

        assert await record_status_event(conn, DEMO_BIZ, wamid, "read") is True
        assert await _status(conn, wamid) == "read"

        # tot logul e păstrat (append-only)
        n = await conn.fetchval(
            "select count(*) from message_status_events where provider_msg_id = $1", wamid
        )
        assert n == 2


async def test_status_does_not_downgrade(pool):
    async with tenant_tx(pool) as (conn, channel_id):
        wamid = f"wamid.{uuid4().hex[:10]}"
        await _outbound_message(conn, channel_id, wamid)

        await record_status_event(conn, DEMO_BIZ, wamid, "read")
        assert await _status(conn, wamid) == "read"

        # 'delivered' sosit out-of-order după 'read' → NU retrogradează
        updated = await record_status_event(conn, DEMO_BIZ, wamid, "delivered")
        assert updated is False
        assert await _status(conn, wamid) == "read"
        # dar evenimentul tot e logat
        n = await conn.fetchval(
            "select count(*) from message_status_events where provider_msg_id = $1", wamid
        )
        assert n == 2


async def test_failed_always_wins(pool):
    async with tenant_tx(pool) as (conn, channel_id):
        wamid = f"wamid.{uuid4().hex[:10]}"
        await _outbound_message(conn, channel_id, wamid)
        await record_status_event(conn, DEMO_BIZ, wamid, "delivered")
        assert await record_status_event(conn, DEMO_BIZ, wamid, "failed") is True
        assert await _status(conn, wamid) == "failed"


async def test_status_for_unknown_message_logs_but_no_update(pool):
    async with tenant_tx(pool) as (conn, _):
        wamid = f"wamid.{uuid4().hex[:10]}"  # niciun mesaj cu acest wamid
        updated = await record_status_event(conn, DEMO_BIZ, wamid, "delivered")
        assert updated is False
        n = await conn.fetchval(
            "select count(*) from message_status_events where provider_msg_id = $1", wamid
        )
        assert n == 1  # evenimentul e logat oricum
