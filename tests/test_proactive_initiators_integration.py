"""Integration (DB real, rollback per test) — inițiatorii proactivi (PL-1).

Acoperă exact partea pe care unit-urile (fakes) n-o pot: SQL-ul real al `create_proactive_job`
(idempotența ON CONFLICT pe indexul parțial 019) + candidate-query-urile + sweep-urile end-to-end,
sub rol `bot_runtime` (RLS de producție). Migrarea 019 (`dedupe_key`) e aplicată TRANZACȚIONAL în
setup (idempotent: no-op după ce 019 e aplicat pe bune) ca testul să fie self-contained și să
ruleze chiar înainte de deploy. Totul e rollback-uit la final → demo DB curat.
"""

from contextlib import asynccontextmanager
from uuid import uuid4

import pytest

from src.db.connection import close_pool, get_pool
from src.db.queries.proactive import (
    create_proactive_job,
    find_abandoned_carts,
    find_restocked_subscriptions,
)
from src.proactive.initiators import sweep_abandoned_cart, sweep_back_in_stock

pytestmark = [pytest.mark.integration, pytest.mark.slow]

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
            # 019 aplicat TRANZACȚIONAL (idempotent) → self-contained; rollback la final
            await conn.execute(
                "alter table proactive_jobs add column if not exists dedupe_key text"
            )
            await conn.execute(
                "create unique index if not exists uq_proactive_jobs_dedupe "
                "on proactive_jobs (business_id, dedupe_key) where dedupe_key is not null"
            )
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


async def _contact_conv(conn, channel_id):
    contact_id = await conn.fetchval(
        "insert into contacts (business_id) values ($1) returning id::text", DEMO_BIZ
    )
    conv_id = await conn.fetchval(
        "insert into conversations (business_id, contact_id, channel_id) "
        "values ($1, $2, $3) returning id::text",
        DEMO_BIZ,
        contact_id,
        channel_id,
    )
    return contact_id, conv_id


async def test_create_proactive_job_dedup(pool):
    async with tenant_tx(pool) as (conn, channel_id):
        contact_id, conv_id = await _contact_conv(conn, channel_id)
        key = f"abandoned_cart:{uuid4().hex[:8]}"
        j1 = await create_proactive_job(
            conn,
            DEMO_BIZ,
            contact_id=contact_id,
            conversation_id=conv_id,
            kind="abandoned_cart",
            dedupe_key=key,
        )
        j2 = await create_proactive_job(
            conn,
            DEMO_BIZ,
            contact_id=contact_id,
            conversation_id=conv_id,
            kind="abandoned_cart",
            dedupe_key=key,
        )
        assert j1 is not None and j2 is None  # al doilea = deduplicat (ON CONFLICT DO NOTHING)

        # fără dedupe_key → mereu insert (NULL nu intră în indexul parțial)
        n1 = await create_proactive_job(
            conn,
            DEMO_BIZ,
            contact_id=contact_id,
            conversation_id=conv_id,
            kind="back_in_stock",
            payload={"product_id": "x"},
        )
        n2 = await create_proactive_job(
            conn,
            DEMO_BIZ,
            contact_id=contact_id,
            conversation_id=conv_id,
            kind="back_in_stock",
            payload={"product_id": "x"},
        )
        assert n1 is not None and n2 is not None


async def test_sweep_abandoned_cart_end_to_end(pool):
    async with tenant_tx(pool) as (conn, channel_id):
        contact_id, conv_id = await _contact_conv(conn, channel_id)
        # coș abandonat acum 2h, neconvertit, neexpirat
        await conn.execute(
            "insert into checkout_links "
            "(business_id, conversation_id, contact_id, ref_code, url, created_at) "
            "values ($1, $2, $3, $4, $5, now() - interval '2 hours')",
            DEMO_BIZ,
            conv_id,
            contact_id,
            f"ref-{uuid4().hex[:8]}",
            "https://shop.example/checkout?ref=x",
        )
        found = await find_abandoned_carts(
            conn, DEMO_BIZ, older_than_seconds=3600, max_age_seconds=604800, limit=100
        )
        assert any(c["conversation_id"] == conv_id for c in found)

        n = await sweep_abandoned_cart(
            conn, DEMO_BIZ, older_than_seconds=3600, max_age_seconds=604800, limit=100
        )
        assert n >= 1
        # al doilea sweep NU mai creează (dedup pe checkout_link_id)
        n2 = await sweep_abandoned_cart(
            conn, DEMO_BIZ, older_than_seconds=3600, max_age_seconds=604800, limit=100
        )
        assert n2 == 0
        cnt = await conn.fetchval(
            "select count(*) from proactive_jobs "
            "where business_id = $1 and conversation_id = $2 and kind = 'abandoned_cart'",
            DEMO_BIZ,
            conv_id,
        )
        assert cnt == 1


async def test_sweep_back_in_stock_end_to_end(pool):
    async with tenant_tx(pool) as (conn, channel_id):
        contact_id, conv_id = await _contact_conv(conn, channel_id)
        product_id = await conn.fetchval(
            "select id::text from products "
            "where business_id = $1 and availability in ('in_stock', 'low_stock') limit 1",
            DEMO_BIZ,
        )
        assert product_id, "demo catalog ar trebui să aibă produse pe stoc"
        await conn.execute(
            "insert into back_in_stock_subscriptions (business_id, contact_id, product_id) "
            "values ($1, $2, $3)",
            DEMO_BIZ,
            contact_id,
            product_id,
        )
        found = await find_restocked_subscriptions(conn, DEMO_BIZ, limit=200)
        mine = [s for s in found if s["contact_id"] == contact_id]
        assert mine and mine[0]["conversation_id"] == conv_id  # rutat pe conv-ul contactului

        n = await sweep_back_in_stock(conn, DEMO_BIZ, limit=300)
        assert n >= 1
        # marcat notified → al doilea sweep nu mai are candidați (re-subscribe l-ar re-arma)
        n2 = await sweep_back_in_stock(conn, DEMO_BIZ, limit=300)
        assert n2 == 0
        notified = await conn.fetchval(
            "select notified_at is not null from back_in_stock_subscriptions "
            "where business_id = $1 and contact_id = $2",
            DEMO_BIZ,
            contact_id,
        )
        assert notified is True
