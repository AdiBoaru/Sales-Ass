"""NX-162 (Funnel Truth) — checkout click/convert events + split atribuire. ZERO DB/LLM real.

Trei zone:
- `checkout_link_converted` prin `process_order` (query-uri monkeypatch-uite pe `webhook.orders`),
  gated pe `inserted` (redelivery-ul nu dublează);
- endpoint `/r/{business_id}/{ref_code}` prin AsyncClient (tenant_conn + query-uri + insert_events
  monkeypatch-uite pe `src.webhook.redirect`): happy, idempotent la dublu-click, not-found,
  business_id invalid;
- guard structural pe `_ROLLUP_SQL` (split direct_bot/assisted separat, nu însumat — SQL-ul real
  agregat e testat cu DB `@integration`, ca restul rollup-ului).

Invariante: fără PII în properties (doar ref_code/id/enum/counts); idempotency pe click.
"""

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from src.db.queries.usage import _ROLLUP_SQL
from src.webhook import orders as om
from src.webhook import redirect as rd
from src.webhook.app import app

# --- checkout_link_converted (engine process_order) --------------------------

_LINK = {"id": "cl-1", "contact_id": "c-1", "conversation_id": "conv-1", "converted_order_id": None}


def _order(**over):
    base = {
        "external_id": "ORD-1",
        "status": "paid",
        "total": 165.98,
        "placed_at": "2026-06-16T10:00:00Z",
        "items": [],
    }
    base.update(over)
    return base


def _patch_orders(monkeypatch, *, link=None, inserted=True):
    sink: dict = {}

    async def fake_get_link(conn, business_id, ref_code):
        return link

    async def fake_upsert(conn, business_id, **kw):
        return {"id": "order-1", "inserted": inserted}

    async def fake_items(conn, order_id, items):
        return len(items)

    async def fake_mark(conn, business_id, checkout_link_id, order_id):
        sink["converted_marked"] = (checkout_link_id, order_id)

    async def fake_events(conn, business_id, events, *, conversation_id=None, contact_id=None):
        sink["events"] = [e.type for e in events]
        sink["props"] = {e.type: e.properties for e in events}
        return len(events)

    monkeypatch.setattr(om, "get_checkout_link_by_ref", fake_get_link)
    monkeypatch.setattr(om, "upsert_order", fake_upsert)
    monkeypatch.setattr(om, "insert_order_items", fake_items)
    monkeypatch.setattr(om, "mark_checkout_converted", fake_mark)
    monkeypatch.setattr(om, "insert_events", fake_events)
    return sink


async def test_converted_event_on_attributed_insert(monkeypatch):
    sink = _patch_orders(monkeypatch, link=_LINK, inserted=True)
    await om.process_order(object(), "b", _order(ref="cl-ref"))
    assert "checkout_link_converted" in sink["events"]
    props = sink["props"]["checkout_link_converted"]
    assert props == {"ref_code": "cl-ref", "order_id": "order-1", "attribution": "assisted"}
    # fără PII: doar ref_code (uuid), order_id (uuid), attribution (enum)
    assert set(props) == {"ref_code", "order_id", "attribution"}


async def test_no_converted_on_redelivery(monkeypatch):
    """Re-livrarea aceleiași comenzi (inserted=False) NU re-emite conversia (idempotent)."""
    sink = _patch_orders(monkeypatch, link=_LINK, inserted=False)
    await om.process_order(object(), "b", _order(ref="cl-ref"))
    assert "checkout_link_converted" not in sink["events"]


async def test_no_converted_without_ref(monkeypatch):
    """Comandă fără link atribuit → fără eveniment de conversie."""
    sink = _patch_orders(monkeypatch, link=None, inserted=True)
    await om.process_order(object(), "b", _order())
    assert "checkout_link_converted" not in sink["events"]


# --- endpoint /r/{business_id}/{ref_code} ------------------------------------

_BIZ = "6098812a-50fc-44bd-a1ba-bc77e6399158"  # uuid valid
_TARGET = {
    "id": "cl-1",
    "url": "https://shop.example.ro/checkout?ref=abc",
    "conversation_id": "conv-1",
    "contact_id": "c-1",
}


