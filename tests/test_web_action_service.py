"""NX-236 — autorizarea: dovada de emitere, legăturile de tenant/sesiune/conversație, one-shot.

Semnătura nu e autorizare. Fișierul verifică exact asta: un token perfect valid criptografic e
refuzat dacă turul-sursă nu l-a emis, dacă îl prezintă altă sesiune sau alt tenant, dacă sursa a
dispărut, sau dacă butonul a fost deja folosit de alt turn.

Fără Postgres: `authorize_action` primește un provider fals care întoarce rânduri de ledger.
Comportamentul pe DB REALĂ (concurență, unicii) e în `test_web_action_replay_db.py`.
"""

from __future__ import annotations

import base64
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from src.db.queries.web_turns import WebTurnRow
from src.web import action_service as svc
from src.web.action_crypto import parse_key_ring, seal
from src.web.action_models import ActionArgs, ActionPlan, TurnFacts, plan_actions
from src.web.turn_service import session_ref_hash

NOW = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)
BIZ = "biz-1"
TOKEN = "public-token"
VISITOR = "visitor-1"
PID_A = "11111111-1111-4111-8111-111111111111"
PID_B = "22222222-2222-4222-8222-222222222222"
SECRET = "fingerprint-secret"


def _ring(key_id: str = "k1", seed: bytes = b"\x01"):
    return parse_key_ring(f"{key_id}:{base64.b64encode(seed * 32).decode()}")


KEY_ONE = bytes([1]) * 32
KEY_TWO = bytes([2]) * 32


def _rotated_ring():
    """Inel după rotație: cheia NOUĂ în față, cea veche păstrată (fereastra de overlap)."""
    new_key = base64.b64encode(KEY_TWO).decode()
    old_key = base64.b64encode(KEY_ONE).decode()
    return parse_key_ring(f"k2:{new_key},k1:{old_key}")


def _row(**over) -> WebTurnRow:
    base = dict(
        id=str(uuid4()),
        business_id=BIZ,
        conversation_id="conv-1",
        contact_id="ct1",
        session_ref_hash=session_ref_hash(TOKEN, VISITOR),
        client_turn_id=str(uuid4()),
        request_fingerprint="fp",
        schema_version="web-turn.v2",
        status="completed",
        attempt=1,
        lease_owner=None,
        lease_epoch=1,
        lease_expires_at=None,
        deadline_at=None,
        conversation_revision_at_accept=4,
        pipeline_version="web-chat.v1",
        response_json={"content": "ok", "products": [], "suggestions": []},
        safe_error_code=None,
        accepted_at=NOW,
        updated_at=NOW,
        completed_at=NOW,
    )
    base.update(over)
    return WebTurnRow(**base)


def _source_with_actions(plans: tuple[ActionPlan, ...] | None = None, **over) -> WebTurnRow:
    plans = plans or (ActionPlan("request_details", ActionArgs(product_ref=PID_A)),)
    view = svc.merge_actions_into_view(
        {"content": "ok", "products": [{"product_id": PID_A}], "suggestions": []}, plans
    )
    return _row(response_json=view, **over)


class _Db:
    """Provider fals: întoarce un rând-sursă și, opțional, un consumator deja înregistrat."""

    def __init__(self, source: WebTurnRow | None, consumer: WebTurnRow | None = None) -> None:
        self.source = source
        self.consumer = consumer
        self.operations: list[str] = []

    def __call__(self, operation: str = "?"):
        self.operations.append(operation)
        db = self

        @asynccontextmanager
        async def _cm():
            yield db

        return _cm()

    # Seam-urile pe care le folosește `authorize_action` (monkeypatch-uite prin modul).


def _provider(source: WebTurnRow | None, consumer: WebTurnRow | None = None, monkeypatch=None):
    db = _Db(source, consumer)

    async def _get_turn_by_id(conn, business_id, turn_id):
        if source is None or business_id != source.business_id or turn_id != source.id:
            return None
        return source

    async def _find(conn, business_id, conversation_id, fingerprint):
        return consumer

    monkeypatch.setattr(svc, "get_turn_by_id", _get_turn_by_id)
    monkeypatch.setattr(svc, "find_turn_by_fingerprint", _find)
    return db


