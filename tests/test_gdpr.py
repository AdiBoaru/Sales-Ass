"""NX-72 — strat GDPR. Două niveluri:

• UNIT (gate rapid): orchestrarea `_erase_on_conn`/`_export_on_conn` cu fake conn +
  query-uri monkeypatch-uite — tranziții de status, audit, izolare, eșec → failed.
• INTEGRATION (DB real, rollback, exclus din CI): erase anonimizează efectiv +
  export întoarce toate secțiunile. ZERO LLM (taskul nu cheamă niciun model).
"""

from contextlib import asynccontextmanager
from uuid import uuid4

import pytest

from src.db.connection import close_pool, get_pool
from src.gdpr import erase as g

DEMO_BIZ = "6098812a-50fc-44bd-a1ba-bc77e6399158"


# ============================================================================ #
# UNIT — fake conn + monkeypatch (rulează în gate)
# ============================================================================ #


class FakeTxn:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *a):
        return False


class FakeConn:
    def __init__(self, *, fail_on_erase=False):
        self.executed: list[tuple] = []
        self.fail_on_erase = fail_on_erase

    def transaction(self):
        return FakeTxn()

    async def execute(self, query, *args):
        self.executed.append((query, args))
        if self.fail_on_erase and "gdpr_erase_contact" in query:
            raise RuntimeError("erase boom")


def _patch_lifecycle(monkeypatch, *, owned=True):
    calls = {"create": [], "processing": [], "done": [], "failed": [], "audit": []}

    async def create_request(conn, biz, cid, kind, rb):
        calls["create"].append((biz, cid, kind, rb))
        return "req-1"

    async def mark_processing(conn, biz, rid):
        calls["processing"].append(rid)

    async def mark_done(conn, biz, rid, *, result_ref):
        calls["done"].append((rid, result_ref))

    async def mark_failed(conn, biz, rid):
        calls["failed"].append(rid)

    async def write_audit(conn, biz, action, entity, eid, details):
        calls["audit"].append((action, entity, eid, details))

    async def contact_in_business(conn, biz, cid):
        return owned

    monkeypatch.setattr(g, "create_request", create_request)
    monkeypatch.setattr(g, "mark_processing", mark_processing)
    monkeypatch.setattr(g, "mark_done", mark_done)
    monkeypatch.setattr(g, "mark_failed", mark_failed)
    monkeypatch.setattr(g, "write_audit", write_audit)
    monkeypatch.setattr(g, "contact_in_business", contact_in_business)
    return calls


def _patch_reads(monkeypatch, *, fail=False):
    async def fetch_contact(conn, biz, cid):
        if fail:
            raise RuntimeError("read boom")
        return {"display_name": "Ana", "consent": {}}

    async def fetch_identities(conn, biz, cid):
        return [{"channel_kind": "telegram", "external_id": "tg-1"}]

    async def fetch_conversations(conn, biz, cid):
        return [{"id": "conv1"}]

    async def fetch_messages(conn, biz, cid):
        return [{"body": "hi"}, {"body": "ms"}]

    async def count_messages(conn, biz, cid):
        return 5

    async def fetch_orders(conn, biz, cid):
        return [{"external_id": "o1"}]

    monkeypatch.setattr(g, "fetch_contact", fetch_contact)
    monkeypatch.setattr(g, "fetch_identities", fetch_identities)
    monkeypatch.setattr(g, "fetch_conversations", fetch_conversations)
    monkeypatch.setattr(g, "fetch_messages", fetch_messages)
    monkeypatch.setattr(g, "count_messages", count_messages)
    monkeypatch.setattr(g, "fetch_orders", fetch_orders)


async def test_erase_happy_runs_function_audits_and_marks_done(monkeypatch):
    calls = _patch_lifecycle(monkeypatch, owned=True)
    conn = FakeConn()
    req = await g._erase_on_conn(conn, "b1", "c1", requested_by="admin")
    assert req == "req-1"
    assert calls["processing"] == ["req-1"]
    assert any("gdpr_erase_contact" in q for q, _ in conn.executed)
    assert calls["audit"] == [
        ("gdpr_erase", "contact", "c1", {"request_id": "req-1", "requested_by": "admin"})
    ]
    assert calls["done"] == [("req-1", None)]
    assert calls["failed"] == []


async def test_erase_wrong_tenant_marks_failed_without_running_erase(monkeypatch):
    calls = _patch_lifecycle(monkeypatch, owned=False)
    conn = FakeConn()
    req = await g._erase_on_conn(conn, "b1", "c-altul", requested_by="admin")
    assert req == "req-1"
    assert calls["failed"] == ["req-1"]
    assert not any("gdpr_erase_contact" in q for q, _ in conn.executed)  # erase NU rulat
    assert calls["done"] == []


async def test_erase_db_failure_marks_failed_and_reraises(monkeypatch):
    calls = _patch_lifecycle(monkeypatch, owned=True)
    conn = FakeConn(fail_on_erase=True)
    with pytest.raises(RuntimeError, match="erase boom"):
        await g._erase_on_conn(conn, "b1", "c1", requested_by="admin")
    assert calls["failed"] == ["req-1"]
    assert calls["done"] == []


