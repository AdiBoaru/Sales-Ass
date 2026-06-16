"""F2-2 — atribuire comenzi: engine `process_order` + endpoint `/webhook/orders`. ZERO DB/real.

Engine cu query-urile monkeypatch-uite (pe namespace-ul `src.webhook.orders`); endpoint cu
fakeredis + override de secret. Acoperă: atribuire la match pe ref, ne-atribuit fără/cu ref
necunoscut, idempotență (items doar la insert nou), evenimente, și marginea HTTP (secret/JSON).
"""

import hashlib
import hmac
import json

import fakeredis.aioredis
import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from src.redis_bus import STREAM_INBOUND
from src.webhook import orders as om
from src.webhook.app import app, get_orders_secret, redis_dep

SECRET = "ord-secret"


def _order(**over):
    base = {
        "external_id": "ORD-1",
        "status": "paid",
        "total": 165.98,
        "currency": "RON",
        "placed_at": "2026-06-16T10:00:00Z",
        "items": [{"product_id": None, "name": "Crema", "quantity": 2, "unit_price": 82.99}],
    }
    base.update(over)
    return base


def _patch_queries(monkeypatch, *, link=None, inserted=True):
    sink: dict = {}

    async def fake_get_link(conn, business_id, ref_code):
        sink["ref_looked_up"] = ref_code
        return link

    async def fake_upsert(conn, business_id, **kw):
        sink["upsert"] = kw
        return {"id": "order-1", "inserted": inserted}

    async def fake_items(conn, order_id, items):
        sink["items"] = items
        return len(items)

    async def fake_mark(conn, business_id, checkout_link_id, order_id):
        sink["converted"] = (checkout_link_id, order_id)

    async def fake_events(conn, business_id, events, *, conversation_id=None, contact_id=None):
        sink["events"] = [e.type for e in events]
        sink["events_ctx"] = (conversation_id, contact_id)
        sink["events_props"] = [e.properties for e in events]
        return len(events)

    monkeypatch.setattr(om, "get_checkout_link_by_ref", fake_get_link)
    monkeypatch.setattr(om, "upsert_order", fake_upsert)
    monkeypatch.setattr(om, "insert_order_items", fake_items)
    monkeypatch.setattr(om, "mark_checkout_converted", fake_mark)
    monkeypatch.setattr(om, "insert_events", fake_events)
    return sink


# --- engine ------------------------------------------------------------------


async def test_attributed_when_ref_matches(monkeypatch):
    link = {
        "id": "cl-1",
        "contact_id": "c-1",
        "conversation_id": "conv-1",
        "converted_order_id": None,
    }
    sink = _patch_queries(monkeypatch, link=link, inserted=True)

    res = await om.process_order(object(), "b", _order(ref="cl-ref"))

    assert res == {"order_id": "order-1", "attribution": "assisted", "attributed": True}
    assert sink["ref_looked_up"] == "cl-ref"
    assert sink["upsert"]["attribution"] == "assisted"
    assert sink["upsert"]["attributed_checkout_link_id"] == "cl-1"
    assert sink["upsert"]["contact_id"] == "c-1"
    assert sink["converted"] == ("cl-1", "order-1")
    assert sink["items"]  # inserted nou → items scrise
    assert "order_attributed" in sink["events"]
    assert sink["events_ctx"] == ("conv-1", "c-1")
    # fără PII în properties (doar attribution + total)
    attr_props = sink["events_props"][sink["events"].index("order_attributed")]
    assert attr_props == {"attribution": "assisted", "total": 165.98}


async def test_no_ref_is_unattributed(monkeypatch):
    sink = _patch_queries(monkeypatch, link=None, inserted=True)
    res = await om.process_order(object(), "b", _order())  # fără ref
    assert res["attribution"] == "none" and res["attributed"] is False
    assert sink["upsert"]["attributed_checkout_link_id"] is None
    assert "ref_looked_up" not in sink  # fără ref → niciun lookup
    assert "converted" not in sink
    assert sink["events"] == ["order_received"]


async def test_ref_unknown_is_unattributed(monkeypatch):
    sink = _patch_queries(monkeypatch, link=None, inserted=True)
    res = await om.process_order(object(), "b", _order(ref="ghost"))
    assert res["attribution"] == "none"
    assert sink["ref_looked_up"] == "ghost"  # s-a căutat, dar n-a găsit
    assert "converted" not in sink


async def test_idempotent_no_duplicate_items(monkeypatch):
    sink = _patch_queries(monkeypatch, link=None, inserted=False)  # re-livrare
    await om.process_order(object(), "b", _order())
    assert "items" not in sink  # nu re-inserăm liniile


