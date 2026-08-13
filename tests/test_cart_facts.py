"""NX-237 — CommerceFacts: semantica UNKNOWN, prospețime, variante, monedă. PUR (zero DB)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from src.commerce.cart_models import CommerceFacts, build_snapshot, format_amount
from src.commerce.facts_provider import build_facts

NOW = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
SLA = 86400
P1 = "11111111-1111-4111-8111-111111111111"
V1 = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def row(**over):
    base = {
        "id": P1,
        "name": "Ser hidratant",
        "brand": "Brand",
        "price": 89.0,
        "list_price": None,
        "currency": "RON",
        "availability": "in_stock",
        "stock": 10,
        "rating": 4.6,
        "review_count": 20,
        "review_summary": "hidratant, texturi bune",
        "attributes": {},
        "url": "https://x/y",
        "synced_at": NOW - timedelta(hours=1),
        "updated_at": NOW - timedelta(hours=1),
        "variants": [],
    }
    base.update(over)
    return base


def facts_for(r, variant=None):
    batch = build_facts([r], [(P1, variant)], now=NOW, sla_s=SLA)
    return batch.get(P1, variant)


# ── UNKNOWN ≠ 0 (D8) ────────────────────────────────────────────────────────────────────────


def test_zero_reviews_means_rating_unknown_not_zero_stars():
    f = facts_for(row(rating=0.0, review_count=0))
    assert f.rating is None and "rating" in f.unknown  # „0 stele" ar fi o afirmație


def test_null_stock_is_unknown_but_availability_stays_known():
    f = facts_for(row(stock=None))
    assert f.stock is None and "stock" in f.unknown
    assert f.availability == "in_stock" and f.sellable is None


def test_missing_price_is_unknown_never_zero():
    f = facts_for(row(price=None))
    assert f.price is None and "price" in f.unknown
    assert not f.price_known and f.facts_status == "unknown"


def test_promo_delivery_voucher_are_structurally_unknown():
    """Fără sursă canonică → nu se derivă din `updated_at` generic (data-readiness pct. 5)."""
    f = facts_for(row())
    assert {"delivery_promise", "promotion_eligibility", "voucher"} <= set(f.unknown)


def test_sale_price_is_the_only_canonical_promotion():
    f = facts_for(row(price=79.0, list_price=99.0))
    assert (f.price, f.list_price) == (79.0, 99.0)  # regula _SALE_ACTIVE, calculată în SQL


# ── prospețime ──────────────────────────────────────────────────────────────────────────────


def test_fresh_fact_is_known_and_old_fact_is_stale_not_dropped():
    fresh = facts_for(row())
    assert fresh.facts_status == "known" and not fresh.freshness.stale
    old = facts_for(row(synced_at=NOW - timedelta(days=9), updated_at=NOW - timedelta(days=9)))
    assert old.facts_status == "stale" and old.freshness.stale
    assert old.price == 89.0  # valoarea rămâne vizibilă; vechimea se DECLARĂ


def test_no_timestamp_is_conservatively_stale():
    f = facts_for(row(synced_at=None, updated_at=None))
    assert f.freshness.stale  # fără dovadă de prospețime nu afirmăm prospețime


# ── variante ────────────────────────────────────────────────────────────────────────────────


def test_variant_facts_override_product_facts():
    r = row(price=89.0, variants=[{"id": V1, "label": "50ml", "price": 120.0, "stock": 2}])
    f = facts_for(r, variant=V1)
    assert (f.price, f.stock, f.variant_label) == (120.0, 2, "50ml")


def test_variant_with_zero_known_stock_is_out_of_stock():
    r = row(variants=[{"id": V1, "label": "50ml", "price": 120.0, "stock": 0}])
    f = facts_for(r, variant=V1)
    assert f.availability == "out_of_stock" and f.sellable == "out_of_stock"


def test_foreign_variant_gets_no_facts():
    r = row(variants=[{"id": V1, "label": "50ml", "price": 120.0}])
    batch = build_facts([r], [(P1, "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")], now=NOW, sla_s=SLA)
    assert batch.get(P1, "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb") is None
    assert not batch.variant_known(P1, "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")


# ── snapshot: totaluri server-side ──────────────────────────────────────────────────────────


def _facts_map(*facts: CommerceFacts):
    return {(f.product_id, f.variant_id): f for f in facts}


def test_totals_unknown_if_any_line_price_unknown():
    known = facts_for(row())
    p2 = "22222222-2222-4222-8222-222222222222"
    unknown = CommerceFacts(product_id=p2, name="X", currency="RON", price=None)
    snap = build_snapshot(
        cart_id="c1",
        version=3,
        status="active",
        items=[
            {"product_id": P1, "variant_id": None, "quantity": 1},
            {"product_id": p2, "variant_id": None, "quantity": 1},
        ],
        facts=_facts_map(known, unknown),
    )
    assert snap.totals.status == "unknown" and snap.totals.value is None
    assert not snap.checkout_eligible and "price_unknown" in snap.blocked_reasons


def test_display_strings_are_localized_server_side():
    assert format_amount(89.0, "RON", "ro") == "89,00 lei"
    assert format_amount(89.0, "RON", "en") == "89.00 RON"
    assert format_amount(12.5, "EUR", "ro") == "12,50 EUR"


def test_snapshot_line_totals_and_grand_total():
    f = facts_for(row(price=10.0))
    snap = build_snapshot(
        cart_id="c1",
        version=1,
        status="active",
        items=[{"product_id": P1, "variant_id": None, "quantity": 3}],
        facts=_facts_map(f),
    )
    line = snap.lines[0]
    assert (line.line_total, line.line_total_display) == (30.0, "30,00 lei")
    assert (snap.totals.value, snap.totals.display) == (30.0, "30,00 lei")
    assert snap.totals.units == 3


def test_batch_is_one_provider_call_shape():
    rows = [row(), row(id="22222222-2222-4222-8222-222222222222", name="Alt produs")]
    refs = [(r["id"], None) for r in rows]
    batch = build_facts(rows, refs, now=NOW, sla_s=SLA, query_count=1)
    assert batch.query_count == 1 and len(batch.facts) == 2
