"""NX-237 — coșul canonic pe Postgres REAL (migrarea 041): RLS/FK/CHECK/unique + izolare tenant.

Exclus din CI fast (`-m "not integration"`). Ce nu poate dovedi un fake:

  • FK-ul COMPUS pe (business_id, product_id): produsul ALTUI tenant nu intră structural în coș;
  • UNIQUE NULLS NOT DISTINCT: două linii „fără variantă" ale aceluiași produs sunt UNA;
  • CHECK-ul de cantitate (1..10) — plasa peste politica din cod;
  • unique-ul de receipt (business_id, idempotency_key) — exact-once la nivel de DB;
  • serviciul REAL pe DB real: add → rând + versiune + receipt; retry aceeași cheie → replay.

Rulează DOAR dacă migrarea 041 e aplicată (altfel skip explicit, nu fail fals).
"""

from __future__ import annotations

import json
from uuid import uuid4

import asyncpg
import pytest

from src.commerce.cart_models import CartCommand
from src.commerce.cart_service import CartService
from src.db.connection import admin_conn, close_pool, get_pool
from src.db.provider import static_db
from src.db.queries import carts as q

pytestmark = [pytest.mark.integration]


async def _migrated(conn) -> bool:
    return bool(await conn.fetchval("select to_regclass('conversation_carts') is not null"))


async def _make_tenant(conn, *, price: float = 89.0) -> dict:
    bid = str(uuid4())
    await conn.execute(
        "insert into businesses (id, slug, name, vertical, status, default_locale) "
        "values ($1, $2, 'NX-237 cart', 'beauty_salon', 'active', 'ro')",
        bid,
        f"nx237-{uuid4().hex[:8]}",
    )
    channel_id = str(uuid4())
    await conn.execute(
        "insert into channels (id, business_id, kind, provider_account_id) "
        "values ($1, $2, 'webchat', $3)",
        channel_id,
        bid,
        f"tok-{uuid4().hex[:10]}",
    )
    contact_id = await conn.fetchval(
        "insert into contacts (business_id) values ($1) returning id", bid
    )
    conv = await conn.fetchval(
        "insert into conversations (business_id, contact_id, channel_id, state) "
        "values ($1, $2, $3, '{}'::jsonb) returning id::text",
        bid,
        str(contact_id),
        channel_id,
    )
    pid = await conn.fetchval(
        "insert into products (business_id, name, slug, price, currency, availability, "
        " stock_total, status) values ($1, 'Ser NX-237', $2, $3, 'RON', 'in_stock', 20, "
        "'active') returning id::text",
        bid,
        f"ser-{uuid4().hex[:8]}",
        price,
    )
    return {"bid": bid, "conv": conv, "pid": pid, "contact": str(contact_id)}


@pytest.fixture
async def tenants():
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        migrated = await _migrated(conn)
        if migrated:
            a = await _make_tenant(conn, price=89.0)
            b = await _make_tenant(conn, price=45.0)
    if not migrated:
        # Pool-ul e legat de event loop-ul ACESTUI test (pytest-asyncio face loop nou per test)
        # → îl închidem înainte de skip, altfel următorul test moștenește un pool mort.
        await close_pool()
        pytest.skip("migrarea 041 (conversation_carts) nu e aplicată pe acest DB")
    try:
        yield a, b
    finally:
        async with admin_conn(pool) as conn:
            for t in (a, b):
                await conn.execute("delete from businesses where id = $1", t["bid"])
        await close_pool()


def _service(conn, tenant) -> CartService:
    return CartService(
        db=static_db(conn),
        business_id=tenant["bid"],
        contact_id=tenant["contact"],
        language="ro",
        sla_s=86400,
    )


def _add(pid: str, qty: int = 1) -> CartCommand:
    return CartCommand.parse("add", {"product_id": pid, "quantity": qty})


# ── serviciul real pe DB real ───────────────────────────────────────────────────────────────


async def test_add_persists_versioned_cart_and_receipt(tenants):
    a, _ = tenants
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        out = await _service(conn, a).mutate(
            a["conv"], _add(a["pid"], 2), idempotency_key="k-1", turn_id="t-1"
        )
        assert out.ok and out.snapshot.version == 1
        assert out.snapshot.totals.value == 178.0
        cart = await q.get_active_cart(conn, a["bid"], a["conv"])
        assert cart is not None and cart["version"] == 1
        items = await q.get_cart_items(conn, a["bid"], cart["id"])
        assert [(it["product_id"], it["quantity"]) for it in items] == [(a["pid"], 2)]
        receipt = await q.get_receipt_by_key(conn, a["bid"], "k-1")
        assert receipt is not None and receipt["status"] == "succeeded"
        # Replay: aceeași cheie → același receipt, ZERO a doua mutație.
        replay = await _service(conn, a).mutate(
            a["conv"], _add(a["pid"], 2), idempotency_key="k-1", turn_id="t-1"
        )
        assert replay.ok and replay.receipt.replayed
        items = await q.get_cart_items(conn, a["bid"], cart["id"])
        assert items[0]["quantity"] == 2