async def test_invalid_order_raises(monkeypatch):
    _patch_queries(monkeypatch)
    with pytest.raises(ValidationError):
        await om.process_order(
            object(), "b", {"status": "paid"}
        )  # lipsă external_id/total/placed_at


# --- endpoint (margine HTTP cu fakeredis) ------------------------------------


def _sign(body: bytes, secret: str = SECRET) -> str:
    """Semnătura pe care o calculează emitentul: sha256=<hmac_sha256(secret, corp brut)>."""
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@pytest.fixture
async def client_and_redis():
    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    app.dependency_overrides[get_orders_secret] = lambda: SECRET
    app.dependency_overrides[redis_dep] = lambda: fake
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac, fake
    app.dependency_overrides.clear()
    await fake.aclose()


async def test_order_endpoint_enqueues(client_and_redis):
    ac, fake = client_and_redis
    body = json.dumps(_order(ref="r")).encode()
    resp = await ac.post(
        "/webhook/orders/biz-1", content=body, headers={"X-Orders-Signature": _sign(body)}
    )
    assert resp.status_code == 200
    assert await fake.xlen(STREAM_INBOUND) == 1
    entries = await fake.xrange(STREAM_INBOUND)
    data = json.loads(entries[0][1]["data"])
    assert data["kind"] == "order" and data["business_id"] == "biz-1"
    assert data["order"]["external_id"] == "ORD-1"


async def test_order_endpoint_utf8_signed_on_raw_bytes(client_and_redis):
    ac, fake = client_and_redis
    # HMAC e pe octeții bruți → diacriticele validează identic, fără re-serializare.
    body = json.dumps(_order(items=[{"name": "Cremă hidratantă", "unit_price": 50.0}])).encode()
    resp = await ac.post(
        "/webhook/orders/biz-1", content=body, headers={"X-Orders-Signature": _sign(body)}
    )
    assert resp.status_code == 200
    assert await fake.xlen(STREAM_INBOUND) == 1


async def test_order_endpoint_missing_signature(client_and_redis):
    ac, fake = client_and_redis
    resp = await ac.post("/webhook/orders/biz-1", content=json.dumps(_order()).encode())
    assert resp.status_code == 403
    assert await fake.xlen(STREAM_INBOUND) == 0


async def test_order_endpoint_wrong_signature(client_and_redis):
    ac, fake = client_and_redis
    # Atacatorul nu cunoaște corpul real: semnează un ALT corp → respins.
    body = json.dumps(_order()).encode()
    sig_of_other = _sign(b'{"external_id":"FAKE"}')
    resp = await ac.post(
        "/webhook/orders/biz-1", content=body, headers={"X-Orders-Signature": sig_of_other}
    )
    assert resp.status_code == 403
    assert await fake.xlen(STREAM_INBOUND) == 0


async def test_order_endpoint_signature_without_prefix(client_and_redis):
    ac, fake = client_and_redis
    body = json.dumps(_order()).encode()
    bare_hex = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()  # fără 'sha256='
    resp = await ac.post(
        "/webhook/orders/biz-1", content=body, headers={"X-Orders-Signature": bare_hex}
    )
    assert resp.status_code == 403
    assert await fake.xlen(STREAM_INBOUND) == 0


async def test_order_endpoint_secret_unconfigured_fail_closed(client_and_redis):
    ac, fake = client_and_redis
    app.dependency_overrides[get_orders_secret] = lambda: ""  # secret negsetat
    body = json.dumps(_order()).encode()
    resp = await ac.post(
        "/webhook/orders/biz-1", content=body, headers={"X-Orders-Signature": _sign(body, "")}
    )
    assert resp.status_code == 403  # fail-closed: gol → respinge
    assert await fake.xlen(STREAM_INBOUND) == 0


async def test_order_endpoint_old_secret_header_rejected(client_and_redis):
    ac, fake = client_and_redis
    # Contractul vechi (X-Orders-Secret cu secretul în clar) nu mai autentifică.
    body = json.dumps(_order()).encode()
    resp = await ac.post("/webhook/orders/biz-1", content=body, headers={"X-Orders-Secret": SECRET})
    assert resp.status_code == 403
    assert await fake.xlen(STREAM_INBOUND) == 0


async def test_order_endpoint_bad_json(client_and_redis):
    ac, fake = client_and_redis
    body = b"{not json"  # JSON rupt DAR semnat corect → 403 nu, 400 (semnătura trece)
    resp = await ac.post(
        "/webhook/orders/biz-1", content=body, headers={"X-Orders-Signature": _sign(body)}
    )
    assert resp.status_code == 400
    assert await fake.xlen(STREAM_INBOUND) == 0
