"""NX-217 felia 2 — agregarea reală în `demand_daily` (DB reală, tenanți throwaway, cleanup).

Exclus din CI fast (`-m "not integration"`). Acoperă exact garanțiile care nu se pot dovedi fără
Postgres:
  - **moartea sursei**: după rollup, ștergerea evenimentelor brute NU schimbă raportul (singurul
    test care demonstrează că rollup-ul își merită existența);
  - **idempotență cu dispariție**: re-rularea după ce un eveniment a fost șters lasă zero rânduri
    orfane (dovada că DELETE+INSERT bate upsert-ul);
  - izolarea cross-tenant, containment-ul pe `product_ids` malformat, dovada (evidence) și
    absența PII în tabel.
"""

import json
from datetime import UTC, date, datetime
from uuid import uuid4

import pytest

from src.db.connection import admin_conn, close_pool, get_pool
from src.db.queries.demand_rollup import rollup_demand_day

pytestmark = [pytest.mark.integration]

DAY = date(2026, 8, 4)
AT = datetime(2026, 8, 4, 10, 0, tzinfo=UTC)


async def _make_business(conn) -> str:
    bid = str(uuid4())
    await conn.execute(
        "insert into businesses (id, slug, name, vertical, status, default_locale) "
        "values ($1, $2, 'NX-217 rollup', 'beauty_salon', 'active', 'ro')",
        bid,
        f"nx217-{uuid4().hex[:8]}",
    )
    return bid


async def _emit(conn, bid: str, event_type: str, props: dict, *, conv=None, at=AT) -> None:
    await conn.execute(
        "insert into analytics_events (business_id, conversation_id, event_type, properties, "
        "created_at) values ($1, $2, $3, $4::jsonb, $5)",
        bid,
        conv,
        event_type,
        json.dumps(props),
        at,
    )


async def _rows(conn, bid: str) -> dict[tuple[str, str, str], dict]:
    rows = await conn.fetch(
        "select signal_kind, dimension_kind, dimension_key, request_count, conversation_count, "
        "evidence_conversation_ids from demand_daily where business_id = $1 and day = $2",
        bid,
        DAY,
    )
    return {(r["signal_kind"], r["dimension_kind"], r["dimension_key"]): dict(r) for r in rows}


@pytest.fixture
async def shop():
    """Două businessuri throwaway (al doilea = martorul de izolare), cu cleanup complet."""
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        a, b = await _make_business(conn), await _make_business(conn)
    try:
        yield a, b
    finally:
        async with admin_conn(pool) as conn:
            for bid in (a, b):
                await conn.execute("delete from demand_daily where business_id = $1", bid)
                await conn.execute("delete from analytics_events where business_id = $1", bid)
                await conn.execute("delete from businesses where id = $1", bid)
        await close_pool()


async def test_unmet_aggregated_on_brand_and_category_with_evidence(shop):
    """Un `unmet_query` cu brand ȘI categorie produce DOUĂ rânduri (dimensiuni diferite) —
    de aceea nu se însumează niciodată peste `dimension_kind`."""
    bid, _ = shop
    pool = await get_pool()
    c1, c2 = str(uuid4()), str(uuid4())
    async with admin_conn(pool) as conn:
        for conv in (c1, c1, c2):  # 3 cereri, 2 conversații distincte
            await _emit(
                conn,
                bid,
                "unmet_query",
                {"reason": "no_result", "brand": "Bioderma", "category_key": "creme-fata"},
                conv=conv,
            )
        await rollup_demand_day(conn, DAY)
        rows = await _rows(conn, bid)

    brand = rows[("unmet_no_result", "brand", "Bioderma")]
    assert brand["request_count"] == 3
    assert brand["conversation_count"] == 2  # conversații distincte, nu evenimente
    assert {str(x) for x in brand["evidence_conversation_ids"]} == {c1, c2}
    assert ("unmet_no_result", "category", "creme-fata") in rows


async def test_survives_death_of_source_events(shop):
    """MOARTEA SURSEI: după rollup, ștergem evenimentele brute (echivalentul unui drop de
    partiție) — raportul rămâne identic. Fără asta, rollup-ul n-ar avea rost."""
    bid, _ = shop
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        await _emit(conn, bid, "unmet_query", {"reason": "out_of_stock", "brand": "X"}, conv=None)
        await rollup_demand_day(conn, DAY)
        before = await _rows(conn, bid)

        await conn.execute("delete from analytics_events where business_id = $1", bid)
        after = await _rows(conn, bid)

    assert before and after == before  # faptele au supraviețuit dispariției sursei


async def test_rerun_after_deletion_leaves_no_orphan_rows(shop):
    """IDEMPOTENȚĂ CU DISPARIȚIE: dimensiunea care nu mai are evenimente DISPARE la re-rulare.
    Un upsert ar fi lăsat rândul vechi pe loc → cerere fantomă raportată la infinit."""
    bid, _ = shop
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        await _emit(conn, bid, "unmet_query", {"reason": "no_result", "brand": "Efemer"})
        await _emit(conn, bid, "unmet_query", {"reason": "no_result", "brand": "Persistent"})
        await rollup_demand_day(conn, DAY)
        assert ("unmet_no_result", "brand", "Efemer") in await _rows(conn, bid)

        await conn.execute(
            "delete from analytics_events where business_id = $1 "
            "and properties->>'brand' = 'Efemer'",
            bid,
        )
        await rollup_demand_day(conn, DAY)
        rows = await _rows(conn, bid)

    assert ("unmet_no_result", "brand", "Efemer") not in rows
    assert ("unmet_no_result", "brand", "Persistent") in rows