async def _authorize(db, token: str, *, client_turn_id: str = "ct-new", ring=None, now=None):
    return await svc.authorize_action(
        db,
        token=token,
        business_id=BIZ,
        channel_token=TOKEN,
        visitor_id=VISITOR,
        client_turn_id=client_turn_id,
        ring=ring or _ring(),
        fingerprint_secret=SECRET,
        now=now or NOW + timedelta(seconds=30),
    )


def _issue(row: WebTurnRow, ring=None, ttl_s: int = 1800):
    return svc.issue_actions(row, svc.plans_from_row(row), ring=ring or _ring(), ttl_s=ttl_s)


# ── Emitere ─────────────────────────────────────────────────────────────────────────────────
def test_issue_is_deterministic_for_the_same_row():
    row = _source_with_actions()
    assert [a.token for a in _issue(row)] == [a.token for a in _issue(row)]


def test_issue_anchors_expiry_in_completed_at_not_in_wall_clock():
    row = _source_with_actions()
    issued = _issue(row, ttl_s=600)[0]
    assert issued.expires_at == int(row.completed_at.timestamp()) + 600


def test_non_terminal_rows_emit_nothing():
    row = _source_with_actions(status="running", completed_at=None)
    assert _issue(row) == ()


def test_unavailable_kinds_are_never_issued():
    """`refine_search` are `available=False` (nu există rafinare deterministă server-side): un
    plan care îl conține nu produce niciun token, oricât de valid ar fi restul rândului."""
    row = _source_with_actions((ActionPlan("refine_search", ActionArgs(filter="cheaper")),))
    assert _issue(row) == ()


def test_mutating_kinds_outside_the_nx240_set_are_never_issued():
    """NX-240 a deschis emiterea DOAR pentru `cart_add_line`/`checkout`. `cart_remove` are handler
    la consum, dar niciun loc în ViewModel din care să pornească — deci nu poate fi sigilat."""
    row = _source_with_actions((ActionPlan("cart_remove", ActionArgs(product_ref=PID_A)),))
    assert _issue(row) == ()


def test_commerce_cta_is_issued_once_planned():
    """Simetric: dacă planul persistat conține un `cart_add_line` (deci faptele au permis-o la
    commit), tokenul EXISTĂ — altfel butonul emis de projector n-ar avea ce purta."""
    row = _source_with_actions((ActionPlan("cart_add_line", ActionArgs(product_ref=PID_A)),))
    issued = _issue(row)
    assert [a.plan.kind for a in issued] == ["cart_add_line"]


def test_rows_without_a_plan_emit_nothing():
    assert _issue(_row()) == ()


# ── Drumul fericit ──────────────────────────────────────────────────────────────────────────
async def test_valid_token_authorizes(monkeypatch):
    source = _source_with_actions()
    db = _provider(source, monkeypatch=monkeypatch)
    issued = _issue(source)[0]
    verdict = await _authorize(db, issued.token)
    assert isinstance(verdict, svc.AuthorizedAction)
    assert verdict.command.kind == "request_details"
    assert verdict.command.args.product_ref == PID_A
    assert verdict.command.source_turn_id == source.id
    assert verdict.key_slot == "current"
    assert verdict.fingerprint == svc.action_fingerprint(
        SECRET, business_id=BIZ, channel_token=TOKEN, action_id=issued.action_id
    )


async def test_authorization_takes_one_short_checkout(monkeypatch):
    source = _source_with_actions()
    db = _provider(source, monkeypatch=monkeypatch)
    await _authorize(db, _issue(source)[0].token)
    assert db.operations == ["web_action_authorize"]


# ── Dovada de emitere ───────────────────────────────────────────────────────────────────────
async def test_token_for_an_action_the_source_never_emitted_is_rejected(monkeypatch):
    """Sigiliu valid, dar planul persistat nu conține acțiunea ⇒ nu a fost emisă de noi."""
    source = _source_with_actions()
    issued = _issue(source)[0]
    # Sursa își pierde planul (ex. rând scris de o cale fără acțiuni).
    stripped = _row(id=source.id, response_json={"content": "ok", "products": []})
    db = _provider(stripped, monkeypatch=monkeypatch)
    verdict = await _authorize(db, issued.token)
    assert isinstance(verdict, svc.ActionRejected)
    assert verdict.code == "action_not_found" and verdict.reason == "not_emitted"


