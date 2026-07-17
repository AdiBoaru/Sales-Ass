"""NX-173 (P0) — proactivul NU are voie să promoveze un produs contraindicat.

Forma cea mai gravă a bug-ului, fiindcă e NESOLICITATĂ și în afara conversației:
  1. clienta se abonează la back-in-stock pentru un ser cu retinol (legitim — încă n-a spus nimic);
  2. peste câteva zile declară sarcina într-o conversație → `state.safety` = pregnancy;
  3. produsul revine pe stoc → jobul vechi trimite pe WhatsApp „serul cu retinal e din nou pe stoc!"

Abonarea e gate-uită la CREARE (`commerce_tools`), dar joburile deja existente nu știu asta —
deci poarta trebuie să fie și la TRIMITERE. Tranzacționalele (`awb_update`, `follow_up`) NU se
gate-uiesc: comanda e deja plasată, nu recomandăm nimic.
"""

import pytest

from src.models import Contact
from src.proactive import scheduler as sch
from src.proactive.scheduler import _process_job, _safety_allows_job

UNSAFE = {
    "id": "unsafe-retinal",
    "name": "LumaDerm Renew Ser",
    "attributes": {"key_ingredients": ["retinal"]},
}
SAFE = {
    "id": "safe-bakuchiol",
    "name": "Ser Bakuchiol",
    "attributes": {"key_ingredients": ["bakuchiol"]},
}
BY_ID = {p["id"]: p for p in (UNSAFE, SAFE)}

PREG_STATE = {"safety": {"contexts": ["pregnancy"], "source": "declared_by_contact"}}


def _route(state=None):
    return {
        "id": "conv-1",
        "channel_id": "chan-1",
        "locale": "ro",
        "channel_kind": "whatsapp",
        "state": state or {},
    }


def _job(kind="back_in_stock", product_id="unsafe-retinal"):
    return {
        "id": "job-1",
        "kind": kind,
        "contact_id": "c1",
        "conversation_id": "conv-1",
        "payload": {"product_id": product_id},
        "template_id": None,
    }


@pytest.fixture(autouse=True)
def _patch(monkeypatch):
    async def by_ids(conn, business_id, ids, **k):
        return [dict(BY_ID[i]) for i in ids if i in BY_ID]

    monkeypatch.setattr(sch, "get_products_by_ids", by_ids)


# --- poarta pură --------------------------------------------------------------------------------


async def test_back_in_stock_blocked_for_pregnant_contact():
    """Scenariul complet: abonare veche + sarcină declarată între timp → jobul NU trece."""
    assert await _safety_allows_job(object(), "biz-1", _job(), _route(PREG_STATE)) is False


async def test_back_in_stock_allowed_for_safe_product():
    job = _job(product_id="safe-bakuchiol")
    assert await _safety_allows_job(object(), "biz-1", job, _route(PREG_STATE)) is True


async def test_back_in_stock_allowed_without_safety_context():
    """Fără context → retinoidul e un produs legitim, notificarea pleacă normal."""
    assert await _safety_allows_job(object(), "biz-1", _job(), _route()) is True


@pytest.mark.parametrize("kind", ["awb_update", "follow_up"])
async def test_transactional_kinds_are_not_gated(kind):
    """Coletul e pe drum / follow-up de proces: nu recomandăm nimic → nu blocăm (ar fi ostil)."""
    job = {**_job(kind=kind), "payload": {"awb": "123", "body": "x"}}
    assert await _safety_allows_job(object(), "biz-1", job, _route(PREG_STATE)) is True


async def test_abandoned_cart_blocked_when_cart_has_contraindicated(monkeypatch):
    async def checkout(conn, business_id, conv_id):
        return {"id": "co-1", "url": "u", "cart": [{"product_id": "unsafe-retinal"}]}

    monkeypatch.setattr(sch, "get_latest_checkout", checkout)
    job = {**_job(kind="abandoned_cart"), "payload": {}}
    assert await _safety_allows_job(object(), "biz-1", job, _route(PREG_STATE)) is False


async def test_abandoned_cart_allowed_when_cart_is_safe(monkeypatch):
    async def checkout(conn, business_id, conv_id):
        return {"id": "co-1", "url": "u", "cart": [{"product_id": "safe-bakuchiol"}]}

    monkeypatch.setattr(sch, "get_latest_checkout", checkout)
    job = {**_job(kind="abandoned_cart"), "payload": {}}
    assert await _safety_allows_job(object(), "biz-1", job, _route(PREG_STATE)) is True


async def test_fails_closed_when_hydration_breaks(monkeypatch):
    """Un mesaj proactiv n-are urgență: a nu trimite e gratis, a trimite greșit nu."""

    async def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(sch, "get_products_by_ids", boom)
    assert await _safety_allows_job(object(), "biz-1", _job(), _route(PREG_STATE)) is False


# --- integrare în `_process_job`: jobul se ANULEAZĂ, nimic nu intră în outbox -------------------


@pytest.fixture
def wired(monkeypatch):
    """Suficient stub cât `_process_job` să ajungă la poartă; capturăm outbox + mark."""
    out: list = []
    marks: list = []

    async def fake_route(conn, business_id, conv_id):
        return _route(PREG_STATE)

    async def fake_contact(conn, business_id, contact_id):
        return Contact(id="c1", business_id=business_id)

    async def fake_to(conn, business_id, contact_id, kind):
        return "+40700000000"

    async def fake_mark(conn, business_id, job_id, status):
        marks.append(status)

    async def fake_enqueue(*a, **k):
        out.append(a)
        return "outbox-1"

    async def fake_build(*a, **k):
        raise AssertionError("build NU trebuie chemat pentru un job blocat de siguranță")

    monkeypatch.setattr(sch, "get_proactive_route", fake_route)
    monkeypatch.setattr(sch, "get_contact_by_id", fake_contact)
    monkeypatch.setattr(sch, "get_recipient_external_id", fake_to)
    monkeypatch.setattr(sch, "mark_job", fake_mark)
    monkeypatch.setattr(sch, "enqueue_outbox", fake_enqueue)
    monkeypatch.setattr(sch, "build_message_spec", fake_build)
    return out, marks


async def test_process_job_cancels_and_sends_nothing(wired):
    out, marks = wired
    events: list = []
    await _process_job(object(), "biz-1", _job(), events)
    assert out == [], "nimic în outbox — mesajul proactiv NU pleacă"
    assert marks == ["cancelled"]
    assert events[0].properties["reason"] == "safety_excluded"