async def test_rerun_is_stable_when_nothing_changed(shop):
    bid, _ = shop
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        await _emit(conn, bid, "product_search", {"brand": "Avene", "category_key": "seruri"})
        await rollup_demand_day(conn, DAY)
        first = await _rows(conn, bid)
        await rollup_demand_day(conn, DAY)
        second = await _rows(conn, bid)
    assert first == second  # fără dublare, fără drift


async def test_malformed_product_ids_are_contained(shop):
    """Un eveniment cu `product_ids` scalar/obiect nu are voie să oprească rollup-ul nocturn
    pentru toți tenanții — guard-ul `jsonb_typeof` îl sare, restul se agregă."""
    bid, _ = shop
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        await _emit(conn, bid, "agent_recommended", {"product_ids": "nu-e-array"})
        await _emit(conn, bid, "agent_recommended", {"product_ids": {"p": 1}})
        await _emit(conn, bid, "agent_recommended", {"product_ids": ["p-ok", "p-ok2"]})
        await rollup_demand_day(conn, DAY)
        rows = await _rows(conn, bid)

    assert rows[("recommended_product", "product", "p-ok")]["request_count"] == 1
    assert ("recommended_product", "product", "p-ok2") in rows
    assert len([k for k in rows if k[0] == "recommended_product"]) == 2  # nimic din cele stricate


async def test_tenant_isolation(shop):
    """Cererea tenantului B nu apare în raportul lui A (P7)."""
    a, b = shop
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        await _emit(conn, a, "unmet_query", {"reason": "no_result", "brand": "AlA"})
        await _emit(conn, b, "unmet_query", {"reason": "no_result", "brand": "AlB"})
        await rollup_demand_day(conn, DAY)
        rows_a, rows_b = await _rows(conn, a), await _rows(conn, b)

    assert ("unmet_no_result", "brand", "AlA") in rows_a
    assert ("unmet_no_result", "brand", "AlB") not in rows_a
    assert ("unmet_no_result", "brand", "AlB") in rows_b


async def test_only_the_target_day_is_aggregated(shop):
    """Fereastra e [zi, zi+1) în UTC: evenimentul de a doua zi nu contaminează ziua rulată."""
    bid, _ = shop
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        await _emit(conn, bid, "unmet_query", {"reason": "no_result", "brand": "Azi"}, at=AT)
        await _emit(
            conn,
            bid,
            "unmet_query",
            {"reason": "no_result", "brand": "Maine"},
            at=datetime(2026, 8, 5, 0, 30, tzinfo=UTC),
        )
        await rollup_demand_day(conn, DAY)
        rows = await _rows(conn, bid)

    assert ("unmet_no_result", "brand", "Azi") in rows
    assert ("unmet_no_result", "brand", "Maine") not in rows


async def test_unknown_reason_is_ignored(shop):
    """Allowlist de reason-uri: un `reason` necunoscut nu inventează un `signal_kind` (l-ar
    respinge constrângerea din DB și ar pica tot rollup-ul)."""
    bid, _ = shop
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        await _emit(conn, bid, "unmet_query", {"reason": "inventat", "brand": "X"})
        await _emit(conn, bid, "unmet_query", {"reason": "price_gap", "product_ids": ["p9"]})
        written = await rollup_demand_day(conn, DAY)
        rows = await _rows(conn, bid)

    assert written == 1
    assert ("unmet_price_gap", "product", "p9") in rows


async def test_no_pii_in_stored_columns(shop):
    """Dovada pe CONȚINUT, nu pe contract: chiar dacă un event ar căra text personal în alte
    câmpuri, în `demand_daily` ajung DOAR dimensiuni normalizate + id-uri."""
    bid, _ = shop
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        await _emit(
            conn,
            bid,
            "unmet_query",
            {
                "reason": "no_result",
                "brand": "Bioderma",
                "query": "sunt Ion Popescu, 0722123456",
                "note": "ion@example.ro",
            },
        )
        await rollup_demand_day(conn, DAY)
        blob = json.dumps(
            [
                {k: str(v) for k, v in r.items()}
                for r in await conn.fetch("select * from demand_daily where business_id = $1", bid)
            ],
            ensure_ascii=False,
        )

    assert "Popescu" not in blob and "0722" not in blob and "@" not in blob


async def test_faq_miss_and_clarify_signals(shop):
    """Semnalele de conținut: FAQ fără răspuns (fără dimensiune azi) + câmpul cerut la
    clarificare (dimensiune reală → acțiune „adaugă informația asta")."""
    bid, _ = shop
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        await _emit(conn, bid, "faq_lookup", {"layer": "miss", "similarity": 0.4})
        await _emit(conn, bid, "faq_lookup", {"layer": "exact"})  # hit → nu e semnal
        await _emit(conn, bid, "clarify_asked", {"field": "category", "attempts": 1})
        await rollup_demand_day(conn, DAY)
        rows = await _rows(conn, bid)

    assert rows[("faq_miss", "none", "")]["request_count"] == 1
    assert ("clarify_asked", "clarify_field", "category") in rows
