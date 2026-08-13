"""NX-234 — contextul de pagină ca input NEÎNCREZĂTOR: normalizare, policy, fingerprint, ruta v2.

Ce dovedesc testele (nu ce speră):
  • forma acceptată e strict ID-uri + suprafață — un câmp comercial e 422 cu cod PROPRIU, înainte
    de accept, deci înainte de fingerprint și de orice muncă;
  • ID-urile sunt bounded și cu charset restrâns (un `'` sau un spațiu nu e un ID);
  • suprafața decide ce ID are sens: un `product_id` afirmat pe `home` se IGNORĂ cu reason code;
  • contextul intră în identitatea requestului: aceeași întrebare de pe DOUĂ pagini nu e același
    turn (409 la refolosirea `client_turn_id`), dar un context gol lasă fingerprint-ul de dinainte
    de card BYTE-IDENTIC;
  • persistarea e durabilă și defensivă: ce se scrie se poate reciti, iar un rând stricat nu crapă
    hot path-ul.
"""

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest

from src.config import get_settings
from src.web import app as wa
from src.web import context as wc
from src.web import turn_service as ts
from src.web.contracts_v2 import PageContextClaim, WebTurnRequestV2, parse_turn_request

PID = "0f6c2c3a-4c1e-4b1e-9a0d-2b0d2f1e7a11"
VID = "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d"
CID = "9e8d7c6b-5a49-4382-9170-6f5e4d3c2b1a"


# ── Normalizare structurală (pură, fără DB) ──────────────────────────────────


def test_product_surface_keeps_uuid_variant_and_category():
    n = wc.normalize_context(
        PageContextClaim(surface="product", product_id=PID, variant_id=VID, category_id=CID)
    )
    assert n.surface == "product"
    assert n.product == wc.ContextRef(PID, "uuid")
    assert n.variant == wc.ContextRef(VID, "uuid")
    assert n.category == wc.ContextRef(CID, "uuid")
    assert n.rejected == ()


def test_uuid_is_canonicalized_so_the_same_page_hashes_the_same():
    upper = wc.normalize_context(PageContextClaim(surface="product", product_id=PID.upper()))
    lower = wc.normalize_context(PageContextClaim(surface="product", product_id=PID))
    assert upper.product == lower.product
    assert wc.fingerprint_context(upper) == wc.fingerprint_context(lower)


def test_platform_key_is_accepted_as_opaque_external_ref():
    n = wc.normalize_context(PageContextClaim(surface="product", product_id="SKU-8812_A"))
    assert n.product == wc.ContextRef("SKU-8812_A", "external")


@pytest.mark.parametrize(
    "raw",
    [
        "p 8812",
        "p'8812",
        "https://shop.example.com/p/ser",
        "../../etc/passwd",
        "p8812; drop table products",
    ],
)
def test_malformed_ids_are_dropped_with_a_reason_not_passed_to_sql(raw):
    n = wc.normalize_context(PageContextClaim(surface="product", product_id=raw))
    assert n.product is None
    assert ("product_id", "id_charset") in n.rejected


@pytest.mark.parametrize(
    ("raw", "reason"),
    [("   ", "id_empty"), ("x" * (wc.MAX_CONTEXT_ID_LEN + 1), "id_too_long")],
)
def test_caps_hold_on_the_read_path_too(raw, reason):
    """Contractul NX-228 prinde gol/oversize la intrare; `normalize_id` îl reface pe drumul de
    RECITIRE din DB, unde nu mai există Pydantic între noi și un rând editat manual."""
    sink = wc._Collector()
    assert wc.normalize_id(raw, "product_id", sink) is None
    assert ("product_id", reason) in sink.rejected


def test_id_not_meaningful_for_surface_is_ignored_not_trusted():
    """Un produs „curent" afirmat pe homepage nu e context bonus, e o ancoră pe care nimeni
    n-a navigat — exact mecanismul prin care un host ar ancora conversația pe orice produs."""
    n = wc.normalize_context(PageContextClaim(surface="home", product_id=PID))
    assert n.product is None
    assert ("product_id", "id_not_meaningful_for_surface") in n.rejected
    assert n.is_empty


