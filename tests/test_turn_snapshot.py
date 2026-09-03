"""NX-234 — `TurnSnapshot`: imuabil, safe, bounded, determinist + rehidratarea de context.

Testele astea nu verifică „că merge", ci că nu se POATE altfel:
  • un snapshot nu ține conexiune, client, callback sau token — `snapshot_safety_violations` o
    demonstrează prin traversare, inclusiv pe un snapshot sabotat deliberat;
  • faptele se rehidratează O DATĂ, cu sursă și vechime; `UNKNOWN` nu devine `0` și nu devine
    `MISMATCH`;
  • o variantă de la ALT produs invalidează tot contextul (zero preț, zero stoc folosite);
  • un ID de la alt tenant e indistinct de unul inexistent;
  • cu prompt exposure stins, faptele nu există în obiect — nu „nu se folosesc".
"""

import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest

from src.catalog import context_resolver as cr
from src.config import get_settings
from src.models import BusinessConfig, ConversationState, ProductRef
from src.privacy import RawInbound, RawText, SafeInbound
from src.web import context as wc
from src.web.contracts_v2 import PageContextClaim
from src.worker import turn_snapshot as tsnap

NOW = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
PID = "0f6c2c3a-4c1e-4b1e-9a0d-2b0d2f1e7a11"
OTHER_PID = "9e8d7c6b-5a49-4382-9170-6f5e4d3c2b1a"
VID = "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d"
CID = "2b3c4d5e-6f70-4812-93a4-b5c6d7e8f901"
OTHER_CID = "3c4d5e6f-7081-4923-a4b5-c6d7e8f90123"


def _product_row(**over):
    row = {
        "id": PID,
        "external_id": "SKU-1",
        "name": "Ser Niacinamidă",
        "brand": "Petală",
        "url": "https://shop.example.com/p/ser",
        "image": "https://shop.example.com/i/ser.jpg",
        "currency": "RON",
        "price": 89.0,
        "list_price": 109.0,
        "price_source": "variant_min",
        "availability": "in_stock",
        "stock_total": 12,
        "rating": 4.8,
        "review_count": 120,
        "review_summary": "Clienții apreciază textura.",
        "category_id": CID,
        "category_name": "Seruri",
        "category_slug": "seruri",
        "category_path": "ingrijire/fata/seruri",
        "delivery_class": "standard",
        "restock_date": None,
        "content_status": "published",
        "updated_at": (NOW - timedelta(hours=2)).isoformat(),
        "synced_at": (NOW - timedelta(hours=2)).isoformat(),
        "verified_at": None,
    }
    row.update(over)
    return row


def _variant_row(**over):
    row = {
        "id": VID,
        "product_id": PID,
        "external_id": "SKU-1-30",
        "label": "30 ml",
        "sku": "SKU-1-30",
        "price": 89.0,
        "list_price": 109.0,
        "stock": 4,
        "updated_at": (NOW - timedelta(hours=2)).isoformat(),
    }
    row.update(over)
    return row


def _category_row(**over):
    row = {
        "id": CID,
        "name": "Seruri",
        "slug": "seruri",
        "path": "ingrijire/fata/seruri",
        "parent_id": None,
        "updated_at": (NOW - timedelta(hours=2)).isoformat(),
    }
    row.update(over)
    return row


class _FakeConn:
    """Conexiune falsă care NUMĂRĂ query-urile — asertul de N+1 e pe contorul ăsta."""

    def __init__(self, rows):
        self.rows = rows
        self.fetches = 0

    async def fetch(self, sql, *params):
        self.fetches += 1
        return self.rows


def _db(rows):
    conn = _FakeConn(rows)

    @asynccontextmanager
    async def provider(operation: str = "?"):
        yield conn

    provider.conn = conn
    return provider


def _rows(*entities):
    return [{"kind": kind, "ref": ref, "data": json.dumps(data)} for kind, ref, data in entities]


def _business(**over):
    return BusinessConfig(
        id=over.pop("id", "b1"),
        slug="demo",
        name="Demo",
        default_locale=over.pop("default_locale", "ro"),
        supported_locales=over.pop("supported_locales", ["ro"]),
        **over,
    )


