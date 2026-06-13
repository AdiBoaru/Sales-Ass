"""Test integration pentru upsert_channel (onboarding, NX-63).

Rulează ca ADMIN (postgres) — channels e read-only pentru bot_runtime. Rollback
per test → demo DB curat.
"""

from uuid import uuid4

import pytest

from src.db.connection import close_pool, get_pool
from src.db.queries.channels import resolve_channel, upsert_channel

pytestmark = pytest.mark.integration

DEMO_BIZ = "6098812a-50fc-44bd-a1ba-bc77e6399158"


@pytest.fixture
async def pool():
    p = await get_pool()
    yield p
    await close_pool()


async def test_upsert_channel_is_idempotent(pool):
    async with pool.acquire() as conn:
        tr = conn.transaction()
        await tr.start()
        try:
            bot_id = f"tgbot-{uuid4().hex[:10]}"

            first = await upsert_channel(conn, DEMO_BIZ, "telegram", bot_id, display_name="@demo")
            assert first["created"] is True

            second = await upsert_channel(conn, DEMO_BIZ, "telegram", bot_id, display_name="@demo")
            assert second["created"] is False  # idempotent — același rând
            assert first["id"] == second["id"]

            row = await conn.fetchrow(
                "select kind, status, business_id::text as business_id from channels where id = $1",
                first["id"],
            )
            assert row["kind"] == "telegram"
            assert row["status"] == "active"
            assert row["business_id"] == DEMO_BIZ
        finally:
            await tr.rollback()


async def test_upsert_then_resolve_finds_it(pool):
    """După seed, resolve_channel('telegram', bot_id) îl găsește (ca în worker)."""
    async with pool.acquire() as conn:
        tr = conn.transaction()
        await tr.start()
        try:
            bot_id = f"tgbot-{uuid4().hex[:10]}"
            await upsert_channel(conn, DEMO_BIZ, "telegram", bot_id)
            found = await resolve_channel(conn, "telegram", bot_id)
            assert found is not None
            assert found["business_id"] == DEMO_BIZ
        finally:
            await tr.rollback()