def _patch_redirect(monkeypatch, *, target, first_click_id, base_url=""):
    sink: dict = {}

    @asynccontextmanager
    async def fake_tenant_conn(business_id):
        sink["tenant_opened"] = business_id
        yield object()

    async def fake_get(conn, business_id, ref_code):
        sink["get"] = (business_id, ref_code)
        return target

    async def fake_stamp(conn, business_id, ref_code):
        sink["stamp"] = (business_id, ref_code)
        return first_click_id

    async def fake_events(conn, business_id, events, *, conversation_id=None, contact_id=None):
        sink["events"] = [e.type for e in events]
        sink["props"] = {e.type: e.properties for e in events}
        sink["events_ctx"] = (conversation_id, contact_id)
        return len(events)

    monkeypatch.setattr(rd, "tenant_conn", fake_tenant_conn)
    monkeypatch.setattr(rd, "get_checkout_redirect", fake_get)
    monkeypatch.setattr(rd, "stamp_checkout_clicked", fake_stamp)
    monkeypatch.setattr(rd, "insert_events", fake_events)
    monkeypatch.setattr(rd, "get_settings", lambda: SimpleNamespace(checkout_base_url=base_url))
    return sink


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def test_redirect_first_click_stamps_emits_and_302(monkeypatch, client):
    sink = _patch_redirect(monkeypatch, target=_TARGET, first_click_id="cl-1")
    resp = await client.get(f"/r/{_BIZ}/refabc")
    assert resp.status_code == 302
    assert resp.headers["location"] == _TARGET["url"]
    assert sink["events"] == ["checkout_link_clicked"]
    assert sink["props"]["checkout_link_clicked"] == {
        "ref_code": "refabc",
        "checkout_link_id": "cl-1",
    }
    assert sink["events_ctx"] == ("conv-1", "c-1")  # drilldown-abil, fără PII


async def test_redirect_double_click_idempotent_no_second_event(monkeypatch, client):
    """Al doilea click (clicked_at deja setat → stamp întoarce None) → 302, dar FĂRĂ event nou."""
    sink = _patch_redirect(monkeypatch, target=_TARGET, first_click_id=None)
    resp = await client.get(f"/r/{_BIZ}/refabc")
    assert resp.status_code == 302
    assert resp.headers["location"] == _TARGET["url"]
    assert "events" not in sink  # niciun al doilea checkout_link_clicked


async def test_redirect_not_found_404_no_leak(monkeypatch, client):
    """ref_code inexistent/expirat (target None) + fără store base → 404 neutru, zero event."""
    sink = _patch_redirect(monkeypatch, target=None, first_click_id=None, base_url="")
    resp = await client.get(f"/r/{_BIZ}/ghost")
    assert resp.status_code == 404
    assert "events" not in sink


async def test_redirect_not_found_falls_back_to_store_base(monkeypatch, client):
    """Cu store base configurat → 302 la fallback safe (nu 404), tot fără event."""
    sink = _patch_redirect(
        monkeypatch, target=None, first_click_id=None, base_url="https://shop.example.ro"
    )
    resp = await client.get(f"/r/{_BIZ}/ghost")
    assert resp.status_code == 302
    assert resp.headers["location"] == "https://shop.example.ro"
    assert "events" not in sink


async def test_redirect_invalid_business_id_no_db_open(monkeypatch, client):
    """business_id ne-uuid → fallback FĂRĂ să deschidă tenant_conn (nu crapă set_config)."""
    sink = _patch_redirect(monkeypatch, target=_TARGET, first_click_id="cl-1", base_url="")
    resp = await client.get("/r/not-a-uuid/refabc")
    assert resp.status_code == 404
    assert "tenant_opened" not in sink  # nu s-a deschis conexiune tenant
    assert "get" not in sink


# --- rollup split (guard structural; SQL real agregat = integration cu DB) ----


def test_rollup_sql_splits_attribution_not_summed():
    """Rollup-ul are cele 4 coloane de split, derivate cu FILTER separat pe attribution —
    direct_bot și assisted NU sunt colapsate într-un singur total."""
    for col in (
        "orders_direct_bot",
        "revenue_direct_bot",
        "orders_assisted",
        "revenue_assisted",
    ):
        assert col in _ROLLUP_SQL
    assert "filter (where attribution = 'direct_bot')" in _ROLLUP_SQL
    assert "filter (where attribution = 'assisted')" in _ROLLUP_SQL
