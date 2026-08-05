"""NX-217 felia 3 — raportul complet pe DB reală (fereastră hibridă + stare catalog + funnel).

Exclus din CI fast. Ce se dovedește aici și nicăieri altundeva:
  - **fereastra hibridă**: ziua CURENTĂ apare în raport ÎNAINTE ca rollup-ul nocturn să ruleze
    (altfel comerciantul deschide raportul dimineața și nu vede nimic din ce s-a întâmplat azi);
  - aceeași dimensiune numărată identic din rollup și din evenimentele live (un singur CTE);
  - regulile citesc starea catalogului de ACUM: același fapt devine `restock` sau
    `add_to_catalog` după cum brandul există sau nu;
  - funnel-ul leagă cererea de comenzile atribuite (NX-162).
"""

import json
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest

from src.analytics.report import build_demand_report
from src.db.connection import admin_conn, close_pool, get_pool
from src.db.queries.demand_report import window_facts
from src.db.queries.demand_rollup import rollup_demand_day

pytestmark = [pytest.mark.integration]

TODAY = date(2026, 8, 4)
YESTERDAY = TODAY - timedelta(days=1)


async def _business(conn) -> str:
    bid = str(uuid4())
    await conn.execute(
        "insert into businesses (id, slug, name, vertical, status, default_locale) "
        "values ($1, $2, 'NX-217 report', 'beauty_salon', 'active', 'ro')",
        bid,
        f"nx217r-{uuid4().hex[:8]}",
    )
    return bid


async def _emit(conn, bid, event_type, props, *, day=YESTERDAY, conv=None):
    await conn.execute(
        "insert into analytics_events (business_id, conversation_id, event_type, properties, "
        "created_at) values ($1, $2, $3, $4::jsonb, $5)",
        bid,
        conv,
        event_type,
        json.dumps(props),
        datetime.combine(day, datetime.min.time(), tzinfo=UTC) + timedelta(hours=10),
    )


async def _brand_with_product(conn, bid, brand_name, *, availability="in_stock") -> str:
    brand_id = await conn.fetchval(
        "insert into brands (business_id, slug, name) values ($1, $2, $3) returning id",
        bid,
        f"b-{uuid4().hex[:8]}",
        brand_name,
    )
    return await conn.fetchval(
        "insert into products (business_id, brand_id, slug, name, price, status, availability) "
        "values ($1, $2, $3, 'Produs test', 100, 'active', $4) returning id::text",
        bid,
        brand_id,
        f"p-{uuid4().hex[:8]}",
        availability,
    )


@pytest.fixture
async def shop():
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        bid = await _business(conn)
    try:
        yield bid
    finally:
        async with admin_conn(pool) as conn:
            await conn.execute("delete from demand_daily where business_id = $1", bid)
            await conn.execute("delete from analytics_events where business_id = $1", bid)
            await conn.execute("delete from products where business_id = $1", bid)
            await conn.execute("delete from brands where business_id = $1", bid)
            await conn.execute("delete from businesses where id = $1", bid)
        await close_pool()


async def test_today_is_visible_before_the_nightly_rollup(shop):
    """Fereastra hibridă: evenimentele de AZI intră în raport fără să fi rulat rollup-ul.
    Fără asta, raportul e gol exact în momentul în care e deschis."""
    bid = shop
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        await _emit(conn, bid, "unmet_query", {"reason": "no_result", "brand": "Azi"}, day=TODAY)
        facts = await window_facts(
            conn, bid, TODAY - timedelta(days=7), TODAY + timedelta(days=1), today=TODAY
        )

    keys = {(f["signal_kind"], f["dimension_key"]) for f in facts}
    assert ("unmet_no_result", "Azi") in keys


async def test_rollup_and_live_day_are_summed_once_each(shop):
    """Istoricul (rollup) + ziua curentă (live) se adună o singură dată — fără dublare când
    rollup-ul a rulat pentru zilele închise."""
    bid = shop
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        await _emit(conn, bid, "unmet_query", {"reason": "no_result", "brand": "X"}, day=YESTERDAY)
        await _emit(conn, bid, "unmet_query", {"reason": "no_result", "brand": "X"}, day=TODAY)
        await rollup_demand_day(conn, YESTERDAY)  # doar ziua ÎNCHEIATĂ
        facts = await window_facts(conn, bid, YESTERDAY, TODAY + timedelta(days=1), today=TODAY)

    row = next(f for f in facts if f["dimension_key"] == "X")
    assert row["request_count"] == 2  # 1 din rollup + 1 live, nu 3, nu 1