def test_variant_without_product_has_no_relation_to_verify():
    n = wc.normalize_context(PageContextClaim(surface="product", variant_id=VID))
    assert n.variant is None
    assert ("variant_id", "variant_without_product") in n.rejected


def test_cart_ref_only_makes_sense_on_cart_surfaces():
    on_cart = wc.normalize_context(PageContextClaim(surface="cart", cart_ref="cart_v7"))
    on_product = wc.normalize_context(
        PageContextClaim(surface="product", product_id=PID, cart_ref="cart_v7")
    )
    assert on_cart.cart_ref == "cart_v7"
    assert on_product.cart_ref is None


def test_unknown_surface_falls_back_to_other_without_anchors():
    claim = PageContextClaim(surface="order", product_id=PID)
    n = wc.normalize_context(claim, allowed_surfaces=frozenset({"product", "other"}))
    assert n.surface == "other"
    assert ("surface", "surface_not_supported") in n.rejected
    assert n.product is None


def test_missing_anchor_is_reported_per_surface():
    assert wc.missing_anchor(wc.normalize_context(PageContextClaim(surface="product"))) == (
        "product_id"
    )
    assert wc.missing_anchor(wc.normalize_context(PageContextClaim(surface="home"))) is None


# ── Locale: forma aici, negocierea server-side ───────────────────────────────


def test_locale_is_reduced_to_the_primary_subtag():
    assert wc.normalize_context(PageContextClaim(locale="ro-RO")).locale == "ro"
    assert wc.normalize_context(PageContextClaim(locale="HU_hu")).locale == "hu"


def test_malformed_locale_is_dropped():
    n = wc.normalize_context(PageContextClaim(locale="ro; drop table"))
    assert n.locale is None
    assert ("locale", "locale_malformed") in n.rejected


def test_unsupported_locale_falls_back_to_the_business_default_deterministically():
    assert wc.negotiate_locale("hu", supported=["ro"], default="ro") == ("ro", "locale_unsupported")
    assert wc.negotiate_locale("hu", supported=["ro", "hu"], default="ro") == ("hu", None)
    assert wc.negotiate_locale(None, supported=["ro"], default="ro") == ("ro", None)


# ── Câmpuri comerciale: 422 cu cod propriu ───────────────────────────────────


@pytest.mark.parametrize("field", ["price", "stock", "title", "cart_items", "rating", "page_url"])
def test_commercial_fields_are_rejected_by_name(field):
    with pytest.raises(wc.CommercialFieldRejected) as e:
        wc.reject_commercial_fields({"surface": "product", field: "orice"})
    assert e.value.field_name == field


def test_contract_itself_forbids_extra_fields():
    """`extra="forbid"` (NX-228) rămâne mecanismul; codul propriu e doar eticheta."""
    with pytest.raises(ValueError, match="extra"):
        PageContextClaim(surface="product", price=89.0)


# ── Fingerprint: contextul face parte din identitatea requestului ────────────


def _fp(context=None):
    return ts.request_fingerprint(
        "secret", business_id="b1", channel_token="tok", text="ce părere ai?", context=context
    )


def test_empty_context_keeps_the_pre_card_fingerprint_byte_identical():
    assert _fp(None) == ts.request_fingerprint(
        "secret", business_id="b1", channel_token="tok", text="ce părere ai?"
    )


def test_same_question_on_two_pages_is_not_the_same_request():
    page_a = wc.fingerprint_context(
        wc.normalize_context(PageContextClaim(surface="product", product_id=PID))
    )
    page_b = wc.fingerprint_context(
        wc.normalize_context(PageContextClaim(surface="product", product_id=CID))
    )
    assert _fp(page_a) != _fp(page_b)
    assert _fp(page_a) == _fp(page_a)


