"""NX-237 — cele DOUĂ căi de intrare în CartService: tool-ul LLM (`run_tool`) și kernelul de
acțiuni (`dispatch`). Aceeași comandă, același serviciu, același receipt. ZERO DB/LLM real.

Persistența = store-ul in-memory partajat (`tests/fake_commerce.py`) — același contract ca
testele de serviciu.
"""

from __future__ import annotations

import pytest

from src.agent.action_kernel import dispatch
from src.config import get_settings
from src.models import BusinessConfig, Contact, ConversationState, InboundMessage, TurnContext
from src.tools.base import run_tool
from src.web.action_models import ActionArgs, ActionCommand
from src.worker.runner import PipelineDeps
from tests.fake_commerce import Store, install, product_row

BIZ = "biz-1"
P1 = "11111111-1111-4111-8111-111111111111"
P2 = "22222222-2222-4222-8222-222222222222"


@pytest.fixture()
def store(monkeypatch) -> Store:
    st = Store()
    install(st, monkeypatch.setattr)
    return st


def _ctx() -> TurnContext:
    return TurnContext(
        turn_id="turn-1",
        business=BusinessConfig(id=BIZ, slug="s", name="n"),
        contact=Contact(id="contact-1", business_id=BIZ),
        message=InboundMessage(provider_msg_id="m", body="x"),
        conversation_id="conv-1",
        state=ConversationState(),
    )


def _deps() -> PipelineDeps:
    return PipelineDeps(conn=object(), redis=None, llm=None)


@pytest.fixture()
def cart_on(monkeypatch):
    monkeypatch.setattr(get_settings(), "conversation_cart_enabled", True)
    monkeypatch.setattr(get_settings(), "web_actions_enabled", True, raising=False)


def _event(ctx, type_):
    return next((e for e in reversed(ctx.events) if e.type == type_), None)


# ── calea LLM (run_tool) ────────────────────────────────────────────────────────────────────


async def test_cart_add_flag_off_is_legacy_untouched(store, monkeypatch):
    """Flag OFF (default) → NIMIC din calea canonică: `state_patch['cart']` clasic."""
    import src.tools.commerce_tools as ctools

    async def fake_by_ids(conn, business_id, ids, **k):
        return [{"id": P1, "name": "Ser", "price": 10.0, "availability": "in_stock"}]

    monkeypatch.setattr(ctools, "get_products_by_ids", fake_by_ids)
    ctx = _ctx()
    res = await run_tool(ctx, _deps(), "cart_add", {"product_id": P1, "quantity": 2})
    assert res.ok and "cart" in res.state_patch and "cart_ref" not in res.state_patch
    assert not store.carts  # serviciul canonic NU a fost atins


async def test_cart_add_flag_on_goes_through_service(store, cart_on):
    store.products[P1] = product_row(P1, price=89.0)
    ctx = _ctx()
    res = await run_tool(ctx, _deps(), "cart_add", {"product_id": P1, "quantity": 2})
    assert res.ok
    # Starea primește DOAR referința (P8) — zero linii, zero preț copiat.
    assert set(res.state_patch) == {"cart_ref"}
    assert res.state_patch["cart_ref"]["version"] == 1
    assert res.cart_snapshot is not None and res.cart_snapshot.lines[0].quantity == 2
    assert res.prices == [178.0]  # totalul grounded (validator)
    assert res.products and res.products[0]["id"] == P1
    assert "Coș actualizat" in res.llm_view
    ev = _event(ctx, "cart_updated")
    assert ev and ev.properties["lines"] == 1 and ev.properties["product_ids"] == [P1]
    assert store.receipts and store.receipts[0]["status"] == "succeeded"


async def test_cart_add_flag_on_rejects_over_canonical_quantity(store, cart_on):
    store.products[P1] = product_row(P1)
    res = await run_tool(_ctx(), _deps(), "cart_add", {"product_id": P1, "quantity": 40})
    assert not res.ok and res.error == "quantity_invalid"
    assert not store.items


async def test_cart_add_flag_on_same_turn_retry_single_increase(store, cart_on):
    """Re-rularea ACELUIAȘI tur (crash/retry executor) → aceeași cheie → o singură creștere."""
    store.products[P1] = product_row(P1)
    ctx = _ctx()
    await run_tool(ctx, _deps(), "cart_add", {"product_id": P1, "quantity": 1})
    await run_tool(ctx, _deps(), "cart_add", {"product_id": P1, "quantity": 1})
    assert store.items[0]["quantity"] == 1 and len(store.receipts) == 1


async def test_checkout_link_flag_on_creates_link_with_receipt(store, cart_on, monkeypatch):
    monkeypatch.setattr(get_settings(), "checkout_base_url", "https://shop.example/co")
    store.products[P1] = product_row(P1, price=50.0)
    ctx = _ctx()
    res = await run_tool(
        ctx,
        _deps(),
        "checkout_link",
        {"cart_items": [{"product_id": P1, "variant_id": None, "quantity": 2}]},
    )
    assert res.ok
    assert res.links == ["https://shop.example/co?ref=turn-1"]
    assert res.prices == [100.0]
    assert store.checkout_links and store.checkout_links[0]["ref_code"] == "turn-1"
    assert any(r["operation"] == "checkout" and r["status"] == "succeeded" for r in store.receipts)
    ev = _event(ctx, "checkout_link_created")
    assert ev and ev.properties["items"] == 1 and ev.properties["value"] == 100.0


