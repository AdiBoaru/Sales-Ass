"""Teste integration pentru worker-ul de procesare (G2b): handle_turn (echo e2e),
resolve_channel_by_phone, load_business. Ating DB-ul real → marcate `integration`.

Curățenie ca în test_queries_runtime: tranzacție rollback-uită + channel
throwaway, query-urile rulează sub rolul `bot_runtime` (RLS de producție).
"""

from contextlib import asynccontextmanager
from uuid import uuid4

import pytest

from src.db.connection import close_pool, get_pool
from src.db.queries.businesses import load_business
from src.db.queries.channels import resolve_channel_by_phone
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
    """Tranzacție rollback-uită + channel throwaway + rol bot_runtime activ.
    Yield: (conn, channel_id)."""
    async with pool.acquire() as conn:
        tr = conn.transaction()
        await tr.start()
        try:
            channel_id = await conn.fetchval(
                """
                insert into channels (business_id, kind, provider_account_id)
                values ($1, 'whatsapp', $2)
                returning id::text
                """,
                business_id,
                f"test-{uuid4()}",
            )
            await conn.execute("set local role bot_runtime")
            await conn.execute("select set_config('app.business_id', $1, true)", business_id)
            yield conn, channel_id
        finally:
            await tr.rollback()


def _event(body="salut", wamid=None):
    return {
        "channel_kind": "whatsapp",
        "channel_account_id": "PNID-demo",
        "sender_external_id": f"+40{uuid4().hex[:9]}",
        "provider_msg_id": wamid or f"wamid.{uuid4().hex[:10]}",
        "content_type": "text",
        "body": body,
        "media_id": None,
        "sender_name": "Ana",
    }


async def _echo_stage(ctx, deps):
    """Stage determinist pentru testele de plumbing (fără LLM real)."""
    ctx.set_reply(f"echo: {ctx.message.body}")


# --------------------------------------------------------------------------- #
# load_business
# --------------------------------------------------------------------------- #


async def test_load_business(pool):
    async with tenant_tx(pool) as (conn, _):
        biz = await load_business(conn, DEMO_BIZ)
    assert biz is not None
    assert biz.id == DEMO_BIZ
    assert biz.slug
    assert isinstance(biz.supported_locales, list)


# --------------------------------------------------------------------------- #
# resolve_channel_by_phone (control plane — admin/postgres, fără RLS)
# --------------------------------------------------------------------------- #


async def test_resolve_channel_by_phone(pool):
    async with pool.acquire() as conn:
        tr = conn.transaction()
        await tr.start()
        try:
            pnid = f"PN-{uuid4().hex[:8]}"
            await conn.execute(
                "insert into channels (business_id, kind, provider_account_id) "
                "values ($1, 'whatsapp', $2)",
                DEMO_BIZ,
                pnid,
            )
            found = await resolve_channel_by_phone(conn, pnid)
            assert found is not None
            assert found["business_id"] == DEMO_BIZ
            assert await resolve_channel_by_phone(conn, "does-not-exist") is None
        finally:
            await tr.rollback()


# --------------------------------------------------------------------------- #
# handle_turn — echo e2e (inbound → reply în outbox)
# --------------------------------------------------------------------------- #


async def test_handle_turn_echo_writes_outbox(pool):
    async with tenant_tx(pool) as (conn, channel_id):
        biz = await load_business(conn, DEMO_BIZ)
        result = await handle_turn(
            conn, biz, channel_id, _event(body="ce preț are X?"), stages=[_echo_stage]
        )

        # reply determinist din stage-ul de test (plumbing, fără LLM)
        assert result.reply_text is not None
        assert "ce preț are X?" in result.reply_text
        assert result.outbox_id is not None

        # un rând în outbox, pending, cu textul corect
        outbox = await conn.fetchrow(
            "select status, payload from outbox where id = $1", result.outbox_id
        )
        assert outbox["status"] == "pending"
        assert result.reply_text in outbox["payload"]  # payload jsonb ca text

        # două mesaje: inbound + outbound
        n = await conn.fetchval(
            "select count(*) from messages where conversation_id = $1", result.conversation_id
        )
        assert n == 2

        # state bumped + fereastra outbound atinsă
        row = await conn.fetchrow(
            "select state_version, last_outbound_at from conversations where id = $1",
            result.conversation_id,
        )
        assert row["state_version"] == 1
        assert row["last_outbound_at"] is not None


async def test_handle_turn_same_contact_same_conversation(pool):
    """Două mesaje de la ACELAȘI wa_id → un singur contact + o singură conversație
    (identity resolution + get_or_create_conversation), tururi distincte."""
    async with tenant_tx(pool) as (conn, channel_id):
        biz = await load_business(conn, DEMO_BIZ)
        ev1 = _event(body="salut")
        ev2 = {**ev1, "provider_msg_id": "wamid.second", "body": "încă unul"}
        r1 = await handle_turn(conn, biz, channel_id, ev1, stages=[_echo_stage])
        r2 = await handle_turn(conn, biz, channel_id, ev2, stages=[_echo_stage])
        assert r1.conversation_id == r2.conversation_id
        assert r1.contact_id == r2.contact_id
        assert r1.turn_id != r2.turn_id