def test_reasons_do_not_change_request_identity():
    """Motivele sunt observabilitate. Două requesturi identice ca ancore nu pot deveni un
    conflict fiindcă unul a mai avut un câmp ignorat pe drum."""
    clean = wc.normalize_context(PageContextClaim(surface="product", product_id=PID))
    noisy = wc.normalize_context(
        PageContextClaim(surface="product", product_id=PID, variant_id="bad id")
    )
    assert noisy.rejected  # chiar a fost ceva de ignorat
    assert wc.fingerprint_context(clean) == wc.fingerprint_context(noisy)


# ── Persistare durabilă (payload de mesaj), citire defensivă ─────────────────


def test_payload_round_trip_preserves_refs_and_kind():
    n = wc.normalize_context(
        PageContextClaim(surface="product", product_id="SKU-1", category_id=CID, locale="ro-RO")
    )
    back = wc.from_payload(wc.to_payload(n))
    assert back is not None
    assert back.surface == "product"
    assert back.product == wc.ContextRef("SKU-1", "external")
    assert back.category == wc.ContextRef(CID, "uuid")
    assert back.locale == "ro"


def test_empty_context_is_not_persisted_at_all():
    assert wc.to_payload(wc.normalize_context(None)) is None
    assert wc.to_payload(wc.normalize_context(PageContextClaim(surface="other"))) is None


@pytest.mark.parametrize(
    "raw", [None, "nu e dict", 42, {}, {"surface": 9}, {"product": "nu e obiect"}]
)
def test_broken_persisted_context_never_raises_on_the_hot_path(raw):
    assert wc.from_payload(raw) is None


def test_persisted_context_is_re_validated_on_read():
    """DB-ul e sursă de durabilitate, nu de încredere: un rând editat manual nu devine adevăr —
    ancora cade, contextul rămâne o suprafață fără ancoră (`partial` downstream), nu un ID toxic."""
    tampered = {"v": 1, "surface": "product", "product": {"id": "p 1", "kind": "external"}}
    back = wc.from_payload(tampered)
    assert back is not None
    assert back.product is None
    assert ("product", "id_charset") in back.rejected


def test_persisted_payload_carries_no_commercial_field():
    payload = wc.to_payload(
        wc.normalize_context(PageContextClaim(surface="product", product_id=PID, variant_id=VID))
    )
    dumped = json.dumps(payload)
    assert not (set(payload) & wc.COMMERCIAL_FIELDS)
    assert "price" not in dumped and "stock" not in dumped


# ── Ruta v2: poarta de margine ───────────────────────────────────────────────


@asynccontextmanager
async def _fake_cm(*a, **k):
    yield None


class _FakeRedis:
    async def incr(self, *a, **k):
        return 1

    async def expire(self, *a, **k):
        return True

    async def get(self, *a, **k):
        return None

    def pipeline(self):
        return self

    def lpush(self, *a, **k):
        return self

    def ltrim(self, *a, **k):
        return self

    async def execute(self):
        return []


class _Req:
    def __init__(self, body: bytes):
        self.headers = {"content-length": str(len(body))}
        self._body = body

    async def stream(self):
        yield self._body

    @property
    def client(self):
        return SimpleNamespace(host="127.0.0.1")


def _v2_body(client_turn_id, *, context=None, text="ce părere ai despre acesta?"):
    payload = {
        "schema_version": "web-turn.v2",
        "client_turn_id": str(client_turn_id),
        "input": {"type": "text", "text": text},
    }
    if context is not None:
        payload["context"] = context
    return json.dumps(payload).encode()