async def test_checkout_link_flag_on_no_url_stays_honest(store, cart_on, monkeypatch):
    monkeypatch.setattr(get_settings(), "checkout_base_url", "")
    res = await run_tool(
        _ctx(),
        _deps(),
        "checkout_link",
        {"cart_items": [{"product_id": P1, "variant_id": None, "quantity": 1}]},
    )
    assert not res.ok and res.error == "no_checkout_url"
    assert not store.checkout_links and not store.receipts


async def test_checkout_link_flag_on_vanished_products(store, cart_on, monkeypatch):
    monkeypatch.setattr(get_settings(), "checkout_base_url", "https://shop.example/co")
    res = await run_tool(
        _ctx(),
        _deps(),
        "checkout_link",
        {"cart_items": [{"product_id": P1, "variant_id": None, "quantity": 1}]},
    )
    assert not res.ok and res.error == "no_valid_products"  # paritate cu mesajul legacy


# ── calea de acțiuni (kernel) ───────────────────────────────────────────────────────────────


def _command(kind: str, action_id: str | None = None, **args) -> ActionCommand:
    return ActionCommand(
        # id distinct per acțiune LOGICĂ (în producție `derive_action_id` face exact asta);
        # două comenzi cu ACELAȘI id sunt, prin contract, aceeași apăsare → replay.
        action_id=action_id or f"act:{kind}:{sorted(args.items())!r}",
        kind=kind,
        args=ActionArgs(**args),
        policy="one_shot",
        source_turn_id="src-turn",
        source_revision=0,
        conversation_id="conv-1",
    )


async def test_kernel_cart_add_line_handled_with_receipt(store, cart_on):
    store.products[P1] = product_row(P1, price=89.0)
    ctx = _ctx()
    outcome = await dispatch(ctx, _deps(), _command("cart_add_line", product_ref=P1, quantity=2))
    assert type(outcome).__name__ == "Handled"
    assert ctx.reply is not None and "coșul conversației" in ctx.reply.text
    assert "178,00 lei" in ctx.reply.text  # totalul display, server-owned (2 × 89)
    assert ctx.state_patch["cart_ref"]["version"] == 1
    assert store.receipts[0]["action_id"].startswith("act:cart_add_line")
    assert store.receipts[0]["idempotency_key"].startswith("a:act:cart_add_line")


async def test_kernel_cart_disabled_refuses_honestly(store, monkeypatch):
    monkeypatch.setattr(get_settings(), "conversation_cart_enabled", False)
    ctx = _ctx()
    outcome = await dispatch(ctx, _deps(), _command("cart_add_line", product_ref=P1))
    assert type(outcome).__name__ == "Rejected" and outcome.code == "action_unavailable"
    assert ctx.reply is not None  # refuz VIZIBIL (P6), nu ecran gol
    assert not store.items


async def test_kernel_and_tool_share_receipt_per_action(store, cart_on):
    """Aceeași acțiune consumată de două ori (race pe one-shot scăpat) → un receipt, o creștere."""
    store.products[P1] = product_row(P1)
    await dispatch(_ctx(), _deps(), _command("cart_add_line", action_id="same", product_ref=P1))
    await dispatch(_ctx(), _deps(), _command("cart_add_line", action_id="same", product_ref=P1))
    assert store.items[0]["quantity"] == 1 and len(store.receipts) == 1


async def test_kernel_cart_remove_and_clear(store, cart_on):
    store.products[P1] = product_row(P1)
    store.products[P2] = product_row(P2)
    ctx = _ctx()
    await dispatch(ctx, _deps(), _command("cart_add_line", product_ref=P1))
    await dispatch(ctx, _deps(), _command("cart_add_line", product_ref=P2))
    out = await dispatch(_ctx(), _deps(), _command("cart_remove", product_ref=P1))
    assert type(out).__name__ == "Handled"
    assert [it["product_id"] for it in store.items] == [P2]
    ctx2 = _ctx()
    out = await dispatch(ctx2, _deps(), _command("cart_clear"))
    assert type(out).__name__ == "Handled" and not store.items
    assert ctx2.reply is not None and "golit" in ctx2.reply.text


async def test_kernel_checkout_serves_link(store, cart_on, monkeypatch):
    monkeypatch.setattr(get_settings(), "checkout_base_url", "https://shop.example/co")
    store.products[P1] = product_row(P1)
    await dispatch(_ctx(), _deps(), _command("cart_add_line", product_ref=P1))
    ctx = _ctx()
    outcome = await dispatch(ctx, _deps(), _command("checkout"))
    assert type(outcome).__name__ == "Handled"
    assert "https://shop.example/co?ref=turn-1" in ctx.reply.text
    assert store.active_cart(BIZ, "conv-1") is None  # coșul acoperit → checked_out


async def test_kernel_checkout_empty_cart_refused(store, cart_on, monkeypatch):
    monkeypatch.setattr(get_settings(), "checkout_base_url", "https://shop.example/co")
    ctx = _ctx()
    outcome = await dispatch(ctx, _deps(), _command("checkout"))
    assert type(outcome).__name__ == "Rejected"
    assert "gol" in ctx.reply.text  # copy onest, nu o promisiune