async def test_token_from_another_turn_is_rejected(monkeypatch):
    """Același kind + aceleași argumente, dar emis de ALT turn: `action_id` diferă prin sursă."""
    other = _source_with_actions()
    issued = _issue(other)[0]
    mine = _source_with_actions()  # alt id de turn, același plan
    db = _provider(mine, monkeypatch=monkeypatch)
    verdict = await _authorize(db, issued.token)
    assert isinstance(verdict, svc.ActionRejected)
    assert verdict.code == "action_not_found"


async def test_missing_source_row_is_rejected(monkeypatch):
    source = _source_with_actions()
    issued = _issue(source)[0]
    db = _provider(None, monkeypatch=monkeypatch)
    verdict = await _authorize(db, issued.token)
    assert isinstance(verdict, svc.ActionRejected)
    assert verdict.code == "action_not_found" and verdict.reason == "source_missing"


async def test_non_terminal_source_is_rejected(monkeypatch):
    source = _source_with_actions()
    issued = _issue(source)[0]
    db = _provider(
        _row(id=source.id, status="running", response_json=source.response_json),
        monkeypatch=monkeypatch,
    )
    verdict = await _authorize(db, issued.token)
    assert isinstance(verdict, svc.ActionRejected)
    assert verdict.reason == "source_not_terminal"


# ── Legături ────────────────────────────────────────────────────────────────────────────────
async def test_token_moved_to_another_tenant_is_rejected(monkeypatch):
    source = _source_with_actions()
    issued = _issue(source)[0]
    db = _provider(source, monkeypatch=monkeypatch)
    verdict = await svc.authorize_action(
        db,
        token=issued.token,
        business_id="biz-2",  # alt tenant
        channel_token=TOKEN,
        visitor_id=VISITOR,
        client_turn_id="ct",
        ring=_ring(),
        fingerprint_secret=SECRET,
        now=NOW,
    )
    assert isinstance(verdict, svc.ActionRejected)
    assert verdict.code == "action_not_found" and verdict.reason == "tenant_mismatch"


async def test_token_presented_by_another_visitor_is_rejected(monkeypatch):
    source = _source_with_actions()
    issued = _issue(source)[0]
    db = _provider(source, monkeypatch=monkeypatch)
    verdict = await svc.authorize_action(
        db,
        token=issued.token,
        business_id=BIZ,
        channel_token=TOKEN,
        visitor_id="visitor-2",  # altă sesiune de browser
        client_turn_id="ct",
        ring=_ring(),
        fingerprint_secret=SECRET,
        now=NOW,
    )
    assert isinstance(verdict, svc.ActionRejected)
    assert verdict.reason == "session_mismatch"


async def test_source_row_owned_by_another_session_is_rejected(monkeypatch):
    """Defense-in-depth: chiar dacă pseudonimul ar trece, RÂNDUL trebuie să fie al sesiunii."""
    source = _source_with_actions()
    issued = _issue(source)[0]
    hijacked = _row(id=source.id, response_json=source.response_json, session_ref_hash="altcineva")
    db = _provider(hijacked, monkeypatch=monkeypatch)
    verdict = await _authorize(db, issued.token)
    assert isinstance(verdict, svc.ActionRejected)
    assert verdict.reason == "source_not_owned"


async def test_source_moved_to_another_conversation_is_rejected(monkeypatch):
    source = _source_with_actions()
    issued = _issue(source)[0]
    moved = _row(
        id=source.id,
        response_json=source.response_json,
        conversation_id="conv-99",
    )
    db = _provider(moved, monkeypatch=monkeypatch)
    verdict = await _authorize(db, issued.token)
    assert isinstance(verdict, svc.ActionRejected)
    assert verdict.reason == "conversation_mismatch"


# ── Timp, chei, indisponibilitate ───────────────────────────────────────────────────────────
async def test_expired_token_reports_a_distinct_code(monkeypatch):
    source = _source_with_actions()
    issued = _issue(source, ttl_s=60)[0]
    db = _provider(source, monkeypatch=monkeypatch)
    verdict = await _authorize(db, issued.token, now=NOW + timedelta(hours=1))
    assert isinstance(verdict, svc.ActionRejected)
    assert verdict.code == "action_expired"


