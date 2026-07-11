"""NX-164 — Demand Queries: read-side agregat peste faptele de cerere. SQL REAL (seed + rollback).

Două straturi:
- pure (rulează în CI): logica `_evidence` (dedup + cap) + gărzi de sursă (zero `estimated_value`/
  `confidence`, `WHERE business_id = $1` pe fiecare query);
- integration (`@pytest.mark.integration`, EXCLUS din CI): seedează `analytics_events`/`usage_daily`
  sintetic într-o tranzacție ROLLBACK-uită și verifică agregările reale, izolarea tenant, drilldown
  fără PII. Citirea pe conn ADMIN (bot_runtime n-are SELECT pe analytics_events).
"""

import inspect
import json
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest

from src.db.queries import demand

DEMO_BIZ = "6098812a-50fc-44bd-a1ba-bc77e6399158"


# --- pure (CI) ---------------------------------------------------------------


def test_evidence_dedups_preserves_order_and_caps():
    raw = ["c1", "c1", "c2", "c3", "c2", "c4", "c5", "c6", "c7"]
    out = demand._evidence(raw, cap=3)
    assert out == ["c1", "c2", "c3"]  # unic, ordine păstrată, capat


def test_evidence_handles_none_and_empty():
    assert demand._evidence(None) == []
    assert demand._evidence(["c1", None, "", "c2"], cap=5) == ["c1", "c2"]


def test_module_has_no_estimated_value_or_confidence():
    """Invariant NX-164: read-side onest — nicio estimare de bani, niciun scor de inferență."""
    src = inspect.getsource(demand)
    assert "estimated_value" not in src
    assert "confidence" not in src


def test_every_query_is_tenant_scoped():
    """P7: fiecare query filtrează explicit pe business_id = $1 (izolare, mecanism primar)."""
    src = inspect.getsource(demand)
    # câte SELECT-uri din analytics_events/usage_daily, atâtea `business_id = $1`.
    assert src.count("business_id = $1") >= 4


def test_module_is_read_side_only():
    """Read-side strict: nicio scriere în cod (INSERT/UPDATE/DELETE) — doar SELECT/agregare."""
    src = inspect.getsource(demand).lower()
    assert "insert into" not in src
    assert "update " not in src
    assert "delete " not in src


# --- integration (DB real, exclus din CI) ------------------------------------

pytest_integration = pytest.mark.integration


@pytest.fixture
async def pool():
    from src.db.connection import close_pool, get_pool

    p = await get_pool()
    yield p
    await close_pool()


@pytest.fixture
def biz():
    """business_id PROASPĂT per test (analytics_events n-are FK pe business_id) → seed hermetic,
    zero interferență cu datele reale ale tenantului demo din aceeași fereastră de timp."""
    return str(uuid4())


@asynccontextmanager
async def admin_tx(pool):
    """Tranzacție pe conn ADMIN (service role — SELECT pe analytics_events + insert de seed),
    ROLLBACK la final → DB demo curat. Read-side-ul rulează exact pe acest tip de conn."""
    async with pool.acquire() as conn:
        tr = conn.transaction()
        await tr.start()
        try:
            yield conn
        finally:
            await tr.rollback()


async def _ins(conn, business_id, event_type, props, *, conversation_id=None, created_at=None):
    await conn.execute(
        """
        insert into analytics_events
            (business_id, conversation_id, event_type, properties, created_at)
        values ($1, $2, $3, $4::jsonb, coalesce($5, now()))
        """,
        business_id,
        conversation_id,
        event_type,
        json.dumps(props),
        created_at,
    )


def _window():
    now = datetime.now(UTC)
    return now - timedelta(hours=1), now + timedelta(hours=1)


@pytest_integration
async def test_top_unmet_no_result_groups_by_brand_with_evidence(pool, biz):
    since, until = _window()
    async with admin_tx(pool) as conn:
        for _ in range(3):
            await _ins(
                conn,
                biz,
                "unmet_query",
                {"reason": "no_result", "brand": "Bioderma"},
                conversation_id=str(uuid4()),
            )
        await _ins(
            conn,
            biz,
            "unmet_query",
            {"reason": "no_result", "brand": "Avene"},
            conversation_id=str(uuid4()),
        )
        rows = await demand.top_unmet(conn, biz, since, until, reason="no_result")

    by_brand = {r["brand"]: r for r in rows}
    assert by_brand["Bioderma"]["request_count"] == 3
    assert len(by_brand["Bioderma"]["evidence_conversation_ids"]) == 3  # 3 conversații distincte
    assert by_brand["Avene"]["request_count"] == 1
    assert rows[0]["brand"] == "Bioderma"  # ordonat desc pe count


@pytest_integration
async def test_top_unmet_reason_filter_separates_named_not_found(pool, biz):
    since, until = _window()
    async with admin_tx(pool) as conn:
        await _ins(conn, biz, "unmet_query", {"reason": "no_result", "brand": "X"})
        await _ins(conn, biz, "unmet_query", {"reason": "named_not_found", "brand": "X"})
        no_result = await demand.top_unmet(conn, biz, since, until, reason="no_result")
        named = await demand.top_unmet(conn, biz, since, until, reason="named_not_found")

    assert sum(r["request_count"] for r in no_result) == 1
    assert sum(r["request_count"] for r in named) == 1  # reason-urile NU se amestecă