async def test_same_fact_becomes_restock_or_add_to_catalog_by_catalog_state(shop):
    """Aceleași evenimente, verdicte diferite după starea catalogului de ACUM — motivul pentru
    care acțiunile NU se materializează niciodată."""
    bid = shop
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        for _ in range(4):
            await _emit(conn, bid, "unmet_query", {"reason": "no_result", "brand": "Avene"})
        await rollup_demand_day(conn, YESTERDAY)

        absent = await build_demand_report(conn, bid, days=7, min_requests=3, today=TODAY)
        await _brand_with_product(conn, bid, "Avene", availability="out_of_stock")
        present = await build_demand_report(conn, bid, days=7, min_requests=3, today=TODAY)

    assert [a["kind"] for a in absent["actions"]] == ["add_to_catalog"]
    assert [a["kind"] for a in present["actions"]] == ["restock"]
    assert present["actions"][0]["context"]["products_in_catalog"] == 1


async def test_report_carries_trend_from_previous_window(shop):
    bid = shop
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        for _ in range(5):  # fereastra curentă (ultimele 7 zile)
            await _emit(conn, bid, "unmet_query", {"reason": "no_result", "brand": "Trend"})
        for _ in range(3):  # fereastra precedentă
            await _emit(
                conn,
                bid,
                "unmet_query",
                {"reason": "no_result", "brand": "Trend"},
                day=TODAY - timedelta(days=9),
            )
        await rollup_demand_day(conn, YESTERDAY)
        await rollup_demand_day(conn, TODAY - timedelta(days=9))
        report = await build_demand_report(conn, bid, days=7, min_requests=3, today=TODAY)

    action = report["actions"][0]
    assert (action["count"], action["prev_count"]) == (5, 3)


async def test_report_shape_is_honest(shop):
    """Contractul: fereastră, acțiuni, indicatori de sănătate, funnel, venit (split NX-162
    păstrat separat) — și niciun `estimated_value` / `confidence` nicăieri în payload."""
    bid = shop
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        await _emit(conn, bid, "faq_lookup", {"layer": "miss"})
        await rollup_demand_day(conn, YESTERDAY)
        report = await build_demand_report(conn, bid, days=7, today=TODAY)

    assert set(report) == {"window", "actions", "health", "category_funnel", "revenue", "facts"}
    assert report["health"]["faq_misses"] == 1
    assert report["actions"] == []  # un miss de FAQ nu e o acțiune (nu știm despre ce era)
    assert "revenue_direct_bot" in report["revenue"] and "revenue_assisted" in report["revenue"]
    blob = json.dumps(report, ensure_ascii=False, default=str)
    assert "estimated_value" not in blob and "confidence" not in blob


async def test_category_funnel_links_demand_to_attributed_orders(shop):
    """Bucla cerere × conversie: căutările pe categorie și comenzile atribuite ajung în
    același rând, cu numărători SEPARAȚI (rata se derivă la afișare)."""
    bid = shop
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        cat_id = await conn.fetchval(
            "insert into categories (business_id, slug, name) values ($1, 'seruri', 'Seruri') "
            "returning id",
            bid,
        )
        pid = await conn.fetchval(
            "insert into products (business_id, primary_category_id, slug, name, price, status) "
            "values ($1, $2, $3, 'Ser', 120, 'active') returning id",
            bid,
            cat_id,
            f"p-{uuid4().hex[:8]}",
        )
        for _ in range(3):
            await _emit(conn, bid, "product_search", {"category_key": "seruri"})
        order_id = await conn.fetchval(
            "insert into orders (business_id, external_id, status, total, attribution, placed_at) "
            "values ($1, $2, 'paid', 120, 'direct_bot', $3) returning id",
            bid,
            f"o-{uuid4().hex[:8]}",
            datetime.combine(YESTERDAY, datetime.min.time(), tzinfo=UTC),
        )
        await conn.execute(
            "insert into order_items (order_id, product_id, name, quantity, unit_price) "
            "values ($1, $2, 'Ser', 1, 120)",
            order_id,
            pid,
        )
        await rollup_demand_day(conn, YESTERDAY)
        report = await build_demand_report(conn, bid, days=7, today=TODAY)

    row = next(r for r in report["category_funnel"] if r["category"] == "seruri")
    assert row["searched"] == 3 and row["ordered"] == 1
