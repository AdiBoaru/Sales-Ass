"""NX-173 (P0) — caruselul (`worker/callback.py`) NU are voie să reexpună produse blocate.

Gaură confirmată de review-ul Codex pe `5c7d0a9`: apăsarea ◀/▶ e un drum de inbound NON-LLM — nu
trece prin pipeline, deci nici prin runner, `safety_compose.enforce` sau vreun gate de tool. Citea
`displayed_products` direct din state și punea lista întreagă în payload-ul `edit_media`.

Repro-ul din review, ca test:
  1. state cu `safety.contexts=["pregnancy"]` ȘI `displayed_products=[unsafe-retinal, ...]` (vechi);
  2. callback `car:nav:0`;
  3. înainte: `edit_media` pleca cu produsul contraindicat în payload.
"""

import pytest

from src.config import get_settings
from src.models import BusinessConfig
from src.worker import callback as cb
from src.worker.callback import handle_callback, parse_nav

UNSAFE = {
    "id": "unsafe-retinal",
    "name": "LumaDerm Renew Ser",
    "price": 149.0,
    "attributes": {"key_ingredients": ["retinal"]},
}
SAFE = {
    "id": "safe-bakuchiol",
    "name": "Ser Bakuchiol Gentle",
    "price": 84.0,
    "attributes": {"key_ingredients": ["bakuchiol"]},
}
BY_ID = {p["id"]: p for p in (UNSAFE, SAFE)}

# ref-urile din state (P8: doar id/nume/preț — FĂRĂ attributes; de-asta e nevoie de hidratare)
REF_UNSAFE = {"product_id": "unsafe-retinal", "name": "LumaDerm Renew Ser", "price": 149.0}
REF_SAFE = {"product_id": "safe-bakuchiol", "name": "Ser Bakuchiol Gentle", "price": 84.0}

BIZ = BusinessConfig(id="biz-1", slug="s", name="n")
EVENT = {
    "data": "car:nav:0",
    "sender_external_id": "chat-1",
    "card_message_id": "card-1",
    "provider_msg_id": "cb-1",
    "channel_kind": "telegram",
}


@pytest.fixture
def enqueued(monkeypatch):
    """Interceptează `enqueue_outbox` → vedem EXACT ce ar pleca spre client."""
    out: list[dict] = []

    async def fake_enqueue(conn, business_id, conv_id, key, payload):
        out.append(payload)
        return "outbox-1"

    async def fake_contact(conn, business_id, kind, ext_id):
        from src.models import Contact

        return Contact(id="c1", business_id=business_id)

    async def fake_events(*a, **k):
        return None

    async def fake_by_ids(conn, business_id, ids, **k):
        return [dict(BY_ID[i]) for i in ids if i in BY_ID]

    monkeypatch.setattr(cb, "enqueue_outbox", fake_enqueue)
    monkeypatch.setattr(cb, "get_or_create_contact", fake_contact)
    monkeypatch.setattr(cb, "insert_events", fake_events)
    monkeypatch.setattr(cb, "get_products_by_ids", fake_by_ids)
    return out


def _conv(state: dict) -> dict:
    return {"id": "conv-1", "state": state}


def _patch_conv(monkeypatch, state: dict) -> None:
    async def fake_conv(conn, business_id, contact_id, channel_id, locale=None):
        return _conv(state)

    monkeypatch.setattr(cb, "get_or_create_conversation", fake_conv)


# --- repro din review ---------------------------------------------------------------------------


async def test_carousel_does_not_expose_blocked_product_from_stale_state(monkeypatch, enqueued):
    """Repro EXACT: safety activ + displayed_products vechi cu retinoid + `car:nav:0`."""
    _patch_conv(
        monkeypatch,
        {"safety": {"contexts": ["pregnancy"]}, "displayed_products": [REF_UNSAFE, REF_SAFE]},
    )
    await handle_callback(object(), BIZ, "chan-1", EVENT)
    assert len(enqueued) == 1
    ids = [p["product_id"] for p in enqueued[0]["products"]]
    assert "unsafe-retinal" not in ids, "produs contraindicat în payload-ul edit_media"
    assert ids == ["safe-bakuchiol"]


async def test_carousel_noop_when_all_blocked(monkeypatch, enqueued):
    """Tot setul blocat → nicio editare (semantica de no-op existentă), nu un card nesigur."""
    _patch_conv(
        monkeypatch, {"safety": {"contexts": ["pregnancy"]}, "displayed_products": [REF_UNSAFE]}
    )
    assert await handle_callback(object(), BIZ, "chan-1", EVENT) is None
    assert enqueued == []


async def test_carousel_unaffected_without_safety_context(monkeypatch, enqueued):
    """Fără context → caruselul merge exact ca înainte (zero query în plus, zero filtrare)."""
    calls = []

    async def spy(conn, business_id, ids, **k):
        calls.append(ids)
        return []

    monkeypatch.setattr(cb, "get_products_by_ids", spy)
    _patch_conv(monkeypatch, {"displayed_products": [REF_UNSAFE, REF_SAFE]})
    await handle_callback(object(), BIZ, "chan-1", EVENT)
    assert [p["product_id"] for p in enqueued[0]["products"]] == [
        "unsafe-retinal",
        "safe-bakuchiol",
    ]
    assert calls == [], "fără context de siguranță nu hidratăm degeaba"


async def test_carousel_safe_products_still_navigate(monkeypatch, enqueued):
    _patch_conv(
        monkeypatch, {"safety": {"contexts": ["pregnancy"]}, "displayed_products": [REF_SAFE]}
    )
    await handle_callback(object(), BIZ, "chan-1", EVENT)
    assert [p["product_id"] for p in enqueued[0]["products"]] == ["safe-bakuchiol"]


async def test_carousel_fails_closed_when_hydration_breaks(monkeypatch, enqueued):
    """DB pică pe un context ACTIV → no-op, NU servim setul nefiltrat."""

    async def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(cb, "get_products_by_ids", boom)
    _patch_conv(
        monkeypatch,
        {"safety": {"contexts": ["pregnancy"]}, "displayed_products": [REF_UNSAFE, REF_SAFE]},
    )
    assert await handle_callback(object(), BIZ, "chan-1", EVENT) is None
    assert enqueued == []


async def test_carousel_index_out_of_range_after_filtering(monkeypatch, enqueued):
    """`car:nav:1` pe un set care după filtrare are 1 produs → no-op (nu index eronat)."""
    _patch_conv(
        monkeypatch,
        {"safety": {"contexts": ["pregnancy"]}, "displayed_products": [REF_SAFE, REF_UNSAFE]},
    )
    assert await handle_callback(object(), BIZ, "chan-1", {**EVENT, "data": "car:nav:1"}) is None
    assert enqueued == []


async def test_kill_switch_off_restores_old_carousel(monkeypatch, enqueued):
    get_settings.cache_clear()
    monkeypatch.setenv("SAFETY_CONTRAINDICATIONS_ENABLED", "false")
    try:
        _patch_conv(
            monkeypatch,
            {"safety": {"contexts": ["pregnancy"]}, "displayed_products": [REF_UNSAFE, REF_SAFE]},
        )
        await handle_callback(object(), BIZ, "chan-1", EVENT)
        assert "unsafe-retinal" in [p["product_id"] for p in enqueued[0]["products"]]
    finally:
        get_settings.cache_clear()


def test_parse_nav_unchanged():
    assert parse_nav("car:nav:2") == 2
    assert parse_nav("altceva") is None
