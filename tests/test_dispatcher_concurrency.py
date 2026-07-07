from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest

import src.worker.dispatcher as disp
from src.db.queries import outbox
from src.proactive.scheduler import _outbox_priority_for_job


def _row(business_id: str, idx: int, *, priority: int = 10) -> dict:
    return {
        "id": f"{business_id}-{idx}",
        "conversation_id": f"conv-{business_id}",
        "kind": "message",
        "payload": {"type": "text", "to": "customer", "text": "hi"},
        "attempts": 1,
        "priority": priority,
        "created_at": datetime.now(UTC) - timedelta(milliseconds=250),
        "channel_kind": "whatsapp",
        "channel_account_id": "acct",
    }


class _FakeConn:
    def __init__(self, business_id: str):
        self.business_id = business_id


@pytest.mark.asyncio
async def test_claim_due_orders_by_explicit_priority():
    class Conn:
        query = ""

        async def fetch(self, query, *args):
            self.query = query
            self.args = args
            return []

    conn = Conn()
    assert await outbox.claim_due(conn, "biz-1") == []
    assert "order by o2.priority, o2.next_attempt_at, o2.id" in conn.query
    assert "o.priority" in conn.query
    assert "o.created_at" in conn.query


@pytest.mark.asyncio
async def test_enqueue_outbox_sends_priority_argument():
    class Conn:
        async def fetchval(self, query, *args):
            self.query = query
            self.args = args
            return "outbox-1"

    conn = Conn()
    out = await outbox.enqueue_outbox(
        conn,
        "biz-1",
        "conv-1",
        "turn-1",
        {"type": "text"},
        priority=outbox.OUTBOX_PRIORITY_MARKETING,
    )
    assert out == "outbox-1"
    assert "priority" in conn.query
    assert conn.args[-1] == outbox.OUTBOX_PRIORITY_MARKETING


def test_proactive_priority_classification():
    assert _outbox_priority_for_job("awb_update") == outbox.OUTBOX_PRIORITY_TRANSACTIONAL
    assert _outbox_priority_for_job("abandoned_cart") == outbox.OUTBOX_PRIORITY_MARKETING
    assert _outbox_priority_for_job("follow_up") == outbox.OUTBOX_PRIORITY_MARKETING


@pytest.mark.asyncio
async def test_dispatch_due_respects_global_and_tenant_concurrency(monkeypatch):
    rows = {biz: [_row(biz, i) for i in range(4)] for biz in ("A", "B", "C")}
    active_global = 0
    max_global = 0
    active_by_tenant = {biz: 0 for biz in rows}
    max_by_tenant = {biz: 0 for biz in rows}
    emitted = []

    @asynccontextmanager
    async def fake_admin_conn(pool):
        yield _FakeConn("admin")

    @asynccontextmanager
    async def fake_tenant_conn(business_id):
        yield _FakeConn(business_id)

    async def fake_business_ids(conn):
        return list(rows)

    async def fake_claim_due(conn, business_id, *, limit):
        assert conn.business_id == business_id
        return rows[business_id][:limit]

    async def fake_dispatch_row(conn, business_id, registry, row):
        nonlocal active_global, max_global
        active_global += 1
        active_by_tenant[business_id] += 1
        max_global = max(max_global, active_global)
        max_by_tenant[business_id] = max(max_by_tenant[business_id], active_by_tenant[business_id])
        await asyncio.sleep(0.01)
        active_by_tenant[business_id] -= 1
        active_global -= 1
        return "sent"

    async def fake_insert_events(conn, business_id, events, **kwargs):
        emitted.extend((business_id, event.type, event.properties) for event in events)
        return len(events)

    monkeypatch.setattr(disp, "admin_conn", fake_admin_conn)
    monkeypatch.setattr(disp, "tenant_conn", fake_tenant_conn)
    monkeypatch.setattr(disp, "business_ids_with_due_outbox", fake_business_ids)
    monkeypatch.setattr(disp, "claim_due", fake_claim_due)
    monkeypatch.setattr(disp, "dispatch_row", fake_dispatch_row)
    monkeypatch.setattr(disp, "insert_events", fake_insert_events)

    handled = await disp.dispatch_due(
        object(), object(), batch=4, global_concurrency=3, tenant_concurrency=2
    )

    assert handled == 12
    assert max_global <= 3
    assert all(v <= 2 for v in max_by_tenant.values())
    assert len(emitted) == 12
    statuses = {
        props["status"] for _, event_type, props in emitted if event_type == "outbox_dispatch"
    }
    assert statuses == {"sent"}


@pytest.mark.asyncio
async def test_slow_tenant_waiter_does_not_consume_global_slot(monkeypatch):
    rows = {"A": [_row("A", 1), _row("A", 2)], "B": [_row("B", 1)]}
    finished: list[str] = []

    @asynccontextmanager
    async def fake_admin_conn(pool):
        yield _FakeConn("admin")

    @asynccontextmanager
    async def fake_tenant_conn(business_id):
        yield _FakeConn(business_id)

    async def fake_business_ids(conn):
        return ["A", "B"]

    async def fake_claim_due(conn, business_id, *, limit):
        return rows[business_id][:limit]

    async def fake_dispatch_row(conn, business_id, registry, row):
        if row["id"] == "A-1":
            await asyncio.sleep(0.05)
        else:
            await asyncio.sleep(0.001)
        finished.append(row["id"])
        return "sent"

    async def fake_insert_events(conn, business_id, events, **kwargs):
        return len(events)

    monkeypatch.setattr(disp, "admin_conn", fake_admin_conn)
    monkeypatch.setattr(disp, "tenant_conn", fake_tenant_conn)
    monkeypatch.setattr(disp, "business_ids_with_due_outbox", fake_business_ids)
    monkeypatch.setattr(disp, "claim_due", fake_claim_due)
    monkeypatch.setattr(disp, "dispatch_row", fake_dispatch_row)
    monkeypatch.setattr(disp, "insert_events", fake_insert_events)

    handled = await disp.dispatch_due(
        object(), object(), batch=2, global_concurrency=2, tenant_concurrency=1
    )

    assert handled == 3
    assert finished[0] == "B-1"
