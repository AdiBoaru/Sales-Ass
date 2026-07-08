"""NX-148 felia 1 — teste integration pentru conversation_facts (grant + RLS + upsert).

Rulează sub rolul REAL `bot_runtime` + `app.business_id` (ca producția). Rollback la final →
demo DB curat. Excluse din CI (ating Supabase live), rulate manual / de verifier.
"""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from src.db.connection import close_pool, get_pool
from src.db.queries.facts import fetch_relevant_facts, upsert_facts

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
            await conn.execute("set local role bot_runtime")
            await conn.execute("select set_config('app.business_id', $1, true)", business_id)
            yield conn
        finally:
            await tr.rollback()


async def test_upsert_then_fetch_and_confidence_bump(pool):
    contact = str(uuid4())
    async with tenant_tx(pool) as conn:
        n = await upsert_facts(
            conn,
            DEMO_BIZ,
            contact,
            str(uuid4()),
            [
                {"fact_type": "budget_band", "fact_value": "100-200", "confidence": 0.6},
                {"fact_type": "skin_type", "fact_value": "sensitive", "confidence": 0.7},
            ],
        )
        assert n == 2

        got = await fetch_relevant_facts(conn, DEMO_BIZ, contact)
        by_type = {f["fact_type"]: f for f in got}
        assert set(by_type) == {"budget_band", "skin_type"}
        assert by_type["skin_type"]["fact_value"] == "sensitive"

        # re-menționare cu confidence mai mare → bump la max, fără duplicat.
        await upsert_facts(
            conn,
            DEMO_BIZ,
            contact,
            None,
            [{"fact_type": "skin_type", "fact_value": "sensitive", "confidence": 0.95}],
        )
        got2 = await fetch_relevant_facts(conn, DEMO_BIZ, contact)
        skin = [f for f in got2 if f["fact_type"] == "skin_type"]
        assert len(skin) == 1
        assert float(skin[0]["confidence"]) == pytest.approx(0.95)


async def test_expired_fact_not_returned(pool):
    contact = str(uuid4())
    past = datetime.now(timezone.utc) - timedelta(days=1)
    async with tenant_tx(pool) as conn:
        await upsert_facts(
            conn,
            DEMO_BIZ,
            contact,
            None,
            [
                {
                    "fact_type": "budget_band",
                    "fact_value": "50",
                    "confidence": 0.9,
                    "expires_at": past,
                }
            ],
        )
        got = await fetch_relevant_facts(conn, DEMO_BIZ, contact)
        assert got == []


async def test_tenant_isolation_facts_invisible_cross_business(pool):
    contact = str(uuid4())
    other_biz = str(uuid4())
    async with tenant_tx(pool) as conn:
        await upsert_facts(
            conn,
            DEMO_BIZ,
            contact,
            None,
            [{"fact_type": "budget_band", "fact_value": "100", "confidence": 0.8}],
        )
        # comută tenantul RLS → facts DEMO_BIZ devin invizibile (business_id = current_business_id).
        await conn.execute("select set_config('app.business_id', $1, true)", other_biz)
        got = await fetch_relevant_facts(conn, other_biz, contact)
        assert got == []