async def _build(db, *, context, state=None, business=None, now=NOW):
    return await tsnap.build_turn_snapshot(
        db,
        turn_id="t1",
        business=business or _business(),
        contact_id="ct1",
        conversation_id="c1",
        conversation_revision=3,
        state=state or ConversationState(),
        raw_inbound=RawInbound(RawText("ce părere ai despre acesta?")),
        safe_inbound=SafeInbound(text="ce părere ai despre acesta?"),
        context=context,
        channel_id="chan",
        now=now,
    )


def _claim(**kw):
    return wc.normalize_context(PageContextClaim(**kw))


# ── Rehidratare: un round-trip, fapte cu sursă și vechime ────────────────────


async def test_pdp_context_is_rehydrated_from_catalog_not_from_the_browser():
    db = _db(_rows(("product", PID, _product_row())))
    snap = await _build(db, context=_claim(surface="product", product_id=PID))
    assert snap.surface.status == "resolved"
    assert snap.page_product_id == PID
    p = snap.surface.product
    assert p.name == "Ser Niacinamidă"  # numele vine din catalog
    assert p.price == 89.0 and p.list_price == 109.0 and p.on_sale
    assert p.source == "catalog.products"
    assert p.freshness.stale is False and p.freshness.bucket == "1-24h"
    assert db.conn.fetches == 1


@pytest.mark.parametrize("n_refs", [1, 6, 10])
async def test_batch_hydration_is_one_query_regardless_of_ref_count(n_refs):
    """Detectorul de N+1: 1 ref sau 10, tot un round-trip. Un lookup per referință ar fi pe
    drumul sincron al fiecărui turn."""
    refs = [wc.ContextRef(f"{PID[:-2]}{i:02d}", "uuid") for i in range(n_refs)]
    db = _db(_rows(*[("product", r.value, _product_row(id=r.value)) for r in refs]))
    out = await cr.hydrate_refs(db, "b1", products=refs)
    assert len(out["product"]) == n_refs
    assert db.conn.fetches == 1


async def test_unknown_is_not_zero_and_not_mismatch():
    """`products.rating` are `default 0`: un produs fără recenzii arată în DB exact ca unul
    evaluat cu zero. „0 stele" e o afirmație; absența recenziilor nu e."""
    db = _db(
        _rows(
            (
                "product",
                PID,
                _product_row(
                    rating=0.0,
                    review_count=0,
                    review_summary=None,
                    stock_total=None,
                    delivery_class=None,
                ),
            )
        )
    )
    snap = await _build(db, context=_claim(surface="product", product_id=PID))
    p = snap.surface.product
    assert p.rating is None
    assert {"rating", "stock_total", "review_summary", "delivery_class"} <= p.unknown
    # Livrarea și promoția n-au feed canonic: UNKNOWN prin construcție, nu „lipsă temporar".
    assert cr.STRUCTURALLY_UNKNOWN <= p.unknown


async def test_stale_marks_the_fact_it_does_not_hide_it():
    db = _db(
        _rows(
            (
                "product",
                PID,
                _product_row(updated_at=(NOW - timedelta(days=30)).isoformat(), synced_at=None),
            )
        )
    )
    snap = await _build(db, context=_claim(surface="product", product_id=PID))
    assert snap.surface.status == "stale"
    assert snap.surface.product.price == 89.0  # valoarea rămâne
    assert snap.surface.product.freshness.stale is True
    assert snap.surface.product.freshness.bucket == ">7d"


async def test_missing_timestamp_is_conservatively_stale():
    db = _db(_rows(("product", PID, _product_row(updated_at=None, synced_at=None))))
    snap = await _build(db, context=_claim(surface="product", product_id=PID))
    assert snap.surface.product.freshness.stale is True
    assert snap.surface.product.freshness.bucket == "unknown"


# ── Relații: variantă, categorie, tenant ─────────────────────────────────────


async def test_variant_from_another_product_invalidates_the_whole_context():
    db = _db(
        _rows(
            ("product", PID, _product_row()),
            ("variant", VID, _variant_row(product_id=OTHER_PID)),
        )
    )
    snap = await _build(db, context=_claim(surface="product", product_id=PID, variant_id=VID))
    assert snap.surface.status == "invalid"
    assert snap.surface.product is None and snap.surface.variant is None
    assert ("variant_product", "cross_product") in snap.surface.relation_rejections
    # Zero preț/stoc folosit: nici măcar ale produsului legitim.
    assert "89" not in json.dumps(snap.to_safe_dict())


