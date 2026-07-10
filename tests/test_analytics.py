"""Teste integration pentru persistarea analytics_events.

bot_runtime are DOAR INSERT pe analytics_events (append-only) — ca să citim
înapoi în test, ieșim din rol (`reset role` → postgres) în aceeași tranzacție.
Rollback la final → demo DB curat.
"""

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from src.db.connection import close_pool, get_pool
from src.db.queries.analytics import insert_events
from src.db.queries.businesses import load_business
from src.models import Event
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


async def test_insert_events_writes_rows_with_token_columns(pool):
    conv_id = str(uuid4())
    async with tenant_tx(pool) as (conn, _):
        n = await insert_events(
            conn,
            DEMO_BIZ,
            [
                Event("stage_completed", {"stage": "echo", "latency_ms": 1.2}),
                Event("llm_call", {"tokens_in": 300, "tokens_out": 40, "cost_usd": 0.0005}),
            ],
            conversation_id=conv_id,
        )
        assert n == 2

        # bot_runtime nu are SELECT pe analytics_events → citim ca admin
        await conn.execute("reset role")
        rows = await conn.fetch(
            "select event_type, tokens_in, cost_usd from analytics_events "
            "where conversation_id = $1 order by event_type",
            conv_id,
        )
        assert {r["event_type"] for r in rows} == {"stage_completed", "llm_call"}
        llm = next(r for r in rows if r["event_type"] == "llm_call")
        assert llm["tokens_in"] == 300
        assert float(llm["cost_usd"]) == pytest.approx(0.0005)


async def test_insert_events_empty_is_noop(pool):
    async with tenant_tx(pool) as (conn, _):
        assert await insert_events(conn, DEMO_BIZ, []) == 0


async def test_fetch_turn_events_preserves_insertion_order_on_created_at_tie(pool):
    """NX-146 felia 2 fix: `analytics_events.id` (identity, monotonic la insert) e tiebreaker-ul,
    NU `event_type` (alfabetic, poate reordona artificial traiectoria unui tur când 2 evenimente
    au exact același `created_at`, ex. inserate în aceeași tranzacție/batch)."""
    from src.db.queries.analytics import fetch_turn_events

    turn_id = str(uuid4())
    async with tenant_tx(pool) as (conn, _):
        ts = datetime(2026, 1, 1, 10, 0, 0, tzinfo=UTC)
        # „z_event" înainte de „a_event" — dacă tiebreaker-ul ar fi event_type, ordinea ar ieși
        # inversată alfabetic; cu `id` ca tiebreaker, ordinea de inserare se păstrează.
        await conn.execute(
            """
            insert into analytics_events (business_id, event_type, properties, turn_id, created_at)
            values ($1, 'z_event', '{}'::jsonb, $2, $3::timestamptz),
                   ($1, 'a_event', '{}'::jsonb, $2, $3::timestamptz)
            """,
            DEMO_BIZ,
            turn_id,
            ts,
        )
        await conn.execute("reset role")
        events = await fetch_turn_events(conn, DEMO_BIZ, turn_id)
        assert [e["event_type"] for e in events] == ["z_event", "a_event"]


async def test_handle_turn_persists_runner_events(pool):
    async with tenant_tx(pool) as (conn, channel_id):
        biz = await load_business(conn, DEMO_BIZ)
        result = await handle_turn(conn, biz, channel_id, _event())

        await conn.execute("reset role")
        types = await conn.fetch(
            "select event_type from analytics_events where conversation_id = $1",
            result.conversation_id,
        )
        evset = {r["event_type"] for r in types}
        # runner-ul emite stage_completed (per stagiu) + pipeline_early_exit (echo dă reply)
        assert "stage_completed" in evset
        assert "pipeline_early_exit" in evset
