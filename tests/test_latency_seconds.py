"""Integration — coloana generată `messages.latency_s` (secunde). DB real, rollback per test.

Aplică migrarea 020 TRANZACȚIONAL (idempotent: no-op după deploy) → self-contained; inserează un
mesaj cu `latency_ms` și verifică `latency_s = latency_ms/1000` (2 zecimale) + NULL→NULL. Rollback.
"""

from contextlib import asynccontextmanager
from uuid import uuid4

import pytest

from src.db.connection import close_pool, get_pool

pytestmark = [pytest.mark.integration, pytest.mark.slow]

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
            # 020 aplicat tranzacțional ca owner (înainte de bot_runtime) → self-contained
            await conn.execute(
                "alter table messages add column if not exists latency_s numeric(6,2) "
                "generated always as (round(latency_ms / 1000.0, 2)) stored"
            )
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


async def _contact_conv(conn, channel_id):
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
    return contact_id, conv_id


async def test_latency_s_generated_from_ms(pool):
    async with tenant_tx(pool) as (conn, channel_id):
        contact_id, conv_id = await _contact_conv(conn, channel_id)
        await conn.execute(
            "insert into messages (business_id, conversation_id, contact_id, direction, author, "
            "provider_msg_id, status, latency_ms) "
            "values ($1, $2, $3, 'outbound', 'bot', $4, 'sent', 2500)",
            DEMO_BIZ,
            conv_id,
            contact_id,
            f"wamid.{uuid4().hex[:10]}",
        )
        s = await conn.fetchval(
            "select latency_s from messages where business_id = $1 and conversation_id = $2 "
            "and direction = 'outbound'",
            DEMO_BIZ,
            conv_id,
        )
        assert float(s) == 2.50  # 2500ms → 2.50s (secunde, 2 zecimale)


async def test_latency_s_null_when_ms_null(pool):
    async with tenant_tx(pool) as (conn, channel_id):
        contact_id, conv_id = await _contact_conv(conn, channel_id)
        await conn.execute(
            "insert into messages (business_id, conversation_id, contact_id, direction, author, "
            "provider_msg_id, status) values ($1, $2, $3, 'inbound', 'contact', $4, 'received')",
            DEMO_BIZ,
            conv_id,
            contact_id,
            f"wamid.{uuid4().hex[:10]}",
        )
        s = await conn.fetchval(
            "select latency_s from messages where business_id = $1 and conversation_id = $2 "
            "and direction = 'inbound'",
            DEMO_BIZ,
            conv_id,
        )
        assert s is None  # latency_ms NULL (mesaj inbound) → latency_s NULL
