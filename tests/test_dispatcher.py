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


async def _claim_mine(conn, turn) -> dict:
    """NX-177: rândul revendicat care aparține ACESTUI test, din tot ce întoarce `claim_due`.

    `claim_due` revendică TOT ce e pending pe tenant, iar tenantul demo e PARTAJAT: rulările de
    sim, widgetul web sau un test manual lasă rânduri commit-uite în `outbox`. Testele asertau
    `len(rows) == 1` / `[row] = ...` → picau cu «assert 2 == 1» de îndată ce mai exista un rând —
    un eșec de IGIENĂ raportat ca regresie de dispatcher.

    Filtrăm pe `outbox_id`-ul întors de `handle_turn` → testul e despre rândul LUI, indiferent ce
    altceva mai e în coadă. (Datele reziduale rămân o problemă separată: scripts/sim/cleanup.py.)"""
    rows = await claim_due(conn, DEMO_BIZ)
    mine = [r for r in rows if str(r["id"]) == str(turn.outbox_id)]
    assert len(mine) == 1, (
        f"rândul turului ({turn.outbox_id}) nu e printre cele {len(rows)} revendicate"
    )
    return mine[0]


# --------------------------------------------------------------------------- #
# claim_due — întoarce channel_kind + channel_account_id (expeditor) din join
# --------------------------------------------------------------------------- #


async def test_claim_returns_sender_channel(pool):
    async with tenant_tx(pool) as (conn, channel_id):
        pnid = await conn.fetchval(
            "select provider_account_id from channels where id = $1", channel_id
        )
        turn = await _enqueue_via_turn(conn, channel_id)
        row = await _claim_mine(conn, turn)
        assert row["channel_kind"] == "whatsapp"
        assert row["channel_account_id"] == pnid  # ĂSTA e testul: join-ul aduce expeditorul
        assert row["payload"]["type"] == "text"


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
        row = await _claim_mine(conn, turn)

        meta = FakeMeta()
        status = await dispatch_row(conn, DEMO_BIZ, _registry(meta), row)

        assert status == "sent"
        assert len(meta.calls) == 1  # un singur apel pe canal
        assert meta.calls[0][0] == row["channel_account_id"]  # account expeditor corect

        ob = await conn.fetchrow(
            "select status, sent_message_id::text from outbox where id = $1", row["id"]
        )
        assert ob["status"] == "sent"
        # NX-177: mesajul legat de RÂNDUL DISPATCH-uit, nu „vreun outbound al conversației".
        # Un reply lung se sparge în 2 mesaje (P9) → un tur produce 2 rânduri de outbox + 2 mesaje
        # outbound, iar `dispatch_row` leagă wamid-ul DOAR de al lui. Vechiul `fetchrow` fără
        # `order by` lua unul la întâmplare și pica pe «assert None is not None» când nimerea
        # mesajul celuilalt rând. Contract stale, nu regresie de dispatcher.
        assert ob["sent_message_id"] is not None, (
            "dispatch-ul n-a legat mesajul de rândul de outbox"
        )
        msg = await conn.fetchrow(
            "select provider_msg_id, status, direction from messages where id = $1",
            ob["sent_message_id"],
        )
        assert msg["direction"] == "outbound"
        assert msg["provider_msg_id"] is not None  # wamid-ul întors de canal
        assert msg["status"] == "sent"


async def test_dispatch_row_failure_marks_failed_with_backoff(pool):
    async with tenant_tx(pool) as (conn, channel_id):
        turn = await _enqueue_via_turn(conn, channel_id)
        row = await _claim_mine(conn, turn)

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
        turn = await _enqueue_via_turn(conn, channel_id)
        row = await _claim_mine(conn, turn)
        empty_registry = ChannelSenderRegistry()  # niciun sender
        status = await dispatch_row(conn, DEMO_BIZ, empty_registry, row)
        assert status == "dead"


async def test_claim_visibility_timeout_hides_then_reclaims(pool):
    """Reaper implicit: un rând 'dispatching' rămas (dispatcher mort) redevine
    scadent după visibility timeout și e re-revendicat. now() e constant în
    tranzacție, deci simulăm expirarea împingând next_attempt_at în trecut."""
    async with tenant_tx(pool) as (conn, channel_id):
        turn = await _enqueue_via_turn(conn, channel_id)
        # NX-177: primul claim ia TOT ce e scadent pe tenantul (partajat) demo — inclusiv rânduri
        # reziduale. Ne interesează DOAR rândul nostru; restul e zgomot de mediu, nu subiectul.
        first = await claim_due(conn, DEMO_BIZ, visibility_timeout_s=120)
        oid = str(turn.outbox_id)
        assert oid in [str(r["id"]) for r in first]

        # imediat după claim: 'dispatching' cu next_attempt_at în viitor → ascuns
        assert oid not in [str(r["id"]) for r in await claim_due(conn, DEMO_BIZ)]

        # dispatcher „mort": visibility timeout expirat (next_attempt_at în trecut)
        await conn.execute(
            "update outbox set next_attempt_at = now() - interval '1 second' where id = $1",
            oid,
        )
        reclaimed = await claim_due(conn, DEMO_BIZ)
        assert oid in [str(r["id"]) for r in reclaimed]  # același rând, re-revendicat