async def test_tampered_token_is_generically_invalid(monkeypatch):
    source = _source_with_actions()
    issued = _issue(source)[0]
    db = _provider(source, monkeypatch=monkeypatch)
    tampered = issued.token[:-2] + ("AB" if not issued.token.endswith("AB") else "CD")
    verdict = await _authorize(db, tampered)
    assert isinstance(verdict, svc.ActionRejected)
    assert verdict.code == "action_invalid"


async def test_rotated_key_still_authorizes_during_overlap(monkeypatch):
    """Emis cu k1, verificat după rotație: valabil până la expirare (fereastra de overlap)."""
    old_ring = _ring("k1", b"\x01")
    source = _source_with_actions()
    issued = svc.issue_actions(source, svc.plans_from_row(source), ring=old_ring, ttl_s=1800)[0]
    rotated = _rotated_ring()
    db = _provider(source, monkeypatch=monkeypatch)
    verdict = await _authorize(db, issued.token, ring=rotated)
    assert isinstance(verdict, svc.AuthorizedAction)
    assert verdict.key_slot == "previous"


async def test_emission_proof_uses_the_tokens_key_not_the_current_one(monkeypatch):
    """Altfel o rotație ar invalida retroactiv dovada pentru tokenuri emise legitim."""
    old_ring = _ring("k1", b"\x01")
    source = _source_with_actions()
    issued = svc.issue_actions(source, svc.plans_from_row(source), ring=old_ring, ttl_s=1800)[0]
    rotated = _rotated_ring()
    db = _provider(source, monkeypatch=monkeypatch)
    verdict = await _authorize(db, issued.token, ring=rotated)
    assert isinstance(verdict, svc.AuthorizedAction)


async def test_commerce_kind_is_refused_until_receipts_exist(monkeypatch):
    """Un token de coș (fabricat cu cheia noastră) e refuzat ONEST, fără nicio mutație."""
    from src.web.action_crypto import derive_action_id
    from src.web.action_models import ActionEnvelope

    ring = _ring()
    key = ring.current
    args = ActionArgs(product_ref=PID_A)
    source = _row()
    action_id = derive_action_id(
        key, source_turn_id=source.id, kind="cart_add_line", args=args.to_canonical()
    )
    envelope = ActionEnvelope(
        version="a1",
        action_id=action_id,
        kind="cart_add_line",
        args=args,
        policy="one_shot",
        audience="web-widget-v2",
        tenant_ref=svc.pseudonym(key, svc.SCOPE_TENANT, BIZ),
        session_ref=svc.pseudonym(key, svc.SCOPE_SESSION, session_ref_hash(TOKEN, VISITOR)),
        conversation_ref=svc.pseudonym(key, svc.SCOPE_CONVERSATION, source.conversation_id),
        source_turn_id=source.id,
        source_revision=1,
        issued_at=int(NOW.timestamp()),
        expires_at=int(NOW.timestamp()) + 1800,
    )
    plan = ActionPlan("cart_add_line", args)
    source = _row(
        id=source.id,
        response_json=svc.merge_actions_into_view({"content": "ok", "products": []}, (plan,)),
    )
    db = _provider(source, monkeypatch=monkeypatch)
    verdict = await _authorize(db, seal(key, envelope))
    assert isinstance(verdict, svc.ActionRejected)
    assert verdict.code == "action_unavailable"


# ── Consum one-shot ─────────────────────────────────────────────────────────────────────────
async def test_same_token_same_turn_is_allowed_through_to_replay(monkeypatch):
    source = _source_with_actions()
    issued = _issue(source)[0]
    fingerprint = svc.action_fingerprint(
        SECRET, business_id=BIZ, channel_token=TOKEN, action_id=issued.action_id
    )
    consumer = _row(client_turn_id="ct-1", request_fingerprint=fingerprint)
    db = _provider(source, consumer, monkeypatch=monkeypatch)
    verdict = await _authorize(db, issued.token, client_turn_id="ct-1")
    assert isinstance(verdict, svc.AuthorizedAction)