def _wire(monkeypatch, accept):
    s = get_settings()
    monkeypatch.setattr(s, "web_turn_v2_enabled", True)
    monkeypatch.setattr(s, "web_context_enabled", True)

    async def fake_verify(token, vid, sig):
        return wa.WebSession(business_id="b1", token=token, visitor_id=vid)

    async def fake_resolve_channel(conn, kind, token):
        return {"channel_id": "chan", "business_id": "b1"}

    async def fake_load_business(conn, bid):
        return SimpleNamespace(id=bid, daily_cost_cap_usd=None, default_locale="ro")

    events: list = []

    async def fake_persist(db, business_id, conversation_id, contact_id, evs, **kw):
        events.extend(evs)

    async def fake_wake(redis, business_id, turn_id):
        return None

    async def fail_handle(*a, **k):
        raise AssertionError("acceptul nu rulează pipeline")

    monkeypatch.setattr(wa, "_verify", fake_verify)
    monkeypatch.setattr(wa, "get_redis", lambda: _coro(_FakeRedis()))
    monkeypatch.setattr(wa, "get_pool", lambda: _coro(None))
    monkeypatch.setattr(wa, "admin_conn", _fake_cm)
    monkeypatch.setattr(wa, "tenant_db", lambda business_id: _fake_cm)
    monkeypatch.setattr(wa, "resolve_channel", fake_resolve_channel)
    monkeypatch.setattr(wa, "load_business", fake_load_business)
    monkeypatch.setattr(wa, "persist_events", fake_persist)
    monkeypatch.setattr(wa, "wake_executor", fake_wake)
    monkeypatch.setattr(wa, "handle_turn", fail_handle)
    monkeypatch.setattr(wa, "accept_web_turn", accept)
    monkeypatch.setattr(wa, "cost_over_budget", lambda *a, **k: _coro(False))
    monkeypatch.setattr(wa, "web_cost_over_visitor_cap", lambda *a, **k: _coro(False))
    monkeypatch.setattr(wa, "web_rate_limited", lambda *a, **k: _coro(False))
    return events


async def _coro(value):
    return value


def _accepted_row(client_turn_id):
    from src.db.queries.web_turns import WebTurnRow
    from tests.test_web_turn_api_v2 import NOW

    return WebTurnRow(
        id=str(uuid4()),
        business_id="b1",
        conversation_id="c1",
        contact_id="ct1",
        session_ref_hash=ts.session_ref_hash("tok", "web_1"),
        client_turn_id=str(client_turn_id),
        request_fingerprint="fp",
        schema_version="web-turn.v2",
        status="accepted",
        attempt=0,
        lease_owner=None,
        lease_epoch=0,
        lease_expires_at=None,
        deadline_at=None,
        conversation_revision_at_accept=1,
        pipeline_version=ts.RESPONSE_CONTRACT_SYNC_V1,
        response_json=None,
        safe_error_code=None,
        accepted_at=NOW,
        updated_at=NOW,
        completed_at=None,
    )


async def test_commercial_field_is_422_before_accept(monkeypatch):
    async def never_accept(*a, **k):
        raise AssertionError("nu se acceptă nimic cu fapte comerciale în context")

    _wire(monkeypatch, never_accept)
    res = await wa.web_turn_accept_v2(
        _Req(_v2_body(uuid4(), context={"surface": "product", "product_id": PID, "price": 89.0})),
        token="tok",
        visitor_id="web_1",
        sig="s",
    )
    assert res.status_code == 422
    assert json.loads(res.body)["error"]["code"] == "context_commercial_field"


async def test_accepted_context_is_persisted_and_enters_the_fingerprint(monkeypatch):
    captured: dict = {}
    turn_id = uuid4()

    async def fake_accept(db, **kw):
        captured.update(kw)
        return ts.Accepted(_accepted_row(turn_id), inbound_msg_id="m1")

    events = _wire(monkeypatch, fake_accept)
    res = await wa.web_turn_accept_v2(
        _Req(_v2_body(turn_id, context={"surface": "product", "product_id": PID})),
        token="tok",
        visitor_id="web_1",
        sig="s",
    )
    assert res.status_code == 202
    # durabil: contextul normalizat pleacă spre rândul de mesaj, cu ID-uri, fără fapte
    assert captured["page_context"]["surface"] == "product"
    assert captured["page_context"]["product"] == {"id": PID, "kind": "uuid"}
    # identitatea requestului include ancora
    assert captured["fingerprint"] != ts.request_fingerprint(
        get_settings().web_turn_fingerprint_secret,
        business_id="b1",
        channel_token="tok",
        text="ce părere ai despre acesta?",
    )
    accepted = [e for e in events if e.type == "web_context_accepted"]
    assert accepted and accepted[0].properties["surface"] == "product"
    assert accepted[0].properties["has_anchor"] is True


