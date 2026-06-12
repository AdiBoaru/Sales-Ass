"""Teste integration pentru query-urile runtime (G1): contacts, conversations,
messages, outbox. Ating DB-ul real Supabase → marcate `integration`, excluse din
CI. Rulează local: pytest -m integration tests/test_queries_runtime.py

Curățenie: fiecare test rulează într-o tranzacție pe care o facem ROLLBACK la
final — demo DB rămâne neatins. În tranzacție creăm un `channel` throwaway ca
`postgres` (bot_runtime n-are INSERT pe channels), apoi coborâm la `bot_runtime`
cu `SET LOCAL ROLE` ca să exercităm query-urile EXACT cu RLS-ul de producție.
"""

from contextlib import asynccontextmanager
from uuid import uuid4

import pytest

from src.db.connection import close_pool, get_pool
from src.db.queries.contacts import get_or_create_contact
from src.db.queries.conversations import (
    StateConflict,
    get_or_create_conversation,
    patch_conversation_state,
    touch_last_inbound,
)
from src.db.queries.messages import get_recent_messages, insert_message
from src.db.queries.outbox import claim_due, enqueue_outbox, mark_failed, mark_sent
from src.models import Author, Direction

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
    Yield: (conn, channel_id). Tot ce se scrie dispare la ieșire."""
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
            # coborâm la bot_runtime pentru restul (RLS ca în producție)
            await conn.execute("set local role bot_runtime")
            await conn.execute("select set_config('app.business_id', $1, true)", business_id)
            yield conn, channel_id
        finally:
            await tr.rollback()


# --------------------------------------------------------------------------- #
# contacts — identity resolution
# --------------------------------------------------------------------------- #


async def test_contact_created_then_resolved(pool):
    async with tenant_tx(pool) as (conn, _):
        ext = f"+4072{uuid4().hex[:7]}"
        c1 = await get_or_create_contact(conn, DEMO_BIZ, "whatsapp", ext, display_name="Ana")
        c2 = await get_or_create_contact(conn, DEMO_BIZ, "whatsapp", ext)
        assert c1.id == c2.id  # același user → același contact
        assert c1.business_id == DEMO_BIZ
        assert c1.display_name == "Ana"
        assert isinstance(c1.profile, dict)


async def test_different_external_id_different_contact(pool):
    async with tenant_tx(pool) as (conn, _):
        a = await get_or_create_contact(conn, DEMO_BIZ, "whatsapp", f"+40{uuid4().hex[:9]}")
        b = await get_or_create_contact(conn, DEMO_BIZ, "whatsapp", f"+40{uuid4().hex[:9]}")
        assert a.id != b.id


# --------------------------------------------------------------------------- #
# conversations — load/create + optimistic lock
# --------------------------------------------------------------------------- #


async def test_conversation_get_or_create_is_idempotent(pool):
    async with tenant_tx(pool) as (conn, channel_id):
        contact = await get_or_create_contact(conn, DEMO_BIZ, "whatsapp", f"+40{uuid4().hex[:9]}")
        conv1 = await get_or_create_conversation(conn, DEMO_BIZ, contact.id, channel_id)
        conv2 = await get_or_create_conversation(conn, DEMO_BIZ, contact.id, channel_id)
        assert conv1["id"] == conv2["id"]
        assert conv1["status"] == "open"
        assert conv1["state_version"] == 0
        assert conv1["state"] == {}


async def test_patch_state_optimistic_lock(pool):
    async with tenant_tx(pool) as (conn, channel_id):
        contact = await get_or_create_contact(conn, DEMO_BIZ, "whatsapp", f"+40{uuid4().hex[:9]}")
        conv = await get_or_create_conversation(conn, DEMO_BIZ, contact.id, channel_id)

        v1 = await patch_conversation_state(
            conn, DEMO_BIZ, conv["id"], {"a": 1}, expected_version=0
        )
        assert v1 == 1

        # versiune greșită (0, dar acum e 1) → conflict
        with pytest.raises(StateConflict):
            await patch_conversation_state(conn, DEMO_BIZ, conv["id"], {"a": 2}, expected_version=0)

        # versiunea corectă merge mai departe + touch_outbound
        v2 = await patch_conversation_state(
            conn, DEMO_BIZ, conv["id"], {"a": 2}, expected_version=1, touch_outbound=True
        )
        assert v2 == 2
        row = await conn.fetchrow(
            "select state, last_outbound_at from conversations where id = $1", conv["id"]
        )
        assert row["last_outbound_at"] is not None


async def test_touch_last_inbound(pool):
    async with tenant_tx(pool) as (conn, channel_id):
        contact = await get_or_create_contact(conn, DEMO_BIZ, "whatsapp", f"+40{uuid4().hex[:9]}")
        conv = await get_or_create_conversation(conn, DEMO_BIZ, contact.id, channel_id)
        await touch_last_inbound(conn, DEMO_BIZ, conv["id"])
        ts = await conn.fetchval(
            "select last_inbound_at from conversations where id = $1", conv["id"]
        )
        assert ts is not None


# --------------------------------------------------------------------------- #
# messages — insert + istoric (max 8)
# --------------------------------------------------------------------------- #


async def test_insert_and_roundtrip_message(pool):
    async with tenant_tx(pool) as (conn, channel_id):
        contact = await get_or_create_contact(conn, DEMO_BIZ, "whatsapp", f"+40{uuid4().hex[:9]}")
        conv = await get_or_create_conversation(conn, DEMO_BIZ, contact.id, channel_id)
        mid = await insert_message(
            conn,
            DEMO_BIZ,
            conv["id"],
            contact.id,
            Direction.INBOUND,
            Author.CONTACT,
            body="salut",
        )
        assert mid
        msgs = await get_recent_messages(conn, DEMO_BIZ, conv["id"])
        assert len(msgs) == 1
        assert msgs[0].body == "salut"
        assert msgs[0].direction == Direction.INBOUND
        assert msgs[0].author == Author.CONTACT


async def test_history_capped_at_8_oldest_first(pool):
    async with tenant_tx(pool) as (conn, channel_id):
        contact = await get_or_create_contact(conn, DEMO_BIZ, "whatsapp", f"+40{uuid4().hex[:9]}")
        conv = await get_or_create_conversation(conn, DEMO_BIZ, contact.id, channel_id)
        # now() e constant în tranzacție → forțăm created_at distincte explicit
        for i in range(10):
            await conn.execute(
                """
                insert into messages
                    (business_id, conversation_id, contact_id, direction, author, body, created_at)
                values ($1, $2, $3, 'inbound', 'contact', $4, now() + make_interval(secs => $5))
                """,
                DEMO_BIZ,
                conv["id"],
                contact.id,
                f"m{i}",
                i,
            )
        msgs = await get_recent_messages(conn, DEMO_BIZ, conv["id"])
        assert len(msgs) == 8  # cap dur
        assert [m.body for m in msgs] == [f"m{i}" for i in range(2, 10)]  # ultimele 8, cronologic


# --------------------------------------------------------------------------- #
# outbox — enqueue idempotent + claim SKIP LOCKED + mark
# --------------------------------------------------------------------------- #


async def test_enqueue_is_idempotent(pool):
    async with tenant_tx(pool) as (conn, channel_id):
        contact = await get_or_create_contact(conn, DEMO_BIZ, "whatsapp", f"+40{uuid4().hex[:9]}")
        conv = await get_or_create_conversation(conn, DEMO_BIZ, contact.id, channel_id)
        key = f"turn-{uuid4()}"
        first = await enqueue_outbox(conn, DEMO_BIZ, conv["id"], key, {"text": "hi"})
        dup = await enqueue_outbox(conn, DEMO_BIZ, conv["id"], key, {"text": "hi again"})
        assert first is not None
        assert dup is None  # același idempotency_key → nu dublăm


async def test_claim_due_marks_dispatching_once(pool):
    async with tenant_tx(pool) as (conn, channel_id):
        contact = await get_or_create_contact(conn, DEMO_BIZ, "whatsapp", f"+40{uuid4().hex[:9]}")
        conv = await get_or_create_conversation(conn, DEMO_BIZ, contact.id, channel_id)
        oid = await enqueue_outbox(conn, DEMO_BIZ, conv["id"], f"turn-{uuid4()}", {"text": "hi"})

        first = await claim_due(conn, DEMO_BIZ)
        ids = [r["id"] for r in first]
        assert oid in ids
        claimed = next(r for r in first if r["id"] == oid)
        assert claimed["attempts"] == 1
        assert claimed["payload"] == {"text": "hi"}

        # al doilea claim nu-l mai vede (status = dispatching)
        second = await claim_due(conn, DEMO_BIZ)
        assert oid not in [r["id"] for r in second]


async def test_mark_sent_and_failed(pool):
    async with tenant_tx(pool) as (conn, channel_id):
        contact = await get_or_create_contact(conn, DEMO_BIZ, "whatsapp", f"+40{uuid4().hex[:9]}")
        conv = await get_or_create_conversation(conn, DEMO_BIZ, contact.id, channel_id)

        sent_id = await enqueue_outbox(conn, DEMO_BIZ, conv["id"], f"turn-{uuid4()}", {"t": 1})
        await mark_sent(conn, DEMO_BIZ, sent_id)
        st = await conn.fetchval("select status from outbox where id = $1", sent_id)
        assert st == "sent"

        fail_id = await enqueue_outbox(conn, DEMO_BIZ, conv["id"], f"turn-{uuid4()}", {"t": 2})
        status = await mark_failed(conn, DEMO_BIZ, fail_id, attempts=1, error="boom")
        assert status == "failed"
        row = await conn.fetchrow(
            "select status, last_error, next_attempt_at > now() as scheduled "
            "from outbox where id = $1",
            fail_id,
        )
        assert row["status"] == "failed"
        assert row["last_error"] == "boom"
        assert row["scheduled"] is True

        # epuizarea încercărilor → dead
        dead = await mark_failed(conn, DEMO_BIZ, fail_id, attempts=6, error="boom")
        assert dead == "dead"