async def test_variant_of_the_page_product_is_kept():
    db = _db(_rows(("product", PID, _product_row()), ("variant", VID, _variant_row())))
    snap = await _build(db, context=_claim(surface="product", product_id=PID, variant_id=VID))
    assert snap.surface.status == "resolved"
    assert snap.surface.variant.label == "30 ml" and snap.surface.variant.stock == 4


async def test_incompatible_category_is_dropped_not_turned_into_a_hard_filter():
    db = _db(
        _rows(
            ("product", PID, _product_row()),
            (
                "category",
                OTHER_CID,
                _category_row(id=OTHER_CID, slug="rujuri", path="machiaj/buze/rujuri"),
            ),
        )
    )
    snap = await _build(
        db, context=_claim(surface="product", product_id=PID, category_id=OTHER_CID)
    )
    assert snap.surface.status == "partial"
    assert snap.surface.category is None
    assert snap.surface.product is not None  # produsul rămâne ancoră
    assert ("category_product", "incompatible") in snap.surface.relation_rejections


async def test_ancestor_category_is_compatible():
    db = _db(
        _rows(
            ("product", PID, _product_row()),
            ("category", CID, _category_row(path="ingrijire/fata")),
        )
    )
    snap = await _build(db, context=_claim(surface="product", product_id=PID, category_id=CID))
    assert snap.surface.status == "resolved"
    assert snap.surface.category is not None


async def test_id_of_another_tenant_is_indistinguishable_from_missing():
    """Query-ul e `business_id = $1`, deci rândul nu vine deloc. Motivul e ACELAȘI ca la un id
    inexistent sau nepublicat — altfel statusul ar fi un oracol de existență cross-tenant."""
    db = _db([])  # tenantul curent nu vede nimic
    snap = await _build(db, context=_claim(surface="product", product_id=PID))
    assert snap.surface.status == "partial"
    assert snap.surface.reasons[-1] == "anchor_not_found"
    assert snap.surface.product is None


async def test_product_surface_without_any_id_is_absent_not_a_guess():
    """Pagina de produs poate randa widgetul înainte să știe id-ul: e context absent, nu
    invalid, și în niciun caz o ancoră ghicită."""
    db = _db([])
    snap = await _build(db, context=_claim(surface="product"))
    assert snap.surface.status == "absent"
    assert snap.page_product_id is None
    assert wc.missing_anchor(_claim(surface="product")) == "product_id"
    assert db.conn.fetches == 0


async def test_no_context_means_no_query_at_all():
    db = _db([])
    snap = await _build(db, context=None)
    assert snap.surface.status == "absent"
    assert db.conn.fetches == 0


# ── Catalog indisponibil ─────────────────────────────────────────────────────


async def test_catalog_timeout_is_unavailable_not_an_exception(monkeypatch):
    monkeypatch.setattr(get_settings(), "web_context_hydration_timeout_ms", 10)

    @asynccontextmanager
    async def slow(operation: str = "?"):
        import asyncio

        await asyncio.sleep(0.5)
        yield None

    snap = await _build(slow, context=_claim(surface="product", product_id=PID))
    assert snap.surface.status == "unavailable"
    assert "hydration_timeout" in snap.surface.reasons


async def test_catalog_error_is_unavailable_not_an_exception():
    @asynccontextmanager
    async def broken(operation: str = "?"):
        raise RuntimeError("catalog jos")
        yield  # pragma: no cover

    snap = await _build(broken, context=_claim(surface="product", product_id=PID))
    assert snap.surface.status == "unavailable"
    assert "hydration_failed" in snap.surface.reasons


# ── Cart: seam onest până la NX-237 ──────────────────────────────────────────


async def test_cart_is_unavailable_and_never_reads_conversation_state():
    state = ConversationState(cart=[{"product_id": PID, "name": "Ser", "price": 89.0}])
    db = _db([])
    snap = await _build(db, context=_claim(surface="cart", cart_ref="cart_v7"), state=state)
    assert snap.cart.status == "unavailable"
    assert snap.cart.reason == "no_canonical_cart_source"
    assert snap.cart.ref is None
    assert "89" not in json.dumps(snap.to_safe_dict())