async def test_context_absent_keeps_accept_byte_identical(monkeypatch):
    captured: dict = {}
    turn_id = uuid4()

    async def fake_accept(db, **kw):
        captured.update(kw)
        return ts.Accepted(_accepted_row(turn_id), inbound_msg_id="m1")

    _wire(monkeypatch, fake_accept)
    await wa.web_turn_accept_v2(_Req(_v2_body(turn_id)), token="tok", visitor_id="web_1", sig="s")
    assert captured["page_context"] is None
    assert captured["fingerprint"] == ts.request_fingerprint(
        get_settings().web_turn_fingerprint_secret,
        business_id="b1",
        channel_token="tok",
        text="ce părere ai despre acesta?",
    )


async def test_flag_off_ignores_context_entirely(monkeypatch):
    captured: dict = {}
    turn_id = uuid4()

    async def fake_accept(db, **kw):
        captured.update(kw)
        return ts.Accepted(_accepted_row(turn_id), inbound_msg_id="m1")

    _wire(monkeypatch, fake_accept)
    monkeypatch.setattr(get_settings(), "web_context_enabled", False)
    await wa.web_turn_accept_v2(
        _Req(_v2_body(turn_id, context={"surface": "product", "product_id": PID})),
        token="tok",
        visitor_id="web_1",
        sig="s",
    )
    assert captured["page_context"] is None
    assert captured["fingerprint"] == ts.request_fingerprint(
        get_settings().web_turn_fingerprint_secret,
        business_id="b1",
        channel_token="tok",
        text="ce părere ai despre acesta?",
    )


async def test_same_turn_id_after_navigating_is_an_idempotency_conflict(monkeypatch):
    """Failure matrix: refresh pe ALTĂ pagină cu același `client_turn_id` → conflict, nu un
    turn vechi cu context nou lipit pe el."""
    turn_id = uuid4()
    first = _accepted_row(turn_id)

    async def fake_accept(db, **kw):
        row = first
        if kw["fingerprint"] != stored["fp"]:
            return ts.IdempotencyConflict(row)
        return ts.Accepted(row, inbound_msg_id="m1")

    stored: dict = {"fp": None}

    async def capture_accept(db, **kw):
        if stored["fp"] is None:
            stored["fp"] = kw["fingerprint"]
            return ts.Accepted(first, inbound_msg_id="m1")
        return await fake_accept(db, **kw)

    _wire(monkeypatch, capture_accept)
    ok = await wa.web_turn_accept_v2(
        _Req(_v2_body(turn_id, context={"surface": "product", "product_id": PID})),
        token="tok",
        visitor_id="web_1",
        sig="s",
    )
    assert ok.status_code == 202
    moved = await wa.web_turn_accept_v2(
        _Req(_v2_body(turn_id, context={"surface": "product", "product_id": CID})),
        token="tok",
        visitor_id="web_1",
        sig="s",
    )
    assert moved.status_code == 409
    assert json.loads(moved.body)["error"]["code"] == "idempotency_conflict"


def test_request_contract_still_accepts_only_the_nx228_shape():
    """Fără shape drift: forma requestului rămâne EXACT cea din NX-228."""
    req: WebTurnRequestV2 = parse_turn_request(
        {
            "schema_version": "web-turn.v2",
            "client_turn_id": str(uuid4()),
            "input": {"type": "text", "text": "salut"},
            "context": {"surface": "product", "product_id": PID},
        }
    )
    assert req.context.product_id == PID
    with pytest.raises(ValueError, match="extra"):
        parse_turn_request(
            {
                "schema_version": "web-turn.v2",
                "client_turn_id": str(uuid4()),
                "input": {"type": "text", "text": "salut"},
                "context": {"surface": "product", "context_revision": 7},
            }
        )
