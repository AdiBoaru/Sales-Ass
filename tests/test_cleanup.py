"""NX-84 — job cleanup. Unit (gate): cutoff/drop/allowlist/parse cu mock/fake conn.
Integration (DB real, rollback): expire_semantic_cache șterge doar expirate. ZERO LLM.
"""

from contextlib import asynccontextmanager
from datetime import date
from uuid import uuid4

import pytest

from src.db.connection import close_pool, get_pool
from src.db.queries import maintenance as m
from src.db.queries.maintenance import _PART_RE, expire_semantic_cache
from src.db.queries.semantic_cache import upsert_entry
from src.jobs import cleanup

DEMO_BIZ = "6098812a-50fc-44bd-a1ba-bc77e6399158"


# --------------------------------------------------------------------------- #
# _cutoff_month — calcul pur (fără dateutil)
# --------------------------------------------------------------------------- #


def test_cutoff_month():
    assert cleanup._cutoff_month(date(2026, 8, 15), 6) == date(2026, 2, 1)
    assert cleanup._cutoff_month(date(2026, 1, 10), 1) == date(2025, 12, 1)  # cross-year


# --------------------------------------------------------------------------- #
# drop_old_partitions — retenție + graniță strictă + izolare la eșec
# --------------------------------------------------------------------------- #


async def test_drop_old_partitions_respects_retention_and_boundary(monkeypatch):
    parts = [
        ("messages_2026_01", date(2026, 1, 1)),
        ("messages_2026_02", date(2026, 2, 1)),  # == cutoff → NU se șterge (strict)
        ("messages_2026_03", date(2026, 3, 1)),
        ("analytics_events_2026_01", date(2026, 1, 1)),
    ]
    drop_calls: list[str] = []

    async def f_list(conn):
        return parts

    async def f_drop(conn, name):
        drop_calls.append(name)

    monkeypatch.setattr(cleanup, "list_time_partitions", f_list)
    monkeypatch.setattr(cleanup, "drop_partition", f_drop)
    dropped = await cleanup.drop_old_partitions(
        object(), retention_months=6, today=date(2026, 8, 1)
    )
    # cutoff = 2026-02-01 → doar lunile < cutoff (ianuarie)
    assert set(dropped) == {"messages_2026_01", "analytics_events_2026_01"}
    assert set(drop_calls) == {"messages_2026_01", "analytics_events_2026_01"}


async def test_drop_old_partitions_zero_when_all_recent(monkeypatch):
    async def f_list(conn):
        return [("messages_2026_07", date(2026, 7, 1))]

    async def f_drop(conn, name):
        raise AssertionError("nu trebuie chemat")

    monkeypatch.setattr(cleanup, "list_time_partitions", f_list)
    monkeypatch.setattr(cleanup, "drop_partition", f_drop)
    assert (
        await cleanup.drop_old_partitions(object(), retention_months=6, today=date(2026, 8, 1))
        == []
    )


async def test_drop_partition_failure_is_isolated(monkeypatch):
    parts = [
        ("messages_2026_01", date(2026, 1, 1)),
        ("analytics_events_2026_01", date(2026, 1, 1)),
    ]

    async def f_list(conn):
        return parts

    async def f_drop(conn, name):
        if name == "messages_2026_01":
            raise RuntimeError("boom")

    monkeypatch.setattr(cleanup, "list_time_partitions", f_list)
    monkeypatch.setattr(cleanup, "drop_partition", f_drop)
    dropped = await cleanup.drop_old_partitions(
        object(), retention_months=6, today=date(2026, 8, 1)
    )
    assert dropped == ["analytics_events_2026_01"]  # cel picat sărit, restul continuă


# --------------------------------------------------------------------------- #
# _PART_RE + drop_partition — allowlist anti-injection
# --------------------------------------------------------------------------- #


def test_part_re_matches_monthly_not_default():
    assert _PART_RE.match("messages_2026_06")
    assert _PART_RE.match("analytics_events_2026_12")
    assert not _PART_RE.match("messages_default")
    assert not _PART_RE.match("messages")
    assert not _PART_RE.match("orders_2026_06")  # alt tabel


async def test_drop_partition_rejects_injection():
    class C:
        async def execute(self, q):
            raise AssertionError("DDL nu trebuie executat pe nume ne-validat")

    with pytest.raises(ValueError, match="ne-validat"):
        await m.drop_partition(C(), 'messages_2026_01"; drop table businesses; --')


async def test_drop_partition_valid_executes():
    calls: list[str] = []

    class C:
        async def execute(self, q):
            calls.append(q)

    await m.drop_partition(C(), "messages_2026_06")
    assert calls and "messages_2026_06" in calls[0]


# --------------------------------------------------------------------------- #
# expire_semantic_cache — parsarea „DELETE <n>" (failure case: DELETE 0)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("res", "expected"), [("DELETE 2", 2), ("DELETE 0", 0), ("", 0)])
async def test_expire_parse_delete_count(res, expected):
    class C:
        async def execute(self, q):
            return res

    assert await m.expire_semantic_cache(C()) == expected


# --------------------------------------------------------------------------- #
# _run_on_conn — agregă rezultatele
# --------------------------------------------------------------------------- #


async def test_run_on_conn_aggregates(monkeypatch):
    async def f_drop(conn, *, retention_months):
        return ["messages_2026_01", "analytics_events_2026_01"]

    async def f_expire(conn):
        return 5

    monkeypatch.setattr(cleanup, "drop_old_partitions", f_drop)
    monkeypatch.setattr(cleanup, "expire_semantic_cache", f_expire)
    res = await cleanup._run_on_conn(object(), retention_months=6)
    assert res == {"partitions_dropped": 2, "cache_expired": 5}


# ============================================================================ #
# INTEGRATION — expire pe DB real, rollback (exclus din CI)
# ============================================================================ #


@pytest.fixture
async def pool():
    p = await get_pool()
    yield p
    await close_pool()


@asynccontextmanager
async def admin_tx(pool):
    async with pool.acquire() as conn:
        tr = conn.transaction()
        await tr.start()
        try:
            yield conn
        finally:
            await tr.rollback()


@pytest.mark.integration
async def test_expire_semantic_cache_deletes_only_expired(pool):
    async with admin_tx(pool) as conn:
        exp = f"exp-{uuid4().hex}"
        val = f"val-{uuid4().hex}"
        emb = [0.0] * 1536
        common = dict(
            embedding=emb,
            answer="a",
            volatility_class="static",
            embedding_model="m",
            quality_score=1.0,
        )
        # expirat (ttl_days=-1 → expires_at = now() - 1 zi)
        await upsert_entry(
            conn, DEMO_BIZ, "ro", canonical_str="q1", canonical_hash=exp, ttl_days=-1, **common
        )
        # valabil (ttl_days=1)
        await upsert_entry(
            conn, DEMO_BIZ, "ro", canonical_str="q2", canonical_hash=val, ttl_days=1, **common
        )

        deleted = await expire_semantic_cache(conn)
        assert deleted >= 1  # cel puțin al nostru (poate prinde și alte expirate din DB)

        gone = await conn.fetchval(
            "select count(*) from semantic_cache where business_id = $1 and canonical_hash = $2",
            DEMO_BIZ,
            exp,
        )
        kept = await conn.fetchval(
            "select count(*) from semantic_cache where business_id = $1 and canonical_hash = $2",
            DEMO_BIZ,
            val,
        )
        assert gone == 0 and kept == 1