# ── Imuabilitate, siguranță, determinism ─────────────────────────────────────


async def test_snapshot_is_frozen():
    db = _db(_rows(("product", PID, _product_row())))
    snap = await _build(db, context=_claim(surface="product", product_id=PID))
    with pytest.raises(Exception):  # noqa: B017 — FrozenInstanceError
        snap.locale = "hu"
    with pytest.raises(Exception):  # noqa: B017
        snap.surface.product.price = 1.0


async def test_snapshot_holds_no_connection_client_callback_or_token():
    db = _db(_rows(("product", PID, _product_row()), ("variant", VID, _variant_row())))
    snap = await _build(
        db,
        context=_claim(surface="product", product_id=PID, variant_id=VID),
        state=ConversationState(displayed_products=[ProductRef("p9", "Alt produs", 10.0)]),
    )
    assert tsnap.snapshot_safety_violations(snap) == []


def test_the_safety_checker_actually_catches_a_sabotaged_snapshot():
    """Un checker care nu prinde nimic nu dovedește nimic — îl sabotăm deliberat."""

    class FakeConnection:  # numele contează: markerul e pe tipul de handle
        pass

    class Holder:
        __slots__ = ("value",)

        def __init__(self, value):
            self.value = value

    bad = cr.SurfaceContext(surface="product", reasons=("eyJhbGciOiJIUzI1NiJ9.payload.sig",))
    assert any("token" in v for v in tsnap.snapshot_safety_violations(bad))
    assert tsnap.snapshot_safety_violations(cr.CartSnapshot(status="x", reason="y", ref=None)) == []
    holder = Holder(FakeConnection())
    assert tsnap.snapshot_safety_violations(holder.value)


async def test_safe_serialization_is_deterministic_and_leaks_no_raw_input():
    db = _db(_rows(("product", PID, _product_row())))
    snap = await _build(db, context=_claim(surface="product", product_id=PID))
    a = json.dumps(snap.to_safe_dict(), sort_keys=True)
    b = json.dumps(snap.to_safe_dict(), sort_keys=True)
    assert a == b
    assert "ce părere" not in a  # inputul brut nu se serializează niciodată
    assert snap.input.raw is not None  # dar rămâne disponibil în memoria turului (D6)
    assert "redacted" in repr(snap.input.raw)


async def test_snapshot_will_not_attach_to_another_conversation():
    db = _db([])
    snap = await _build(db, context=None)
    assert snap.matches(conversation_id="c1", revision=3)
    assert not snap.matches(conversation_id="c2")
    assert not snap.matches(conversation_id="c1", revision=4)


async def test_prompt_exposure_off_removes_the_facts_from_the_object():
    db = _db(_rows(("product", PID, _product_row())))
    snap = await _build(db, context=_claim(surface="product", product_id=PID))
    shadow = tsnap.without_evidence(snap)
    assert shadow.surface.status == "resolved"  # măsurarea rămâne
    assert shadow.surface.product is None and shadow.page_product_id is None
    assert "89" not in json.dumps(shadow.to_safe_dict())


# ── Observabilitate low-cardinality ──────────────────────────────────────────


async def test_events_are_low_cardinality_and_carry_no_external_ids():
    db = _db(
        _rows(
            (
                "product",
                PID,
                _product_row(updated_at=(NOW - timedelta(days=30)).isoformat(), synced_at=None),
            ),
            (
                "category",
                OTHER_CID,
                _category_row(id=OTHER_CID, slug="rujuri", path="machiaj/buze/rujuri"),
            ),
        )
    )
    snap = await _build(
        db, context=_claim(surface="product", product_id=PID, category_id=OTHER_CID)
    )
    events = dict(tsnap.snapshot_events(snap).items)
    assert events["web_context_validated"]["surface"] == "product"
    assert events["web_context_validated"]["outcome"] in {"stale", "partial"}
    assert events["web_context_relation_rejected"] == {
        "relation": "category_product",
        "reason": "incompatible",
    }
    assert events["web_context_stale"]["age_bucket"] == ">7d"
    assert events["web_context_query_count"]["bucket"] == "1"
    dumped = json.dumps(tsnap.snapshot_events(snap).items, default=str)
    assert PID not in dumped and OTHER_CID not in dumped


