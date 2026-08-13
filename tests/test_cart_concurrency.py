"""NX-237 — concurență REALĂ pe Postgres: 20 de mutații simultane pe același coș.

Exclus din CI fast (`-m "not integration"`). Frâna de concurență e lockul de rând
(`FOR UPDATE` în `lock_active_cart`), nu disciplina apelanților — aici o dovedim cu conexiuni
SEPARATE (un provider care face checkout real din pool per operație), exact ca în producție:

  • 20 de add-uri cu chei DISTINCTE → serializate; capul de cantitate ține (10), versiunile sunt
    monotone fără găuri, fiecare mutație reușită are exact un receipt;
  • 20 de retry-uri cu ACEEAȘI cheie → O singură mutație, un singur receipt, 19 replay-uri.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest

from src.commerce.cart_models import CART_MAX_LINE_QUANTITY, CartCommand
from src.commerce.cart_service import CartService
from src.db.connection import admin_conn, close_pool, get_pool
from src.db.queries import carts as q

pytestmark = [pytest.mark.integration]


async def _migrated(conn) -> bool:
    return bool(await conn.fetchval("select to_regclass('conversation_carts') is not null"))


@pytest.fixture
async def tenant():
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        migrated = await _migrated(conn)
    if not migrated:
        # Pool nou per event loop: îl închidem înainte de skip, altfel testul următor
        # moștenește un pool legat de un loop închis („Event loop is closed").
        await close_pool()
        pytest.skip("migrarea 041 (conversation_carts) nu e aplicată pe acest DB")
    async with admin_conn(pool) as conn:
        bid = str(uuid4())
        await conn.execute(
            "insert into businesses (id, slug, name, vertical, status, default_locale) "
            "values ($1, $2, 'NX-237 conc', 'beauty_salon', 'active', 'ro')",
            bid,
            f"nx237c-{uuid4().hex[:8]}",
        )
        channel_id = str(uuid4())
        await conn.execute(
            "insert into channels (id, business_id, kind, provider_account_id) "
            "values ($1, $2, 'webchat', $3)",
            channel_id,
            bid,
            f"tok-{uuid4().hex[:10]}",
        )
        contact = await conn.fetchval(
            "insert into contacts (business_id) values ($1) returning id", bid
        )
        conv = await conn.fetchval(
            "insert into conversations (business_id, contact_id, channel_id, state) "
            "values ($1, $2, $3, '{}'::jsonb) returning id::text",
            bid,
            str(contact),
            channel_id,
        )
        pid = await conn.fetchval(
            "insert into products (business_id, name, slug, price, currency, availability, "
            " stock_total, status) values ($1, 'Conc NX-237', $2, 60, 'RON', 'in_stock', 50, "
            "'active') returning id::text",
            bid,
            f"conc-{uuid4().hex[:8]}",
        )
    try:
        yield {"bid": bid, "conv": conv, "pid": pid}
    finally:
        async with admin_conn(pool) as conn:
            await conn.execute("delete from businesses where id = $1", bid)
        await close_pool()


def _pool_db(pool):
    """Provider REAL: fiecare operație = un checkout din pool (conexiuni separate → lockul de
    rând e singura serializare, exact ca în producție)."""

    @asynccontextmanager
    async def _cm(operation: str = ""):
        async with admin_conn(pool) as conn:
            yield conn

    return _cm


def _service(pool, tenant) -> CartService:
    return CartService(db=_pool_db(pool), business_id=tenant["bid"], sla_s=86400)


def _add(pid: str) -> CartCommand:
    return CartCommand.parse("add", {"product_id": pid, "quantity": 1})


async def test_twenty_concurrent_adds_serialize_on_row_lock(tenant):
    pool = await get_pool()
    svc = _service(pool, tenant)
    outcomes = await asyncio.gather(
        *[
            svc.mutate(
                tenant["conv"], _add(tenant["pid"]), idempotency_key=f"k-{i}", turn_id=f"t-{i}"
            )
            for i in range(20)
        ]
    )
    ok = [o for o in outcomes if o.ok]
    rejected = [o for o in outcomes if o.error == "quantity_invalid"]
    # Capul de linie (10) ține sub concurență: exact 10 reușesc, restul sunt refuzuri explicate.
    assert len(ok) == CART_MAX_LINE_QUANTITY
    assert len(rejected) == 20 - CART_MAX_LINE_QUANTITY
    versions = sorted(o.receipt.after_version for o in ok)
    assert versions == list(range(1, CART_MAX_LINE_QUANTITY + 1))  # monotone, fără găuri
    async with admin_conn(pool) as conn:
        cart = await q.get_active_cart(conn, tenant["bid"], tenant["conv"])
        items = await q.get_cart_items(conn, tenant["bid"], cart["id"])
        assert [(it["product_id"], it["quantity"]) for it in items] == [
            (tenant["pid"], CART_MAX_LINE_QUANTITY)
        ]
        receipts = await conn.fetch(
            "select status, count(*) as n from commerce_action_receipts "
            "where business_id = $1 group by status",
            tenant["bid"],
        )
        by_status = {r["status"]: r["n"] for r in receipts}
        assert by_status.get("succeeded") == CART_MAX_LINE_QUANTITY
        assert by_status.get("failed") == 20 - CART_MAX_LINE_QUANTITY


async def test_twenty_retries_same_key_are_one_mutation(tenant):
    pool = await get_pool()
    svc = _service(pool, tenant)
    outcomes = await asyncio.gather(
        *[
            svc.mutate(
                tenant["conv"], _add(tenant["pid"]), idempotency_key="same-key", turn_id="t-1"
            )
            for _ in range(20)
        ]
    )
    assert all(o.ok for o in outcomes)
    assert sum(1 for o in outcomes if not o.receipt.replayed) == 1
    assert sum(1 for o in outcomes if o.receipt.replayed) == 19
    async with admin_conn(pool) as conn:
        cart = await q.get_active_cart(conn, tenant["bid"], tenant["conv"])
        items = await q.get_cart_items(conn, tenant["bid"], cart["id"])
        assert items[0]["quantity"] == 1 and cart["version"] == 1
        n = await conn.fetchval(
            "select count(*) from commerce_action_receipts where business_id = $1",
            tenant["bid"],
        )
        assert n == 1