async def test_export_assembles_all_sections(monkeypatch):
    calls = _patch_lifecycle(monkeypatch)
    _patch_reads(monkeypatch)
    data = await g._export_on_conn(FakeConn(), "b1", "c1", requested_by="admin", kind="export")
    assert data["kind"] == "export"
    assert data["request_id"] == "req-1"
    assert data["contact"]["display_name"] == "Ana"
    assert len(data["identities"]) == 1
    assert len(data["messages"]) == 2  # dump integral
    assert "messages_count" not in data
    assert calls["audit"][0][0] == "gdpr_export"
    assert calls["done"] == [("req-1", None)]


async def test_access_uses_count_not_full_messages(monkeypatch):
    _patch_lifecycle(monkeypatch)
    _patch_reads(monkeypatch)
    data = await g._export_on_conn(FakeConn(), "b1", "c1", requested_by="admin", kind="access")
    assert data["kind"] == "access"
    assert "messages" not in data  # fără dump-ul integral
    assert data["messages_count"] == 5


async def test_export_read_failure_marks_failed_and_reraises(monkeypatch):
    calls = _patch_lifecycle(monkeypatch)
    _patch_reads(monkeypatch, fail=True)
    with pytest.raises(RuntimeError, match="read boom"):
        await g._export_on_conn(FakeConn(), "b1", "c1", requested_by="admin", kind="export")
    assert calls["failed"] == ["req-1"]
    assert calls["done"] == []


# ============================================================================ #
# INTEGRATION — DB real, rollback per test (exclus din CI: @pytest.mark.integration)
# ============================================================================ #


@pytest.fixture
async def pool():
    p = await get_pool()
    yield p
    await close_pool()


@asynccontextmanager
async def admin_tx(pool):
    """Tranzacție rollback-uită pe conexiunea privilegiată (GDPR rulează pe admin_conn)."""
    async with pool.acquire() as conn:
        tr = conn.transaction()
        await tr.start()
        try:
            yield conn
        finally:
            await tr.rollback()


async def _seed(conn, biz):
    cid = await conn.fetchval(
        "insert into contacts (business_id, display_name, profile, consent) "
        "values ($1, 'Ana', '{\"skin\":\"dry\"}'::jsonb, '{\"proactive\":true}'::jsonb) "
        "returning id::text",
        biz,
    )
    ext = f"tg-{uuid4().hex[:10]}"
    await conn.execute(
        "insert into channel_identities (business_id, contact_id, channel_kind, external_id) "
        "values ($1, $2, 'telegram', $3)",
        biz,
        cid,
        ext,
    )
    chan = await conn.fetchval(
        "insert into channels (business_id, kind, provider_account_id) values ($1, 'telegram', $2) "
        "returning id::text",
        biz,
        f"acc-{uuid4().hex[:10]}",
    )
    conv = await conn.fetchval(
        "insert into conversations (business_id, contact_id, channel_id) values ($1, $2, $3) "
        "returning id::text",
        biz,
        cid,
        chan,
    )
    for body in ("salut", "mulțumesc"):
        await conn.execute(
            "insert into messages "
            "(business_id, conversation_id, contact_id, direction, author, body) "
            "values ($1, $2, $3, 'inbound', 'contact', $4)",
            biz,
            conv,
            cid,
            body,
        )
    await conn.execute(
        "insert into orders (business_id, contact_id, external_id, status, total, placed_at) "
        "values ($1, $2, $3, 'paid', 99.5, now())",
        biz,
        cid,
        f"ord-{uuid4().hex[:10]}",
    )
    return cid


@pytest.mark.integration
async def test_erase_anonymizes_and_audits(pool):
    async with admin_tx(pool) as conn:
        cid = await _seed(conn, DEMO_BIZ)
        req = await g._erase_on_conn(conn, DEMO_BIZ, cid, requested_by="tester")

        n_ident = await conn.fetchval(
            "select count(*) from channel_identities where contact_id = $1", cid
        )
        assert n_ident == 0  # telefonul/chat.id dispar
        bodies = await conn.fetch("select body from messages where contact_id = $1", cid)
        assert bodies and all(r["body"] is None for r in bodies)
        row = await conn.fetchrow("select display_name, erased_at from contacts where id = $1", cid)
        assert row["display_name"] is None and row["erased_at"] is not None
        assert await conn.fetchval("select status from gdpr_requests where id = $1", req) == "done"
        n_audit = await conn.fetchval(
            "select count(*) from audit_log where action = 'gdpr_erase' "
            "and business_id = $1 and details->>'request_id' = $2",
            DEMO_BIZ,
            req,
        )
        assert n_audit >= 1


@pytest.mark.integration
async def test_erase_idempotent_second_run_still_done(pool):
    async with admin_tx(pool) as conn:
        cid = await _seed(conn, DEMO_BIZ)
        await g._erase_on_conn(conn, DEMO_BIZ, cid, requested_by="tester")
        req2 = await g._erase_on_conn(conn, DEMO_BIZ, cid, requested_by="tester")
        assert await conn.fetchval("select status from gdpr_requests where id = $1", req2) == "done"


@pytest.mark.integration
async def test_export_returns_all_sections(pool):
    async with admin_tx(pool) as conn:
        cid = await _seed(conn, DEMO_BIZ)
        data = await g._export_on_conn(conn, DEMO_BIZ, cid, requested_by="tester", kind="export")
        assert data["contact"]["display_name"] == "Ana"
        assert len(data["identities"]) == 1 and data["identities"][0]["external_id"].startswith(
            "tg-"
        )
        assert len(data["messages"]) == 2
        assert len(data["orders"]) == 1
        assert (
            await conn.fetchval(
                "select status from gdpr_requests where id = $1", data["request_id"]
            )
            == "done"
        )