async def test_hydration_happens_at_execution_so_a_price_change_after_accept_is_seen():
    """Failure matrix: preț/stoc schimbat între accept și execuție → se rehidratează la
    EXECUȚIE, cu freshness marcat. Contextul persistat e ID-only tocmai ca să nu poată
    „îngheța" un preț vechi în requestul acceptat."""
    at_accept = _db(_rows(("product", PID, _product_row(price=89.0))))
    at_execution = _db(_rows(("product", PID, _product_row(price=59.0))))
    ctx = _claim(surface="product", product_id=PID)
    assert (await _build(at_accept, context=ctx)).surface.product.price == 89.0
    assert (await _build(at_execution, context=ctx)).surface.product.price == 59.0


async def test_locale_fallback_is_reported_and_server_owned():
    db = _db([])
    snap = await _build(
        db,
        context=_claim(surface="product", locale="hu-HU"),
        business=_business(supported_locales=["ro"]),
    )
    assert snap.locale == "ro"
    assert snap.locale_reason == "locale_unsupported"
    assert ("web_context_locale_fallback", {"reason": "locale_unsupported"}) in (
        tsnap.snapshot_events(snap).items
    )


# ── Integrare în processor: owner unic, gating, degradare ────────────────────


class _FakeTx:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *a):
        return False


class _PipelineConn:
    """Conn stub pentru `handle_turn`; răspunde la `fetch` cu rândul de context cerut."""

    def __init__(self, rows):
        self.rows = rows
        self.fetches = 0

    def transaction(self):
        return _FakeTx()

    async def fetch(self, sql, *params):
        self.fetches += 1
        return self.rows


def _fresh(row: dict) -> dict:
    """Un rând cu prospețimea ancorată în ceasul REAL.

    `_build` primește `now=NOW` (fix), deci acolo fixture-ul poate fi datat relativ la `NOW`. Dar
    `_run_turn` merge prin `handle_turn`, care folosește ceasul de sistem — un `synced_at` derivat
    dintr-un `NOW` hardcodat devine „stale" de la sine la 24h după ce s-a scris testul. Ancorăm
    doar pe calea aceea, ca testele cu ceas injectat să rămână deterministe."""
    real_now = datetime.now(UTC)
    return {
        **row,
        "synced_at": (real_now - timedelta(hours=2)).isoformat(),
        "updated_at": (real_now - timedelta(hours=2)).isoformat(),
    }


async def _run_turn(
    monkeypatch, *, page_context, rows=(), prompt_enabled=True, context_enabled=True
):
    """`handle_turn` cu DB stubbed (pattern G8-1) → snapshotul văzut de un stagiu."""
    from src.db.provider import static_db
    from src.models import Contact
    from src.worker import processor as proc
    from src.worker import turn_uow as uow

    seen: dict = {}

    async def capture(ctx, deps):
        seen["snapshot"] = ctx.snapshot
        seen["events"] = list(ctx.events)
        ctx.set_reply("ok")

    async def fake_conv(*a, **k):
        return {
            "id": "conv",
            "state": {},
            "state_version": 7,
            "locale": "ro",
            "bot_active": True,
        }

    async def anoop(*a, **k):
        return None

    async def fake_contact(*a, **k):
        return Contact(id="ct1", business_id="b1")

    async def fake_claim(*a, **k):
        return True

    async def fake_id(*a, **k):
        return "id-1"

    monkeypatch.setattr(uow, "claim_inbound", fake_claim)
    monkeypatch.setattr(uow, "mark_inbound_completed", anoop)
    monkeypatch.setattr(uow, "get_or_create_contact", fake_contact)
    monkeypatch.setattr(uow, "get_or_create_conversation", fake_conv)
    monkeypatch.setattr(uow, "insert_message", fake_id)
    monkeypatch.setattr(uow, "touch_last_inbound", anoop)
    monkeypatch.setattr(uow, "get_recent_messages", anoop)
    monkeypatch.setattr(uow, "get_summary_for_context", anoop)
    monkeypatch.setattr(uow, "fetch_relevant_facts", anoop)
    monkeypatch.setattr(uow, "enqueue_outbox", fake_id)
    monkeypatch.setattr(uow, "patch_conversation_state", anoop)
    monkeypatch.setattr(proc, "persist_events", anoop)
    monkeypatch.setattr(proc, "_record_turn_cost", anoop)
    monkeypatch.setattr(proc, "_llm_within_budget", anoop)
    monkeypatch.setattr(proc, "run_aftercare", anoop)
    s = get_settings()
    monkeypatch.setattr(s, "web_context_enabled", context_enabled)
    monkeypatch.setattr(s, "web_context_prompt_enabled", prompt_enabled and context_enabled)

    conn = _PipelineConn(list(rows))
    event = {
        "channel_kind": "webchat",
        "channel_account_id": "tok",
        "sender_external_id": "web_1",
        "provider_msg_id": "m1",
        "content_type": "text",
        "body": "ce părere ai despre acesta?",
    }
    if page_context is not None:
        event["page_context"] = page_context
    await proc.handle_turn(
        static_db(conn), _business(), "chan", event, stages=[capture], deliver=False
    )
    return seen, conn


