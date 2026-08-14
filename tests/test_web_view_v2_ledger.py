"""NX-240 — integrarea cu ledgerul: ce se îngheață la commit, ce se proiectează la citire.

Trei întrebări, toate despre GRANIȚE:

  1. **Flagul stins schimbă ceva?** Nu. Rândul se persistă identic, iar proiecția rămâne cea
     derivată din payload-ul v1 (NX-233). Un card nou care „doar adaugă" trebuie să fie
     demonstrabil inert, nu presupus inert.
  2. **Ce se persistă?** Verdictul de grounding, nu planul: o poartă rotită mai târziu nu are voie
     să transforme un răspuns deja livrat într-un eșec.
  3. **Ce se întâmplă când proiecția grounded eșuează?** Cade pe cea veche. P6: un răspuns randat
     cu regulile de ieri bate un 500, iar diferența e observabilă.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

import src.web.turn_events as tev
from src.agent.evidence_bundle import build_evidence_bundle
from src.agent.grounding_guard import (
    GROUNDED_PAYLOAD_KEY,
    answer_to_jsonb,
    ground_answer,
)
from src.config import get_settings
from src.db.queries.web_turns import WebTurnRow
from src.web import turn_service as ts
from tests.nx240_helpers import BUSINESS_ID, CLIENT_TURN_ID, NOW, PID_A, plan, row

SLA = 86_400

V1_PAYLOAD = {
    "content": "Uite serul potrivit.",
    "products": [{"product_id": PID_A, "name": "Ser hidratant LumaDerm", "price": 89.0}],
    "suggestions": [],
}


def _grounded_payload(**ground_kwargs) -> dict:
    bundle = build_evidence_bundle(
        business_id=BUSINESS_ID, locale="ro", rows=[row()], now=NOW, sla_s=SLA
    )
    answer = ground_answer(plan(), bundle, locale="ro", **ground_kwargs)
    assert answer.ok
    # Trecerea prin JSON e parte din test: exact bytes ăștia ajung în `jsonb`.
    return json.loads(json.dumps(answer_to_jsonb(answer)))


def _row(**over) -> WebTurnRow:
    base = dict(
        id="turn-nx240",
        business_id=BUSINESS_ID,
        conversation_id="conv-nx240",
        contact_id="ct1",
        session_ref_hash=ts.session_ref_hash("tok", "web_1"),
        client_turn_id=CLIENT_TURN_ID,
        request_fingerprint="fp",
        schema_version="web-turn.v2",
        status="completed",
        attempt=1,
        lease_owner=None,
        lease_epoch=1,
        lease_expires_at=None,
        deadline_at=None,
        conversation_revision_at_accept=3,
        pipeline_version=ts.RESPONSE_CONTRACT_SYNC_V1,
        response_json=dict(V1_PAYLOAD),
        safe_error_code=None,
        accepted_at=NOW,
        updated_at=NOW,
        completed_at=NOW,
    )
    base.update(over)
    return WebTurnRow(**base)


@pytest.fixture
def projector_on(monkeypatch):
    """Aprinde DOAR poarta de proiecție. Relațiile de boot (contract v2 + creier unic) sunt
    validate în `Settings`; aici testăm comportamentul, nu configurația."""
    settings = get_settings()
    monkeypatch.setattr(settings, "web_view_v2_projector_enabled", True, raising=False)
    monkeypatch.setattr(settings, "web_actions_enabled", False, raising=False)
    return settings


# ── Flagul stins = inert ────────────────────────────────────────────────────────────────────
def test_with_the_flag_off_the_projection_is_byte_identical_to_before(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "web_view_v2_projector_enabled", False, raising=False)
    with_payload = _row(response_json={**V1_PAYLOAD, GROUNDED_PAYLOAD_KEY: _grounded_payload()})
    without = _row()
    assert tev.terminal_view(with_payload, "ro") == tev.terminal_view(without, "ro")


def test_a_row_without_the_grounded_key_uses_the_v1_projection(projector_on):
    """Rândurile scrise înainte de card (sau de turnuri care n-au trecut prin MainBrain) rămân
    randabile exact ca înainte — migrarea e LAZY, nu un backfill."""
    view = tev.terminal_view(_row(), "ro")
    assert view["messages"][0]["blocks"][0]["text"] == "Uite serul potrivit."


# ── Flagul aprins = proiecție grounded ──────────────────────────────────────────────────────
def test_grounded_projection_replaces_the_v1_derived_one(projector_on):
    row_with = _row(response_json={**V1_PAYLOAD, GROUNDED_PAYLOAD_KEY: _grounded_payload()})
    view = tev.terminal_view(row_with, "ro")
    blocks = view["messages"][0]["blocks"]
    # Textul vine din `direct_answer`-ul PLANULUI, nu din `content`-ul v1.
    assert blocks[0]["text"] == "Pentru ten uscat merge serul LumaDerm."
    item = blocks[1]["items"][0]
    assert item["price"] == {"current": "89,00 lei", "previous": "120,00 lei", "discount": "-25%"}
    assert item["reason"].startswith("are acid hialuronic")


def test_projection_is_byte_identical_on_repeat(projector_on):
    row_with = _row(response_json={**V1_PAYLOAD, GROUNDED_PAYLOAD_KEY: _grounded_payload()})
    first = json.dumps(tev.terminal_view(row_with, "ro"), sort_keys=True)
    second = json.dumps(tev.terminal_view(row_with, "ro"), sort_keys=True)
    assert first == second


def test_catalog_changes_after_commit_cannot_change_the_answer(projector_on):
    """Cazul din failure matrix: „retry result după facts schimbate ⇒ replay exact". Faptele sunt
    în rând, nu în catalog — deci nu există cale prin care schimbarea să ajungă la client."""
    frozen = _grounded_payload()
    row_with = _row(response_json={**V1_PAYLOAD, GROUNDED_PAYLOAD_KEY: frozen})
    before = tev.terminal_view(row_with, "ro")
    # „Catalogul" se schimbă: preț nou, nume nou. Rândul rămâne cel scris în tranzacția terminală.
    build_evidence_bundle(
        business_id=BUSINESS_ID,
        locale="ro",
        rows=[row(price=9.99, name="ALTCEVA")],
        now=datetime(2027, 1, 1, tzinfo=UTC),
        sla_s=SLA,
    )
    assert tev.terminal_view(row_with, "ro") == before


def test_a_corrupt_grounded_payload_degrades_to_the_v1_projection(projector_on):
    row_with = _row(response_json={**V1_PAYLOAD, GROUNDED_PAYLOAD_KEY: {"schema_version": 99}})
    view = tev.terminal_view(row_with, "ro")
    assert view["messages"][0]["blocks"][0]["text"] == "Uite serul potrivit."


def test_a_grounded_payload_that_cannot_project_degrades_instead_of_raising(projector_on):
    """Un payload sintactic valid dar imposibil de proiectat (conversație fără id) nu are voie să
    scoată 500 pe un terminal — cade pe proiecția veche."""
    broken = {**_grounded_payload(), "business_id": ""}
    row_with = _row(response_json={**V1_PAYLOAD, GROUNDED_PAYLOAD_KEY: broken})
    assert tev.terminal_view(row_with, "ro")["messages"][0]["blocks"][0]["text"]


@pytest.mark.parametrize("status", ["failed", "cancelled"])
def test_non_completed_terminals_keep_the_v1_projection(projector_on, status):
    """`grounded_v2` se scrie doar în tranzacția de SUCCES (`complete_web_turn`). Un rând eșuat
    n-are cum să-l aibă; dacă totuși l-ar avea, nu-l folosim — statusul e autoritatea."""
    row_with = _row(
        status=status,
        safe_error_code="processing_error",
        response_json={**V1_PAYLOAD, GROUNDED_PAYLOAD_KEY: _grounded_payload()},
    )
    view = tev.terminal_view(row_with, "ro")
    assert view["messages"][0]["blocks"][0]["type"] == "notice"


def test_locale_is_the_conversation_locale_not_the_frozen_one(projector_on):
    """Faptele sunt înghețate; PREZENTAREA lor nu. Același verdict, citit în engleză, se
    formatează în engleză — copy-ul e server-owned, dar nu e bătut în cuie în rând."""
    row_with = _row(response_json={**V1_PAYLOAD, GROUNDED_PAYLOAD_KEY: _grounded_payload()})
    view = tev.terminal_view(row_with, "en")
    assert view["chrome"]["dialog_title"] == "Shopping assistant"
    assert view["messages"][0]["blocks"][1]["items"][0]["price"]["current"] == "89.00 RON"


# ── Faptele de commit ───────────────────────────────────────────────────────────────────────
def test_commit_facts_carry_the_verdict_and_the_sellable_refs():
    """Marginea primește DATE (payload + refs), nu obiecte de domeniu: `src/web` nu importă
    `src/agent`, iar serializarea se face o singură dată, în același loc cu restul faptelor."""
    from src.worker.processor import _grounded_facts

    bundle = build_evidence_bundle(
        business_id=BUSINESS_ID, locale="ro", rows=[row()], now=NOW, sla_s=SLA
    )

    class _Ctx:
        grounded = ground_answer(plan(), bundle, locale="ro", commerce_enabled=True)

    facts = _grounded_facts(_Ctx())
    assert facts["commerce_product_refs"] == (PID_A,)
    assert facts["cart_checkout_ready"] is False  # niciun coș în turul ăsta
    assert facts["grounded"]["schema_version"] == 1


def test_a_rejected_verdict_is_not_persisted_at_all():
    """Dacă guardul a respins, nu există verdict de înghețat: turul cade pe fallback-ul
    determinist, iar rândul arată exact ca înainte de card."""
    from src.worker.processor import _grounded_facts

    class _Ctx:
        grounded = None

    assert _grounded_facts(_Ctx()) == {}


def test_commerce_refs_are_empty_when_the_cart_service_is_off():
    from src.worker.processor import _grounded_facts

    bundle = build_evidence_bundle(
        business_id=BUSINESS_ID, locale="ro", rows=[row()], now=NOW, sla_s=SLA
    )

    class _Ctx:
        grounded = ground_answer(plan(), bundle, locale="ro", commerce_enabled=False)

    assert _grounded_facts(_Ctx())["commerce_product_refs"] == ()
