"""Teste integration pentru dedupe layer 2 (NX-51): claim_inbound, cleanup, și
guard-ul din handle_turn. Ating DB-ul real (tabel inbound_dedupe din 004).

Rollback per test → demo DB curat; query-urile rulează sub rol bot_runtime (RLS).
"""

from contextlib import asynccontextmanager
from uuid import uuid4

import pytest

from src.db.connection import close_pool, get_pool
from src.db.queries.businesses import load_business
from src.db.queries.inbound_dedupe import claim_inbound, cleanup_inbound_dedupe
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
                f"test-{uuid4()}",
            )
            await conn.execute("set local role bot_runtime")
            await conn.execute("select set_config('app.business_id', $1, true)", business_id)
            yield conn, channel_id
        finally:
            await tr.rollback()


def _event(wamid, body="salut"):
    return {
        "channel_kind": "whatsapp",
        "channel_account_id": "PNID-demo",
        "sender_external_id": f"+40{uuid4().hex[:9]}",
        "provider_msg_id": wamid,
        "content_type": "text",
        "body": body,
        "media_id": None,
        "sender_name": "Ana",
    }


# --------------------------------------------------------------------------- #
# claim_inbound
# --------------------------------------------------------------------------- #


async def test_claim_first_true_then_false(pool):
    async with tenant_tx(pool) as (conn, _):
        wamid = f"wamid.{uuid4().hex}"
        assert await claim_inbound(conn, DEMO_BIZ, wamid) is True
        assert await claim_inbound(conn, DEMO_BIZ, wamid) is False  # duplicat


async def test_cleanup_deletes_only_old(pool):
    async with tenant_tx(pool) as (conn, _):
        recent = f"wamid.{uuid4().hex}"
        old = f"wamid.{uuid4().hex}"
        await claim_inbound(conn, DEMO_BIZ, recent)
        # inserăm unul „vechi" cu first_seen în trecut
        await conn.execute(
            "insert into inbound_dedupe (business_id, provider_msg_id, first_seen) "
            "values ($1, $2, now() - interval '72 hours')",
            DEMO_BIZ,
            old,
        )
        deleted = await cleanup_inbound_dedupe(conn, older_than_hours=48)
        assert deleted >= 1
        # cel recent rămâne (re-claim → False), cel vechi a dispărut (re-claim → True)
        assert await claim_inbound(conn, DEMO_BIZ, recent) is False
        assert await claim_inbound(conn, DEMO_BIZ, old) is True


# --------------------------------------------------------------------------- #
# guard în handle_turn (layer 2 oprește dublura)
# --------------------------------------------------------------------------- #


async def test_handle_turn_dedupes_retry(pool):
    async with tenant_tx(pool) as (conn, channel_id):
        biz = await load_business(conn, DEMO_BIZ)
        ev = _event(wamid=f"wamid.{uuid4().hex}")

        r1 = await handle_turn(conn, biz, channel_id, ev)
        assert r1.deduped is False
        assert r1.outbox_id is not None

        # exact același payload (retry Meta care a scăpat de Redis layer 1)
        r2 = await handle_turn(conn, biz, channel_id, ev)
        assert r2.deduped is True
        assert r2.outbox_id is None

        # un singur outbox, un singur mesaj inbound + un singur outbound
        n_outbox = await conn.fetchval(
            "select count(*) from outbox where conversation_id = $1", r1.conversation_id
        )
        n_msgs = await conn.fetchval(
            "select count(*) from messages where conversation_id = $1", r1.conversation_id
        )
        assert n_outbox == 1
        assert n_msgs == 2