async def test_processor_attaches_the_snapshot_before_the_pipeline(monkeypatch):
    seen, conn = await _run_turn(
        monkeypatch,
        page_context={"v": 1, "surface": "product", "product": {"id": PID, "kind": "uuid"}},
        rows=_rows(("product", PID, _fresh(_product_row()))),
    )
    snap = seen["snapshot"]
    assert snap is not None
    assert snap.page_product_id == PID
    assert snap.conversation.conversation_id == "conv" and snap.conversation.revision == 7
    assert tsnap.snapshot_safety_violations(snap) == []
    types = {e.type for e in seen["events"]}
    assert "web_context_validated" in types


async def test_prompt_flag_off_keeps_the_measurement_and_drops_the_facts(monkeypatch):
    seen, _ = await _run_turn(
        monkeypatch,
        page_context={"v": 1, "surface": "product", "product": {"id": PID, "kind": "uuid"}},
        rows=_rows(("product", PID, _fresh(_product_row()))),
        prompt_enabled=False,
    )
    snap = seen["snapshot"]
    assert snap.surface.status == "resolved"  # s-a măsurat
    assert snap.page_product_id is None  # dar nu există fapte de expus


async def test_one_context_query_per_turn_not_one_per_consumer(monkeypatch):
    """Faptele se rehidratează O SINGURĂ DATĂ per turn: promptul, tool-urile și projectorul
    citesc din snapshot, nu fiecare din catalog (ar fi prețuri diferite în același răspuns)."""
    _seen, conn = await _run_turn(
        monkeypatch,
        page_context={"v": 1, "surface": "product", "product": {"id": PID, "kind": "uuid"}},
        rows=_rows(("product", PID, _fresh(_product_row()))),
    )
    assert conn.fetches == 1


async def test_context_flag_off_means_no_snapshot_and_no_query(monkeypatch):
    """OFF byte-identic: nu se construiește nimic, nu se atinge catalogul, nimeni nu vede
    un câmp nou."""
    seen, conn = await _run_turn(
        monkeypatch,
        page_context={"v": 1, "surface": "product", "product": {"id": PID, "kind": "uuid"}},
        rows=_rows(("product", PID, _fresh(_product_row()))),
        context_enabled=False,
    )
    assert seen["snapshot"] is None
    assert conn.fetches == 0


async def test_channel_without_page_context_still_gets_a_snapshot_without_queries(monkeypatch):
    seen, conn = await _run_turn(monkeypatch, page_context=None)
    snap = seen["snapshot"]
    assert snap is not None and snap.surface.status == "absent"
    assert conn.fetches == 0


async def test_a_broken_context_never_kills_the_turn(monkeypatch, caplog):
    """P6: contextul e opțional. Un payload stricat produce un snapshot fără ancoră, nu o eroare."""
    seen, _ = await _run_turn(monkeypatch, page_context={"surface": "product", "product": 42})
    assert seen["snapshot"] is not None
    assert seen["snapshot"].page_product_id is None
