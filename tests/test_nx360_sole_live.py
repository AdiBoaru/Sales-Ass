"""NX-360 — citirea paginii live SOLE: disponibilitate din JSON-LD, preț DOAR din rândul de preț.

Paginile sunt reduse la structura reală măsurată pe sole.ro pe 2026-09-30: pe un produs în stoc,
rândul de preț („120 lei”) + voucherul („102 lei”, WELCOME15) + JSON-LD cu prețul voucherului; pe
unul epuizat, niciun rând de preț și JSON-LD `OutOfStock` cu prețul voucherului.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from scripts import sole_refresh as sr
from src.catalog import sole_live as sl


def _ld(availability: str, price: float) -> str:
    doc = {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": "X",
        "offers": {
            "@type": "Offer",
            "priceCurrency": "RON",
            "price": price,
            "availability": availability,
        },
    }
    crumbs = {"@type": "BreadcrumbList", "itemListElement": []}
    return (
        f'<script type="application/ld+json">{json.dumps(crumbs)}</script>'
        f'<script type="application/ld+json">{json.dumps(doc)}</script>'
    )


IN_STOCK = (
    "<html><head>"
    + _ld("http://schema.org/InStock", 102.00058)
    + "</head><body><div class='product-price-row'>"
    "<span class='product-price-row__value'>120 <sup>lei</sup></span></div>"
    "<div class='product-welcome-price'><span class='product-welcome-price__value'>102 lei</span>"
    "<span class='product-welcome-price__code'>cu codul <b>WELCOME15</b></span></div>"
    "</body></html>"
)
OUT_OF_STOCK = (
    "<html><head>"
    + _ld("http://schema.org/OutOfStock", 101.91467)
    + "</head><body><p>Stoc epuizat</p></body></html>"
)


def test_in_stock_page_takes_the_list_price_from_the_price_row_not_json_ld():
    page = sl.parse_page(IN_STOCK)
    assert page.is_product
    assert page.availability == "in_stock"
    assert page.price_regular == Decimal("120.00")
    assert page.price_promo == Decimal("102.00")
    assert page.promo_code == "WELCOME15"
    assert page.ld_price == Decimal("102.00")  # prețul voucherului, doar pentru diagnoză
    facts = page.price_facts()
    assert facts is not None
    assert facts.price == Decimal("120.00")
    assert facts.sale_price is None  # voucherul NU e reducere pentru oricine (regula importului)
    assert (facts.coupon_code, facts.coupon_price) == ("WELCOME15", Decimal("102.00"))


def test_out_of_stock_page_has_no_list_price_and_nothing_is_derived():
    """Defectul importului: fără rând de preț, scraperul lua `offers.price` (voucherul)."""
    page = sl.parse_page(OUT_OF_STOCK)
    assert page.availability == "out_of_stock"
    assert page.price_regular is None
    assert page.ld_price == Decimal("101.91")
    assert page.price_facts() is None
    refresh = sl.decide("p", page)
    assert refresh.status == "ok"
    assert refresh.availability == "out_of_stock"
    assert refresh.price is None
    assert refresh.price_unverified is True


def test_a_page_that_is_not_a_product_changes_nothing():
    refresh = sl.decide("p", sl.parse_page("<html><body>Pagina nu există</body></html>"))
    assert refresh.status == "gone"
    assert refresh.availability is None and refresh.price is None


def test_unknown_schema_availability_is_not_guessed():
    page = sl.parse_page(_ld("http://schema.org/PreOrder", 10.0))
    assert page.availability is None
    assert sl.decide("p", page).status == "unknown_availability"


def test_amounts_with_thousands_and_cents():
    assert sl.parse_amount("1.299,90 lei") == Decimal("1299.90")
    assert sl.parse_amount("51 lei") == Decimal("51.00")
    assert sl.parse_amount("lei") is None
    assert sl.availability_from_schema("https://schema.org/InStock/") == "in_stock"
    assert sl.availability_from_schema("SoldOut") == "out_of_stock"


# ── markup-ul REAL al paginilor SOLE (recenzia NX-360) ──────────────────────────────────────────
#
# Copiate din paginile descărcate pe 2026-09-30: prețul de listă FĂRĂ separator de mii, bănuții
# voucherului într-un `<small>` lipit. Prima variantă a parserului dădea 190 în loc de 1900 și 76
# în loc de 76,50, iar `apply` le-ar fi scris în catalog.

REAL_EXPENSIVE = (
    "<html><head>"
    + _ld("http://schema.org/InStock", 1615.0)
    + '</head><body><span class="product-price-row__value">1900 lei</span>'
    '<span data-price="1900,00008" class="product-welcome-price__value">1615 lei</span> '
    '<span class="product-welcome-price__code-wrap">folosind codul '
    '<b class="product-welcome-price__code">WELCOME15</b></span></body></html>'
)
REAL_CENTS = (
    "<html><head>"
    + _ld("http://schema.org/InStock", 76.5)
    + '</head><body><span class="product-price-row__value">90 lei</span>'
    '<span data-price="89,9998" class="product-welcome-price__value">'
    '<span class="price-sub">76<small>.50</small></span> lei</span> '
    '<span class="product-welcome-price__code-wrap">folosind codul '
    '<b class="product-welcome-price__code">WELCOME15</b></span></body></html>'
)


def test_real_price_without_thousands_separator():
    page = sl.parse_page(REAL_EXPENSIVE)
    assert (page.price_regular, page.price_promo) == (Decimal("1900.00"), Decimal("1615.00"))


def test_real_voucher_cents_in_a_small_tag():
    page = sl.parse_page(REAL_CENTS)
    assert (page.price_regular, page.price_promo) == (Decimal("90.00"), Decimal("76.50"))


@pytest.mark.parametrize(
    ("text", "amount"),
    [
        ("1900 lei", "1900.00"),
        ("76.50 lei", "76.50"),
        ("1.299,90 lei", "1299.90"),
        ("1299,90", "1299.90"),
        ("1 299,90 lei", "1299.90"),
        ("1.299 lei", "1299.00"),
        ("99.9 lei", "99.90"),
    ],
)
def test_amount_formats(text, amount):
    assert sl.parse_amount(text) == Decimal(amount)


def test_in_stock_without_a_price_row_is_not_written():
    """Recenzia: trecut în stoc cu prețul vechi (al voucherului), produsul devenea vandabil exact la
    prețul greșit. Fără preț pe pagină, nu se atinge nimic."""
    page = sl.parse_page(_ld("http://schema.org/InStock", 33.9))
    refresh = sl.decide("p", page)
    assert refresh.status == "in_stock_without_price"
    row = sr.diff_row(CURRENT, refresh)
    assert sr.apply_params("b", row) is None


def test_an_old_report_is_refused():
    from datetime import UTC, datetime, timedelta

    now = datetime(2026, 10, 1, 12, tzinfo=UTC)
    assert sr.report_too_old((now - timedelta(hours=25)).isoformat(), now)
    assert not sr.report_too_old((now - timedelta(hours=2)).isoformat(), now)


# ── raportul și scrierea (pure) ─────────────────────────────────────────────────────────────────

CURRENT = {
    "id": "p1",
    "product_url": "https://sole.ro/x.html",
    "availability": "out_of_stock",
    "price": Decimal("101.91"),
    "sale_price": None,
    "coupon_code": None,
    "coupon_price": None,
    "n_variants": 1,
}


def test_restocked_product_gets_its_real_price_and_coupon():
    """ROUND LAB Birch Juice: epuizat la 101,91 în baza noastră, în stoc la 120 lei pe site."""
    row = sr.diff_row(CURRENT, sl.decide("p1", sl.parse_page(IN_STOCK)))
    assert row["availability"] == {"old": "out_of_stock", "new": "in_stock"}
    assert row["price"]["new"] == {
        "price": "120.00",
        "sale_price": None,
        "coupon_code": "WELCOME15",
        "coupon_price": "102.00",
    }
    product, variant = sr.apply_params("b", row)
    assert product == (
        "b",
        "p1",
        "in_stock",
        Decimal("120.00"),
        None,
        "WELCOME15",
        Decimal("102.00"),
    )
    assert variant == ("b", "p1", Decimal("120.00"), None)
    summary = sr.summarize([row])
    assert summary["availability_flips"] == {"out_of_stock→in_stock": 1}
    assert summary["price_changed"] == 1


def test_still_out_of_stock_keeps_the_stored_price_untouched():
    row = sr.diff_row(CURRENT, sl.decide("p1", sl.parse_page(OUT_OF_STOCK)))
    product, variant = sr.apply_params("b", row)
    assert product[3:] == (None, None, None, None)  # `coalesce` ⇒ prețul rămâne cel din bază
    assert variant is None
    assert sr.summarize([row])["price_unverified"] == 1


def test_multi_variant_product_gets_availability_but_no_price():
    row = sr.diff_row({**CURRENT, "n_variants": 2}, sl.decide("p1", sl.parse_page(IN_STOCK)))
    product, variant = sr.apply_params("b", row)
    assert product[2] == "in_stock"
    assert product[3:] == (None, None, None, None)  # nici prețul produsului (recenzia)
    assert variant is None
    assert sr.summarize([row])["multi_variant_price_skipped"] == 1


def test_gone_and_unknown_rows_are_never_written():
    gone = sr.diff_row(CURRENT, sl.decide("p1", sl.parse_page("<html></html>")))
    assert sr.apply_params("b", gone) is None


# ── recenzia finală: întăriri ────────────────────────────────────────────────────────────────────


def test_a_voucher_whose_code_is_not_recognised_never_becomes_a_general_sale():
    html = IN_STOCK.replace("<b>WELCOME15</b>", "<b>w15</b>")
    facts = sl.parse_page(html).price_facts()
    assert facts is not None
    assert facts.price == Decimal("120.00")
    assert facts.sale_price is None and facts.coupon_price is None
    assert "voucher_without_code" in facts.anomalies


def test_non_breaking_space_as_thousands_separator():
    assert sl.parse_amount("1\xa0900 lei") == Decimal("1900.00")
    assert sl.parse_amount("1\u202f299,90 lei") == Decimal("1299.90")


def test_a_redirect_to_another_product_is_not_the_same_product():
    base = "https://sole.ro/ten/creme/round-lab-birch-f77765.html"
    assert sl.same_product_url(base, "https://sole.ro/ten/creme/round-lab-birch-f77765")
    assert sl.same_product_url(base, "https://sole.ro/ten/creme/round-lab-birch-f77765/?utm=x")
    assert not sl.same_product_url(base, "https://sole.ro/ten/creme/alt-produs-f11111")
    assert not sl.same_product_url(base, "https://sole.ro/ten/creme")
