"""NX-240 — `EvidenceBundle`: fapte cu proveniență, trei stări, și diferența `updated_at` ≠
`verified_at`.

Testele de aici apără o singură propoziție: **absența unei dovezi nu devine niciodată o
afirmație.** Un preț fără monedă nu e „89", un rating fără recenzii nu e „0 stele", un stoc NULL
nu e „indisponibil", iar un rând atins ieri nu e un fapt verificat ieri.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from src.agent.evidence_bundle import (
    FACT_FIELDS,
    MAX_BUNDLE_PRODUCTS,
    STRUCTURALLY_UNSOURCED,
    EvidenceBundle,
    Fact,
    build_evidence_bundle,
    build_product_evidence,
    cart_evidence_from_snapshot,
)
from tests.nx240_helpers import BUSINESS_ID, NOW, PID_A, PID_B, VERIFIED_AT, row

SLA = 86_400


def _evidence(**overrides):
    return build_product_evidence(row(**overrides), business_id=BUSINESS_ID, now=NOW, sla_s=SLA)


# ── Cele trei stări ─────────────────────────────────────────────────────────────────────────
def test_complete_row_yields_known_verified_facts():
    product = _evidence()
    assert product.fact("price").status == "known"
    assert product.fact("price").value == Decimal("89.0")
    assert product.fact("price").verified is True
    assert product.fact("availability").verified is True
    assert product.sellable is None  # se poate PROMITE o mutație


def test_every_declared_field_has_an_entry():
    """Un câmp fără intrare ar fi un al patrulea status nedeclarat („poate există")."""
    product = _evidence()
    for name in FACT_FIELDS:
        assert product.fact(name).status in ("known", "unknown", "stale"), name


def test_price_without_currency_is_unknown_not_a_bare_number():
    product = _evidence(currency=None)
    assert product.fact("price").status == "unknown"
    assert product.fact("price").value is None


def test_zero_reviews_makes_rating_unknown_not_zero_stars():
    product = _evidence(rating=0.0, review_count=0)
    assert product.fact("rating").status == "unknown"
    assert product.fact("rating").reason == "zero_reviews"
    assert product.fact("review_count").status == "unknown"


def test_null_stock_is_unknown_not_out_of_stock():
    product = _evidence(stock=None)
    assert product.fact("stock").status == "unknown"
    # `availability` e un fapt SEPARAT, întreținut de catalog — nu se contaminează de la stoc.
    assert product.fact("availability").value == "in_stock"


def test_list_price_below_current_is_omitted_not_negative_discount():
    product = _evidence(price=120.0, list_price=89.0)
    assert product.fact("list_price").status == "unknown"


def test_delivery_promo_voucher_have_no_source_in_this_environment():
    product = _evidence()
    for name in STRUCTURALLY_UNSOURCED:
        fact = product.fact(name)
        assert fact.status == "unknown" and fact.reason == "no_source", name


# ── verified_at ≠ updated_at ────────────────────────────────────────────────────────────────
def test_updated_at_alone_never_counts_as_verification():
    """Un rând ATINS nu e un rând VERIFICAT. Fără `synced_at`, faptele rămân afișabile (sunt
    valori reale de catalog) dar NEVERIFICATE — iar raportul de readiness le numără separat."""
    product = _evidence(synced_at=None, updated_at=NOW)
    price = product.fact("price")
    assert price.usable is True and price.verified is False
    # CTA-ul rămâne posibil: garanția e la mutație (CartService revalidează), nu pe buton.
    assert product.sellable is None


def test_verified_fact_past_sla_becomes_stale_and_unusable():
    product = build_product_evidence(
        row(synced_at=NOW - timedelta(days=3)), business_id=BUSINESS_ID, now=NOW, sla_s=SLA
    )
    price = product.fact("price")
    assert price.status == "stale" and price.usable is False
    assert product.sellable == "availability_stale"  # expirat ⇒ nici preț, nici buton


def test_unverified_fact_can_never_become_stale():
    """`stale` înseamnă „știm că e vechi". Fără `verified_at` nu știm nimic despre vechime, deci
    a-l numi stale ar fi tot o afirmație nefondată."""
    fact = Fact.known("price", Decimal("10"), source="catalog.products", sla_s=1)
    assert fact.status == "known"


# ── Variante ────────────────────────────────────────────────────────────────────────────────
def test_variant_facts_beat_product_facts():
    product = build_product_evidence(
        row(variants=[{"id": "v1", "label": "50 ml", "price": 149.0, "stock": 7}]),
        business_id=BUSINESS_ID,
        now=NOW,
        sla_s=SLA,
        variant_id="v1",
    )
    assert product.fact("price").value == Decimal("149.0")
    assert product.fact("variant").value == "50 ml"
    assert product.fact("stock").value == 7


def test_variant_with_known_zero_stock_is_out_of_stock_for_that_variant():
    product = build_product_evidence(
        row(availability="in_stock", variants=[{"id": "v1", "label": "50 ml", "stock": 0}]),
        business_id=BUSINESS_ID,
        now=NOW,
        sla_s=SLA,
        variant_id="v1",
    )
    assert product.fact("availability").value == "out_of_stock"
    assert product.sellable == "out_of_stock"


def test_variant_that_does_not_belong_yields_no_borrowed_facts():
    product = build_product_evidence(
        row(variants=[{"id": "v1"}]),
        business_id=BUSINESS_ID,
        now=NOW,
        sla_s=SLA,
        variant_id="v-other",
    )
    assert product.fact("price").reason == "variant_mismatch"
    assert product.fact("title").status == "unknown"


# ── Constrângeri ────────────────────────────────────────────────────────────────────────────
def test_hard_mismatch_blocks_but_unknown_does_not():
    blocked = build_product_evidence(
        row(),
        business_id=BUSINESS_ID,
        now=NOW,
        sla_s=SLA,
        constraints=[{"facet": "spf", "verdict": "MISMATCH", "strength": "hard"}],
    )
    unsure = build_product_evidence(
        row(),
        business_id=BUSINESS_ID,
        now=NOW,
        sla_s=SLA,
        constraints=[{"facet": "spf", "verdict": "UNKNOWN", "strength": "hard"}],
    )
    assert blocked.blocked is True
    assert unsure.blocked is False and unsure.unknown_facets == ("spf",)


def test_soft_mismatch_does_not_block():
    product = build_product_evidence(
        row(),
        business_id=BUSINESS_ID,
        now=NOW,
        sla_s=SLA,
        constraints=[{"facet": "brand", "verdict": "MISMATCH", "strength": "soft"}],
    )
    assert product.blocked is False


def test_unknown_verdict_vocabulary_is_dropped_not_treated_as_match():
    product = build_product_evidence(
        row(),
        business_id=BUSINESS_ID,
        now=NOW,
        sla_s=SLA,
        constraints=[{"facet": "spf", "verdict": "PROBABLY", "strength": "hard"}],
    )
    assert product.constraints == ()


def test_rejected_match_class_is_blocked():
    product = build_product_evidence(
        row(), business_id=BUSINESS_ID, now=NOW, sla_s=SLA, match_class="rejected"
    )
    assert product.blocked is True


# ── Bundle ──────────────────────────────────────────────────────────────────────────────────
def test_bundle_is_bounded_and_deduped_preserving_provider_order():
    rows = [row(f"p{i}") for i in range(10)] + [row("p0")]
    bundle = build_evidence_bundle(
        business_id=BUSINESS_ID, locale="ro", rows=rows, now=NOW, sla_s=SLA
    )
    ids = [p.product_id for p in bundle.products]
    assert len(ids) == MAX_BUNDLE_PRODUCTS == len(set(ids))
    assert ids == [f"p{i}" for i in range(MAX_BUNDLE_PRODUCTS)]


def test_rows_without_identity_are_skipped_not_rendered_blank():
    bundle = build_evidence_bundle(
        business_id=BUSINESS_ID, locale="ro", rows=[{"name": "fără id"}], now=NOW, sla_s=SLA
    )
    assert bundle.products == ()


def test_renderable_excludes_blocked_products():
    bundle = build_evidence_bundle(
        business_id=BUSINESS_ID,
        locale="ro",
        rows=[row(PID_A), row(PID_B)],
        now=NOW,
        sla_s=SLA,
        match_class_by_product={PID_B: "rejected"},
    )
    assert [p.product_id for p in bundle.renderable] == [PID_A]


def test_coverage_counts_every_field_across_products():
    bundle = build_evidence_bundle(
        business_id=BUSINESS_ID,
        locale="ro",
        rows=[row(PID_A), row(PID_B, price=None)],
        now=NOW,
        sla_s=SLA,
    )
    coverage = bundle.coverage()
    assert coverage["price"] == {"known": 1, "stale": 0, "unknown": 1}
    assert coverage["delivery_promise"]["unknown"] == 2
    assert set(coverage) == set(FACT_FIELDS)


# ── Serializare (ce se îngheață în ledger) ──────────────────────────────────────────────────
def test_roundtrip_through_json_preserves_decimal_and_status():
    bundle = build_evidence_bundle(
        business_id=BUSINESS_ID, locale="ro", rows=[row()], now=NOW, sla_s=SLA
    )
    restored = EvidenceBundle.from_jsonb(json.loads(json.dumps(bundle.to_jsonb())))
    assert restored is not None
    original, back = bundle.products[0], restored.products[0]
    assert back.fact("price").value == original.fact("price").value
    assert isinstance(back.fact("price").value, Decimal)
    assert back.fact("rating").status == original.fact("rating").status
    assert back.fact("delivery_promise").reason == "no_source"
    assert restored.as_of == bundle.as_of


def test_unknown_schema_version_is_refused_not_guessed():
    bundle = build_evidence_bundle(
        business_id=BUSINESS_ID, locale="ro", rows=[row()], now=NOW, sla_s=SLA
    )
    payload = bundle.to_jsonb()
    payload["schema_version"] = 99
    assert EvidenceBundle.from_jsonb(payload) is None


def test_broken_payload_degrades_to_unknown_instead_of_raising():
    assert Fact.from_jsonb("price", {"s": "maybe"}).status == "unknown"
    assert Fact.from_jsonb("price", "nu e dict").status == "unknown"
    assert EvidenceBundle.from_jsonb({"schema_version": 1}) is None


# ── Coș ─────────────────────────────────────────────────────────────────────────────────────
def test_cart_evidence_keeps_numbers_not_display_strings():
    """Snapshotul canonic vine cu string-uri de display; bundle-ul păstrează NUMERELE, ca
    formatarea să rămână într-un singur loc (projectorul, cu regulile de locale)."""
    from src.commerce.cart_models import build_snapshot
    from src.commerce.facts_provider import build_facts

    facts = build_facts(
        [dict(row(), synced_at=VERIFIED_AT)],
        [(PID_A, None)],
        now=NOW,
        sla_s=SLA,
    )
    snapshot = build_snapshot(
        cart_id="cart-1",
        version=2,
        status="active",
        items=[{"product_id": PID_A, "variant_id": None, "quantity": 2}],
        facts=facts.facts,
    )
    cart = cart_evidence_from_snapshot(snapshot)
    assert cart is not None
    assert cart.version == 2 and cart.units == 2
    assert cart.lines[0].line_total == Decimal("178.0")
    assert cart.total_status == "known"


def test_foreign_snapshot_shape_yields_no_cart_instead_of_breaking_the_turn():
    assert cart_evidence_from_snapshot(object()) is None
    assert cart_evidence_from_snapshot(None) is None


def test_as_of_is_normalized_to_utc():
    naive = datetime(2026, 8, 14, 12, 0, 0, tzinfo=UTC).astimezone(UTC)
    bundle = build_evidence_bundle(
        business_id=BUSINESS_ID, locale="ro", rows=[], now=naive, sla_s=SLA
    )
    assert bundle.as_of.tzinfo is not None