@pytest_integration
async def test_top_products_unnests_and_skips_events_without_ids(pool, biz):
    since, until = _window()
    async with admin_tx(pool) as conn:
        await _ins(conn, biz, "agent_recommended", {"n": 2, "product_ids": ["p1", "p2"]})
        await _ins(conn, biz, "agent_recommended", {"n": 1, "product_ids": ["p1"]})
        await _ins(conn, biz, "agent_recommended", {"n": 0})  # vechi, fără product_ids → sărit
        rows = await demand.top_products(conn, biz, since, until, event_type="agent_recommended")

    counts = {r["product_id"]: r["mention_count"] for r in rows}
    assert counts == {"p1": 2, "p2": 1}  # unnest corect; evenimentul fără ids nu crapă


@pytest_integration
async def test_top_requested_brands(pool, biz):
    since, until = _window()
    async with admin_tx(pool) as conn:
        await _ins(conn, biz, "product_search", {"brand": "CeraVe", "count": 5})
        await _ins(conn, biz, "product_search", {"brand": "CeraVe", "count": 3})
        await _ins(conn, biz, "product_search", {"brand": "Vichy", "count": 1})
        await _ins(conn, biz, "product_search", {"count": 0})  # fără brand → sărit
        rows = await demand.top_requested_brands(conn, biz, since, until)

    counts = {r["brand"]: r["request_count"] for r in rows}
    assert counts == {"CeraVe": 2, "Vichy": 1}


@pytest_integration
async def test_tenant_isolation_no_cross_leak(pool):
    since, until = _window()
    biz_a, biz_b = str(uuid4()), str(uuid4())  # analytics_events.business_id n-are FK → 2 tenanți
    async with admin_tx(pool) as conn:
        await _ins(conn, biz_a, "unmet_query", {"reason": "no_result", "brand": "Mine"})
        await _ins(conn, biz_b, "unmet_query", {"reason": "no_result", "brand": "Theirs"})
        mine = await demand.top_unmet(conn, biz_a, since, until, reason="no_result")
        theirs = await demand.top_unmet(conn, biz_b, since, until, reason="no_result")

    assert {r["brand"] for r in mine} == {"Mine"}  # tenantul A nu vede „Theirs"
    assert {r["brand"] for r in theirs} == {"Theirs"}


@pytest_integration
async def test_empty_window_returns_empty_not_crash(pool, biz):
    async with admin_tx(pool) as conn:
        far_past = datetime(2020, 1, 1, tzinfo=UTC)
        rows = await demand.top_unmet(
            conn, biz, far_past, far_past + timedelta(hours=1), reason="no_result"
        )
        report = await demand.demand_report(conn, biz, far_past, far_past + timedelta(hours=1))
    assert rows == []
    assert all(section == [] for section in report.values())


@pytest_integration
async def test_drilldown_evidence_is_ids_only_no_pii(pool, biz):
    since, until = _window()
    conv = str(uuid4())
    async with admin_tx(pool) as conn:
        await _ins(
            conn,
            biz,
            "unmet_query",
            {"reason": "no_result", "brand": "Bioderma"},
            conversation_id=conv,
        )
        rows = await demand.top_unmet(conn, biz, since, until, reason="no_result")

    row = rows[0]
    assert row["evidence_conversation_ids"] == [conv]  # drilldown = conversation_id, nimic altceva
    assert set(row) == {"brand", "category_key", "request_count", "evidence_conversation_ids"}


@pytest_integration
async def test_revenue_summary_split_never_combined(pool):
    async with admin_tx(pool) as conn:
        d = date(2026, 7, 1)
        await conn.execute(
            """
            insert into usage_daily
                (business_id, day, orders_attributed, revenue_attributed,
                 orders_direct_bot, revenue_direct_bot, orders_assisted, revenue_assisted)
            values ($1, $2, 5, 500, 2, 200, 3, 300)
            on conflict (business_id, day) do update set
                orders_direct_bot = excluded.orders_direct_bot,
                revenue_direct_bot = excluded.revenue_direct_bot,
                orders_assisted = excluded.orders_assisted,
                revenue_assisted = excluded.revenue_assisted
            """,
            DEMO_BIZ,
            d,
        )
        summary = await demand.revenue_summary(conn, DEMO_BIZ, d, d + timedelta(days=1))

    # bot-led și assisted întorși SEPARAT — niciun câmp care să-i însumeze
    assert summary["orders_direct_bot"] == 2 and summary["revenue_direct_bot"] == 200.0
    assert summary["orders_assisted"] == 3 and summary["revenue_assisted"] == 300.0
    assert "estimated_value" not in summary
    assert not any(k for k in summary if "combined" in k or "total" in k)