async def test_same_token_different_turn_is_already_consumed(monkeypatch):
    source = _source_with_actions()
    issued = _issue(source)[0]
    fingerprint = svc.action_fingerprint(
        SECRET, business_id=BIZ, channel_token=TOKEN, action_id=issued.action_id
    )
    consumer = _row(client_turn_id="ct-1", request_fingerprint=fingerprint)
    db = _provider(source, consumer, monkeypatch=monkeypatch)
    verdict = await _authorize(db, issued.token, client_turn_id="ct-2")
    assert isinstance(verdict, svc.ActionRejected)
    assert verdict.code == "action_already_consumed"


def test_consumption_conflict_is_pure_and_ordered():
    row = _row(client_turn_id="ct-1")
    assert svc.consumption_conflict(None, "ct-1") is None
    assert svc.consumption_conflict(row, "ct-1") is None
    assert svc.consumption_conflict(row, "ct-2").code == "action_already_consumed"


def test_action_fingerprint_ignores_text_and_page_context():
    """Cheia de consum e ACȚIUNEA: același buton de pe două pagini = un singur consum."""
    a = svc.action_fingerprint(SECRET, business_id=BIZ, channel_token=TOKEN, action_id="a1")
    b = svc.action_fingerprint(SECRET, business_id=BIZ, channel_token=TOKEN, action_id="a1")
    c = svc.action_fingerprint(SECRET, business_id=BIZ, channel_token=TOKEN, action_id="a2")
    assert a == b != c


def test_action_fingerprint_is_tenant_scoped():
    a = svc.action_fingerprint(SECRET, business_id=BIZ, channel_token=TOKEN, action_id="a1")
    b = svc.action_fingerprint(SECRET, business_id="biz-2", channel_token=TOKEN, action_id="a1")
    assert a != b


# ── Opțiunea de clarificare ─────────────────────────────────────────────────────────────────
async def test_option_text_comes_from_the_persisted_view(monkeypatch):
    plans = plan_actions(
        {"content": "Care e bugetul?", "products": [], "suggestions": ["Sub 100", "100-200"]},
        TurnFacts(pending_field="budget_max", pending_attempts=1),
    )
    view = svc.merge_actions_into_view(
        {"content": "Care e bugetul?", "products": [], "suggestions": ["Sub 100", "100-200"]},
        plans,
    )
    source = _row(response_json=view)
    issued = [a for a in _issue(source) if a.plan.args.option_ref == 1][0]
    db = _provider(source, monkeypatch=monkeypatch)
    verdict = await _authorize(db, issued.token)
    assert isinstance(verdict, svc.AuthorizedAction)
    assert verdict.command.option_text == "100-200"


async def test_option_text_is_none_when_the_option_list_shrank(monkeypatch):
    plans = plan_actions(
        {"content": "?", "products": [], "suggestions": ["A", "B", "C"]},
        TurnFacts(pending_field="budget_max", pending_attempts=1),
    )
    view = svc.merge_actions_into_view(
        {"content": "?", "products": [], "suggestions": ["A", "B", "C"]}, plans
    )
    source = _row(response_json=view)
    issued = [a for a in _issue(source) if a.plan.args.option_ref == 2][0]
    shrunk = _row(
        id=source.id,
        response_json={**view, "suggestions": ["A"]},
    )
    db = _provider(shrunk, monkeypatch=monkeypatch)
    verdict = await _authorize(db, issued.token)
    assert isinstance(verdict, svc.AuthorizedAction)
    assert verdict.command.option_text is None  # kernelul îl tratează ca stale


# ── Payload persistat ───────────────────────────────────────────────────────────────────────
def test_merge_actions_is_additive_and_optional():
    view = {"content": "x", "products": [], "suggestions": []}
    assert svc.merge_actions_into_view(dict(view), ()) == view
    merged = svc.merge_actions_into_view(
        dict(view), (ActionPlan("show_more", ActionArgs(session_ref="fp1")),)
    )
    assert merged["content"] == "x"
    assert merged[svc.ACTIONS_PAYLOAD_KEY] == [
        {"kind": "show_more", "args": {"session_ref": "fp1"}}
    ]


@pytest.mark.parametrize("broken", [None, "nu e listă", 42, [{"kind": "nope"}]])
def test_plans_from_row_is_defensive(broken):
    assert svc.plans_from_row(_row(response_json={"actions": broken})) == ()
