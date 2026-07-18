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
        # NX-177: „vechi" = FINALIZAT demult. Testul seta `first_seen`, dar purja (NX-86) se uită
        # la `completed_at`/`claimed_at` — coloana aia nu mai e criteriu. Rândul nu se potrivea pe
        # nicio ramură → `deleted == 0`, raportat ca „cleanup stricat". Contract stale, nu bug.
        await conn.execute(
            "insert into inbound_dedupe "
            "(business_id, provider_msg_id, first_seen, claimed_at, completed_at) "
            "values ($1, $2, now() - interval '72 hours', now() - interval '72 hours', "
            "        now() - interval '72 hours')",
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

        async def _counts() -> tuple[int, int]:
            return (
                await conn.fetchval(
                    "select count(*) from outbox where conversation_id = $1", r1.conversation_id
                ),
                await conn.fetchval(
                    "select count(*) from messages where conversation_id = $1", r1.conversation_id
                ),
            )

        before = await _counts()

        # exact același payload (retry Meta care a scăpat de Redis layer 1)
        r2 = await handle_turn(conn, biz, channel_id, ev)
        assert r2.deduped is True
        assert r2.outbox_id is None

        # NX-177: invariantul dedup-ului e că retry-ul NU ADAUGĂ NIMIC — nu că un tur produce
        # exact 1 outbox + 2 mesaje. Un reply lung se sparge în 2 (P9) → „salut" dă 2 rânduri de
        # outbox și 3 mesaje, iar assert-urile fixe (1 / 2) picau pe o schimbare de compunere care
        # n-are legătură cu dedup-ul. Comparăm ÎNAINTE vs DUPĂ: singurul lucru care contează.
        assert await _counts() == before, "retry-ul a scris ceva — dedup-ul layer 2 nu ține"
