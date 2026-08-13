"""NX-237 — CartService: comenzi, versiuni, idempotency, receipts. ZERO DB/LLM real.

Persistența e store-ul in-memory PARTAJAT (`tests/fake_commerce.py`) care implementează exact
contractul query-urilor din `db/queries/carts.py`. Faptele se hidratează prin `build_facts` REAL
(doar `load_cart_facts_rows` e înlocuit cu rândurile fixture) — deci semantica UNKNOWN/stoc/
monedă exersată aici e cea de producție, nu o copie de test.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from src.commerce.cart_models import CART_MAX_LINES, CartCommand
from src.commerce.cart_service import (
    CartService,
    action_idempotency_key,
    tool_idempotency_key,
)
from src.db.provider import static_db
from tests.fake_commerce import NOW, Store, install, product_row

CONV = "conv-1"
BIZ = "biz-1"

P1 = "11111111-1111-4111-8111-111111111111"
P2 = "22222222-2222-4222-8222-222222222222"
V1 = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


@pytest.fixture()
def store(monkeypatch) -> Store:
    st = Store()
    install(st, monkeypatch.setattr)
    return st


def service(store: Store, *, policy: Any = None, adapter: Any = None) -> CartService:
    events: list[tuple[str, dict]] = []
    svc = CartService(
        db=static_db(object()),
        business_id=BIZ,
        contact_id="contact-1",
        language="ro",
        policy=policy,
        emit=lambda name, **props: events.append((name, props)),
        safety_emit=lambda decision, purpose: events.append(("safety", {"purpose": purpose})),
        adapter=adapter,
        sla_s=86400,
        now=lambda: NOW,
    )
    svc.events = events  # type: ignore[attr-defined]
    return svc


def add_cmd(pid: str = P1, qty: int = 1, variant: str | None = None, **kw) -> CartCommand:
    return CartCommand.parse(
        "add", {"product_id": pid, "variant_id": variant, "quantity": qty, **kw}
    )


async def do_add(svc, key: str = "k-1", cmd: CartCommand | None = None):
    return await svc.mutate(CONV, cmd or add_cmd(), idempotency_key=key, turn_id="t-1")


# ── happy paths ─────────────────────────────────────────────────────────────────────────────


async def test_add_creates_cart_receipt_and_versioned_snapshot(store):
    store.products[P1] = product_row(P1, price=89.0)
    out = await do_add(service(store))
    assert out.ok and out.receipt is not None
    assert out.receipt.status == "succeeded" and not out.receipt.replayed
    assert (out.receipt.before_version, out.receipt.after_version) == (0, 1)
    snap = out.snapshot
    assert snap.version == 1 and snap.status == "active"
    assert [(ln.product_id, ln.quantity) for ln in snap.lines] == [(P1, 1)]
    assert snap.totals.status == "known" and snap.totals.value == 89.0
    assert snap.totals.display == "89,00 lei"  # display string SERVER-side, nu în FE
    assert snap.checkout_eligible
    assert snap.to_state_ref() == {"id": snap.cart_id, "version": 1, "lines": 1}
    assert out.products and out.products[0]["id"] == P1  # raw → validator


async def test_add_merges_same_line_and_bumps_version(store):
    store.products[P1] = product_row(P1)
    svc = service(store)
    await do_add(svc, "k-1")
    out = await do_add(svc, "k-2", add_cmd(qty=2))
    assert out.ok
    assert [(ln.product_id, ln.quantity) for ln in out.snapshot.lines] == [(P1, 3)]
    assert out.snapshot.version == 2  # monoton, o singură creștere per mutație


async def test_snapshot_rehydrates_fresh_price_not_stored_one(store):
    store.products[P1] = product_row(P1, price=100.0)
    svc = service(store)
    await do_add(svc)
    store.products[P1]["price"] = 149.0  # prețul se schimbă în catalog DUPĂ add
    snap = await svc.get_snapshot(CONV)
    assert snap.lines[0].unit_price == 149.0  # faptele de ACUM, nu copia de la add
    assert snap.totals.value == 149.0


async def test_set_quantity_remove_clear(store):
    store.products[P1] = product_row(P1)
    store.products[P2] = product_row(P2, price=50.0)
    svc = service(store)
    await do_add(svc, "k-1")
    await do_add(svc, "k-2", add_cmd(P2))
    out = await svc.mutate(
        CONV,
        CartCommand.parse("set_quantity", {"product_id": P1, "quantity": 5}),
        idempotency_key="k-3",
        turn_id="t-1",
    )
    assert out.ok and out.snapshot.lines[0].quantity == 5
    out = await svc.mutate(
        CONV,
        CartCommand.parse("remove", {"product_id": P1}),
        idempotency_key="k-4",
        turn_id="t-1",
    )
    assert out.ok and [ln.product_id for ln in out.snapshot.lines] == [P2]
    out = await svc.mutate(CONV, CartCommand.parse("clear"), idempotency_key="k-5", turn_id="t-1")
    assert out.ok and out.snapshot.is_empty


# ── idempotency + concurență logică ─────────────────────────────────────────────────────────


async def test_retry_same_key_replays_receipt_without_second_mutation(store):
    store.products[P1] = product_row(P1)
    svc = service(store)
    first = await do_add(svc, "k-1", add_cmd(qty=2))
    second = await do_add(svc, "k-1", add_cmd(qty=2))  # retry după response loss
    assert second.ok and second.receipt.replayed
    assert second.receipt.receipt_id == first.receipt.receipt_id
    assert second.snapshot.lines[0].quantity == 2  # NU 4 — zero a doua mutație
    assert second.snapshot.version == 1
    assert len(store.receipts) == 1


async def test_same_action_two_turns_single_increase(store):
    """Failure matrix rândul 1: aceeași acțiune, două tururi care au scăpat de one-shot →
    aceeași cheie (`a:<action_id>`) → un receipt, o singură creștere."""
    store.products[P1] = product_row(P1)
    svc = service(store)
    key = action_idempotency_key("action-123")
    r1, r2 = await asyncio.gather(
        svc.mutate(CONV, add_cmd(), idempotency_key=key, turn_id="t-1", action_id="action-123"),
        svc.mutate(CONV, add_cmd(), idempotency_key=key, turn_id="t-2", action_id="action-123"),
    )
    assert r1.ok and r2.ok
    assert {r1.receipt.replayed, r2.receipt.replayed} == {True, False}
    assert store.items[0]["quantity"] == 1 and len(store.items) == 1
    assert len(store.receipts) == 1


async def test_expected_version_stale_is_conflict_with_fresh_snapshot(store):
    store.products[P1] = product_row(P1)
    svc = service(store)
    await do_add(svc, "k-1")  # version 1
    out = await svc.mutate(
        CONV,
        CartCommand.parse("add", {"product_id": P1, "expected_version": 0}),
        idempotency_key="k-2",
        turn_id="t-1",
    )
    assert out.conflict and not out.ok and out.receipt is None
    assert out.snapshot.version == 1  # snapshotul FRESH, server-owned — fără merge în FE
    assert store.items[0]["quantity"] == 1  # zero mutație
    assert ("cart_version_conflict", {"operation": "add"}) in svc.events


# ── validări înaintea mutației ──────────────────────────────────────────────────────────────


async def test_unknown_product_and_foreign_tenant_look_identical(store):
    out = await do_add(service(store))  # produsul nu există pe tenantul ăsta
    assert out.error == "product_not_found" and out.receipt.status == "failed"
    assert not store.items


async def test_variant_must_belong_to_product(store):
    store.products[P1] = product_row(P1, variants=[{"id": V1, "label": "50ml", "price": 120.0}])
    store.products[P2] = product_row(P2)
    svc = service(store)
    out = await do_add(svc, "k-1", add_cmd(P2, variant=V1))  # varianta e a lui P1, nu P2
    assert out.error == "variant_not_found"
    assert not store.items  # reject ÎNAINTE de mutație


async def test_variant_price_and_stock_win(store):
    store.products[P1] = product_row(
        P1, price=100.0, variants=[{"id": V1, "label": "50ml", "price": 120.0, "stock": 3}]
    )
    out = await do_add(service(store), "k-1", add_cmd(P1, variant=V1, qty=2))
    assert out.ok
    assert out.snapshot.lines[0].unit_price == 120.0
    assert out.snapshot.lines[0].variant_label == "50ml"
    assert out.snapshot.totals.value == 240.0


async def test_safety_gate_blocks_before_write(store):
    store.products[P1] = product_row(P1)

    class Policy:
        def allows(self, product, purpose):
            return False

        def evaluate(self, products, purpose):
            return object()

    out = await do_add(service(store, policy=Policy()))
    assert out.error == "safety_excluded" and out.receipt.status == "failed"
    assert not store.items  # coșul NESCHIMBAT (mutation gate, nu filtru de rezultat)


async def test_out_of_stock_and_unknown_availability_reject(store):
    store.products[P1] = product_row(P1, availability="out_of_stock")
    store.products[P2] = product_row(P2, availability=None)
    svc = service(store)
    assert (await do_add(svc, "k-1", add_cmd(P1))).error == "out_of_stock"
    # provider timeout/stale ⇒ UNKNOWN nu devine „în stoc" (failure matrix)
    assert (await do_add(svc, "k-2", add_cmd(P2))).error == "availability_unknown"
    assert not store.items


async def test_known_stock_bounds_quantity(store):
    store.products[P1] = product_row(P1, stock=1)
    out = await do_add(service(store), "k-1", add_cmd(qty=2))
    assert out.error == "insufficient_stock"
    assert not store.items


async def test_unknown_price_rejected_and_never_zero(store):
    store.products[P1] = product_row(P1, price=None)
    out = await do_add(service(store))
    assert out.error == "price_unknown"  # fără preț inventat, fără „0 lei"


async def test_mixed_currency_rejected(store):
    store.products[P1] = product_row(P1)
    store.products[P2] = product_row(P2, currency="EUR")
    svc = service(store)
    await do_add(svc, "k-1")
    out = await do_add(svc, "k-2", add_cmd(P2))
    assert out.error == "currency_mismatch"  # nu o sumă greșită


async def test_cart_full_rejects_new_line(store):
    pids = [f"{i:08d}-1111-4111-8111-111111111111" for i in range(1, CART_MAX_LINES + 2)]
    for pid in pids:
        store.products[pid] = product_row(pid)
    svc = service(store)
    for i, pid in enumerate(pids[:CART_MAX_LINES]):
        assert (await do_add(svc, f"k-{i}", add_cmd(pid))).ok
    out = await do_add(svc, "k-extra", add_cmd(pids[-1]))
    assert out.error == "cart_full"
    assert len(store.items) == CART_MAX_LINES


async def test_line_quantity_cap(store):
    store.products[P1] = product_row(P1)
    svc = service(store)
    await do_add(svc, "k-1", add_cmd(qty=9))
    out = await do_add(svc, "k-2", add_cmd(qty=2))  # 9 + 2 > 10
    assert out.error == "quantity_invalid"
    assert store.items[0]["quantity"] == 9


async def test_set_quantity_missing_line(store):
    store.products[P1] = product_row(P1)
    out = await service(store).mutate(
        CONV,
        CartCommand.parse("set_quantity", {"product_id": P1, "quantity": 2}),
        idempotency_key="k-1",
        turn_id="t-1",
    )
    assert out.error == "line_not_found"


# ── snapshot: UNKNOWN nu blochează vizibilitatea, dar blochează checkout-ul ─────────────────


async def test_vanished_product_makes_line_unknown_and_blocks_checkout(store):
    store.products[P1] = product_row(P1)
    svc = service(store)
    await do_add(svc)
    del store.products[P1]  # produsul dispare din catalog (arhivat)
    snap = await svc.get_snapshot(CONV)
    assert snap.lines[0].facts_status == "unknown"
    assert snap.totals.status == "unknown"  # fără total parțial prezentat ca total
    assert not snap.checkout_eligible and "price_unknown" in snap.blocked_reasons


# ── checkout ────────────────────────────────────────────────────────────────────────────────


async def test_checkout_from_cart_closes_it_and_is_idempotent(store):
    store.products[P1] = product_row(P1, price=80.0)
    svc = service(store)
    await do_add(svc, "k-1", add_cmd(qty=2))
    out = await svc.create_checkout(
        CONV, idempotency_key="ck-1", turn_id="turn-9", base_url="https://shop.example/co"
    )
    assert out.ok and out.receipt.status == "succeeded"
    assert out.receipt.url == "https://shop.example/co?ref=turn-9"
    assert out.receipt.external_ref == "turn-9"
    assert store.active_cart(BIZ, CONV) is None  # coșul acoperit integral → checked_out
    assert out.snapshot.status == "checked_out"
    assert [ln["price"] for ln in out.lines] == [80.0]
    replay = await svc.create_checkout(
        CONV, idempotency_key="ck-1", turn_id="turn-9", base_url="https://shop.example/co"
    )
    assert replay.ok and replay.receipt.replayed
    assert len(store.checkout_links) == 1  # zero al doilea checkout


async def test_checkout_explicit_line_does_not_consume_uncovered_cart(store):
    store.products[P1] = product_row(P1)
    store.products[P2] = product_row(P2)
    svc = service(store)
    await do_add(svc, "k-1")  # coșul are P1
    out = await svc.create_checkout(
        CONV,
        idempotency_key="ck-1",
        turn_id="turn-9",
        base_url="https://shop.example/co",
        lines=[{"product_id": P2, "variant_id": None, "quantity": 1}],
    )
    assert out.ok
    assert store.active_cart(BIZ, CONV) is not None  # P1 rămâne în coșul activ


async def test_checkout_empty_cart_rejected(store):
    out = await service(store).create_checkout(
        CONV, idempotency_key="ck-1", turn_id="t", base_url="https://shop.example/co"
    )
    assert out.error == "cart_empty" and out.receipt.status == "failed"
    assert not store.checkout_links


async def test_checkout_unknown_price_blocked(store):
    store.products[P1] = product_row(P1, price=None)
    out = await service(store).create_checkout(
        CONV,
        idempotency_key="ck-1",
        turn_id="t",
        base_url="https://shop.example/co",
        lines=[{"product_id": P1, "variant_id": None, "quantity": 1}],
    )
    assert out.error == "price_unknown"
    assert not store.checkout_links  # suma e necesară → checkout blocat, nu inventat


# ── adaptor extern: pending → unknown_reconcile → reconcile (fault injection) ───────────────


class FakeAdapter:
    name = "fake-storefront"

    def __init__(self, *, fail_mode: str | None = None) -> None:
        self.fail_mode = fail_mode
        self.push_calls = 0
        self.lookup_result: Any = None

    async def push_checkout(self, **kw):
        self.push_calls += 1
        if self.fail_mode == "lost":
            raise TimeoutError("răspuns pierdut DUPĂ ce providerul poate a executat")
        if self.fail_mode == "reject":
            from src.commerce.adapters.base import AdapterResult

            return AdapterResult(ok=False, error="provider_rejected")
        from src.commerce.adapters.base import AdapterResult

        return AdapterResult(ok=True, external_ref="ext-1", url="https://store/co/ext-1")

    async def lookup(self, **kw):
        return self.lookup_result


async def _external_checkout(svc, key="ck-1"):
    return await svc.create_checkout(
        CONV, idempotency_key=key, turn_id="turn-9", base_url="https://shop.example/co"
    )


async def test_external_success_finalizes_receipt(store):
    store.products[P1] = product_row(P1)
    adapter = FakeAdapter()
    svc = service(store, adapter=adapter)
    await do_add(svc, "k-1")
    out = await _external_checkout(svc)
    assert out.ok and out.receipt.status == "succeeded"
    assert out.receipt.external_ref == "ext-1"


async def test_response_loss_marks_unknown_reconcile_and_retry_does_not_repush(store):
    store.products[P1] = product_row(P1)
    adapter = FakeAdapter(fail_mode="lost")
    svc = service(store, adapter=adapter)
    await do_add(svc, "k-1")
    out = await _external_checkout(svc)
    assert out.error == "receipt_pending"
    assert out.receipt.status == "unknown_reconcile"
    assert adapter.push_calls == 1
    # Retry orb cu aceeași cheie → REPLAY pe receiptul incert, NU al doilea push.
    retry = await _external_checkout(svc)
    assert retry.error == "receipt_pending" and retry.receipt.replayed
    assert adapter.push_calls == 1


async def test_reconcile_resolves_unknown_via_provider_lookup(store):
    store.products[P1] = product_row(P1)
    adapter = FakeAdapter(fail_mode="lost")
    svc = service(store, adapter=adapter)
    await do_add(svc, "k-1")
    await _external_checkout(svc)
    from src.commerce.adapters.base import AdapterResult

    adapter.lookup_result = AdapterResult(ok=True, external_ref="ext-9", url="https://s/9")
    receipt = await svc.reconcile("ck-1")
    assert receipt.status == "succeeded" and receipt.external_ref == "ext-9"


async def test_reconcile_provider_never_saw_key_means_failed(store):
    store.products[P1] = product_row(P1)
    adapter = FakeAdapter(fail_mode="lost")
    svc = service(store, adapter=adapter)
    await do_add(svc, "k-1")
    await _external_checkout(svc)
    adapter.lookup_result = None  # providerul n-a văzut cheia → operația nu s-a întâmplat
    receipt = await svc.reconcile("ck-1")
    assert receipt.status == "failed" and receipt.result_code == "external_not_found"


# ── aceeași comandă pe ambele căi ───────────────────────────────────────────────────────────


def test_action_and_tool_paths_produce_identical_commands():
    """Kernelul (click) și tool-ul LLM construiesc comanda prin ACELAȘI parse — aceleași
    argumente ⇒ aceeași comandă ⇒ același fingerprint (deci și același receipt la aceeași cheie)."""
    from_click = CartCommand.parse("add", {"product_id": P1, "quantity": 2})
    from_tool = CartCommand.parse("add", {"product_id": P1, "variant_id": None, "quantity": 2})
    assert from_click == from_tool
    assert from_click.fingerprint() == from_tool.fingerprint()
    assert tool_idempotency_key("t-1", from_click) == tool_idempotency_key("t-1", from_tool)


async def test_query_budget_constant_for_ten_lines(store):
    """Bugetul anti-N+1: hidratarea unei mutații pe un coș de 10 linii e UN singur call de
    provider (batch), nu unul per linie."""
    pids = [f"{i:08d}-1111-4111-8111-111111111111" for i in range(1, CART_MAX_LINES + 1)]
    for pid in pids:
        store.products[pid] = product_row(pid)
    svc = service(store)
    for i, pid in enumerate(pids):
        await do_add(svc, f"k-{i}", add_cmd(pid))
    store.hydration_calls = 0
    snap = await svc.get_snapshot(CONV)
    assert len(snap.lines) == CART_MAX_LINES
    assert store.hydration_calls == 1
    store.hydration_calls = 0
    await do_add(svc, "k-last", add_cmd(pids[0]))
    assert store.hydration_calls == 1