async def test_cross_tenant_product_is_not_found(tenants):
    """Produsul tenantului B cerut de tenantul A = indistinct de unul inexistent (P7)."""
    a, b = tenants
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        out = await _service(conn, a).mutate(
            a["conv"], _add(b["pid"]), idempotency_key="k-x", turn_id="t-1"
        )
        assert out.error == "product_not_found"
        cart = await q.get_active_cart(conn, a["bid"], a["conv"])
        items = await q.get_cart_items(conn, a["bid"], cart["id"]) if cart else []
        assert not items


async def test_fk_blocks_foreign_product_structurally(tenants):
    """Chiar și un query GREȘIT (pe lângă serviciu) nu poate lega produsul altui tenant."""
    a, b = tenants
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        await q.create_cart_if_absent(conn, a["bid"], a["conv"])
        cart = await q.get_active_cart(conn, a["bid"], a["conv"])
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await q.insert_cart_item(conn, a["bid"], cart["id"], b["pid"], None, 1, "t-1")


async def test_quantity_check_and_null_variant_unique(tenants):
    a, _ = tenants
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        await q.create_cart_if_absent(conn, a["bid"], a["conv"])
        cart = await q.get_active_cart(conn, a["bid"], a["conv"])
        with pytest.raises(asyncpg.CheckViolationError):
            await q.insert_cart_item(conn, a["bid"], cart["id"], a["pid"], None, 11, "t-1")
        await q.insert_cart_item(conn, a["bid"], cart["id"], a["pid"], None, 1, "t-1")
        # NULLS NOT DISTINCT: a doua linie „fără variantă" a aceluiași produs e ACEEAȘI linie.
        with pytest.raises(asyncpg.UniqueViolationError):
            await q.insert_cart_item(conn, a["bid"], cart["id"], a["pid"], None, 1, "t-1")


async def test_receipt_key_is_exact_once_at_db_level(tenants):
    a, _ = tenants
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        first = await q.insert_receipt(
            conn,
            a["bid"],
            conversation_id=a["conv"],
            cart_id=None,
            operation="add",
            idempotency_key="dup-1",
            status="succeeded",
            before_version=0,
            after_version=1,
            result_code=None,
            turn_id="t-1",
            action_id=None,
        )
        assert first is not None
        second = await q.insert_receipt(
            conn,
            a["bid"],
            conversation_id=a["conv"],
            cart_id=None,
            operation="add",
            idempotency_key="dup-1",
            status="succeeded",
            before_version=0,
            after_version=2,
            result_code=None,
            turn_id="t-2",
            action_id=None,
        )
        assert second is None  # ON CONFLICT DO NOTHING → apelantul face replay


async def test_succeeded_receipt_requires_after_version(tenants):
    a, _ = tenants
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        with pytest.raises(asyncpg.CheckViolationError):
            await q.insert_receipt(
                conn,
                a["bid"],
                conversation_id=a["conv"],
                cart_id=None,
                operation="add",
                idempotency_key="bad-1",
                status="succeeded",
                before_version=0,
                after_version=None,  # o mutație „reușită" fără dovadă de efect
                result_code=None,
                turn_id="t-1",
                action_id=None,
            )


async def test_checkout_closes_cart_and_next_add_opens_new_one(tenants):
    a, _ = tenants
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        svc = _service(conn, a)
        await svc.mutate(a["conv"], _add(a["pid"]), idempotency_key="k-1", turn_id="t-1")
        out = await svc.create_checkout(
            a["conv"], idempotency_key="ck-1", turn_id="t-9", base_url="https://s.example/co"
        )
        assert out.ok and out.snapshot.status == "checked_out"
        link = await conn.fetchrow(
            "select ref_code, cart from checkout_links where business_id = $1", a["bid"]
        )
        assert link["ref_code"] == "t-9"
        cart_payload = link["cart"]
        if isinstance(cart_payload, str):
            cart_payload = json.loads(cart_payload)
        assert cart_payload[0]["price"] == 89.0  # prețul REHIDRATAT, nu afirmat
        # Coșul activ s-a închis; un add nou deschide ALT coș (partial unique permite).
        out2 = await svc.mutate(a["conv"], _add(a["pid"]), idempotency_key="k-2", turn_id="t-2")
        assert out2.ok and out2.snapshot.version == 1
        rows = await conn.fetch(
            "select status from conversation_carts where business_id = $1 order by created_at",
            a["bid"],
        )
        assert [r["status"] for r in rows] == ["checked_out", "active"]
