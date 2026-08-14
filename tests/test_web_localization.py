"""NX-240 — formatarea server-owned: `Decimal`, plural CLDR, fail-safe.

De ce merită teste proprii: fiecare funcție de aici e o regulă comercială care înainte trăia în
browser. Un separator greșit e o sumă greșită; un plural greșit e text care sună a traducere
automată; o excepție e un 500 pe un răspuns altfel bun.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.web.localization import (
    SUPPORTED_LOCALES,
    copy_for,
    currency_word,
    error_message,
    format_amount,
    format_availability,
    format_discount,
    format_freshness,
    format_money,
    format_need,
    format_quantity,
    format_rating,
    label,
    no_results_text,
    normalize_locale,
    plural_form,
    to_decimal,
)


# ── Decimal, nu float ───────────────────────────────────────────────────────────────────────
def test_float_input_is_read_as_written_not_as_binary():
    """`Decimal(0.1)` e `0.1000000000000000055…`. Trecerea prin `str()` e ce face ca prețurile
    să se rotunjească așa cum le vede omul, nu așa cum le vede FPU-ul."""
    assert to_decimal(0.1) == Decimal("0.1")
    assert to_decimal(1.005) == Decimal("1.005")


def test_half_up_rounding_is_explicit():
    assert format_amount(Decimal("1.005"), "en") == "1.01"
    assert format_amount(Decimal("2.675"), "en") == "2.68"


@pytest.mark.parametrize(
    ("locale", "expected"),
    [("ro", "1.234.567,89"), ("en", "1,234,567.89"), ("hu", "1 234 567,89")],
)
def test_grouping_and_separators_per_locale(locale, expected):
    assert format_amount(Decimal("1234567.89"), locale) == expected


def test_currency_word_is_natural_in_ro_and_iso_in_en():
    assert currency_word("RON", "ro") == "lei"
    assert currency_word("RON", "en") == "RON"
    assert currency_word("EUR", "ro") == "EUR"  # nu inventăm simboluri


def test_money_needs_both_amount_and_currency():
    assert format_money(89, "RON", "ro") == "89,00 lei"
    assert format_money(89, None, "ro") is None
    assert format_money(None, "RON", "ro") is None
    assert format_money(89, "  ", "ro") is None


@pytest.mark.parametrize("bad", ["nu-i număr", object(), float("inf"), float("nan"), True])
def test_unformattable_input_returns_none_instead_of_raising(bad):
    assert format_amount(bad, "ro") is None


# ── Reducere ────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("current", "previous", "expected"),
    [
        (Decimal("89"), Decimal("120"), "-25%"),  # real 25,83% → în JOS, nu „-26%"
        (Decimal("100"), Decimal("100"), None),  # nu e reducere
        (Decimal("120"), Decimal("89"), None),  # „reducere" inversă
        (Decimal("99.5"), Decimal("100"), None),  # sub 1% ⇒ zgomot, nu ofertă
        (Decimal("50"), Decimal("100"), "-50%"),
    ],
)
def test_discount_only_when_it_is_real(current, previous, expected):
    assert format_discount(current, previous) == expected


def test_discount_never_overstates_the_offer():
    """Direcția rotunjirii e o decizie de onestitate, nu de estetică: procentul afișat trebuie să
    fie ≤ procentul real, pentru orice pereche de prețuri."""
    for current, previous in ((Decimal("89"), Decimal("120")), (Decimal("7.01"), Decimal("9.99"))):
        shown = int(format_discount(current, previous).strip("-%"))
        real = (previous - current) / previous * 100
        assert shown <= real


# ── Plural ──────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("count", "expected"),
    [(1, "one"), (2, "few"), (19, "few"), (20, "other"), (101, "few"), (120, "other"), (0, "few")],
)
def test_romanian_plural_has_three_categories(count, expected):
    assert plural_form(count, "ro") == expected


def test_english_and_hungarian_have_two():
    assert plural_form(5, "en") == "other" and plural_form(1, "en") == "one"
    assert plural_form(5, "hu") == "other"


def test_rating_uses_the_right_plural_and_drops_a_decorative_zero():
    assert format_rating(Decimal("5"), 1, "ro") == "5 din 5 (1 recenzie)"
    assert format_rating(Decimal("4.75"), 3, "ro") == "4,8 din 5 (3 recenzii)"
    assert format_rating(Decimal("4.75"), 120, "ro") == "4,8 din 5 (120 de recenzii)"


def test_rating_without_reviews_shows_no_parenthesis():
    assert format_rating(Decimal("4.5"), 0, "ro") == "4,5 din 5"
    assert format_rating(Decimal("4.5"), None, "ro") == "4,5 din 5"


def test_rating_outside_the_scale_is_refused_rather_than_displayed():
    assert format_rating(Decimal("7"), 10, "ro") is None
    assert format_rating(Decimal("-1"), 10, "ro") is None


def test_quantity_is_text():
    assert format_quantity(2, "ro") == "2 buc."
    assert format_quantity(1, "en") == "1 pc."
    assert format_quantity(-1, "ro") is None


# ── Disponibilitate ─────────────────────────────────────────────────────────────────────────
def test_low_stock_becomes_urgency_copy_only_when_the_number_is_known():
    assert format_availability("in_stock", 3, "ro") == "Ultimele 3 bucăți"
    assert format_availability("in_stock", 1, "ro") == "Ultima bucată"
    assert format_availability("in_stock", None, "ro") == "În stoc"
    assert format_availability("in_stock", 50, "ro") == "În stoc"


def test_unknown_availability_returns_nothing_not_unavailable():
    assert format_availability(None, 3, "ro") is None
    assert format_availability("", 3, "ro") is None
    assert format_availability("stare_inventată", 3, "ro") is None


def test_out_of_stock_is_stated_plainly():
    assert format_availability("out_of_stock", 0, "ro") == "Stoc epuizat"


# ── Prospețime ──────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("age_s", "expected"),
    [
        (0, "verificat acum"),
        (59, "verificat acum"),
        (60, "verificat acum un minut"),
        (125, "verificat acum 2 minute"),
        (3600, "verificat acum o oră"),
        (86400, "verificat ieri"),
        (86400 * 21, "verificat acum 21 de zile"),
    ],
)
def test_freshness_text(age_s, expected):
    assert format_freshness(age_s, "ro") == expected


def test_freshness_refuses_negative_ages():
    assert format_freshness(-1, "ro") is None


# ── Locale ──────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("raw", ["ro-RO", "RO", "ro_RO", "ro"])
def test_locale_normalization(raw):
    assert normalize_locale(raw) == "ro"


@pytest.mark.parametrize("raw", ["de", "klingon", "", None, 42])
def test_unknown_locale_falls_back_to_the_pilot(raw):
    assert normalize_locale(raw) == "ro"


@pytest.mark.parametrize("locale", SUPPORTED_LOCALES)
def test_every_locale_has_the_complete_copy_surface(locale):
    """Un label lipsă într-un locale ar apărea abia în producție, pe un client care nu vorbește
    româna. Îl prindem aici, la fiecare adăugare de copy."""
    copy = copy_for(locale)
    assert set(copy["chrome"]) == {
        "launcher_label",
        "dialog_title",
        "dialog_description",
        "close_label",
        "new_chat_label",
    }
    assert set(copy["announcements"]) == {
        "accepted",
        "working",
        "validating",
        "completed",
        "failed",
        "cancelled",
    }
    assert set(copy["no_results"]) == {"no_match", "insufficient_data", "dependency_unavailable"}
    assert set(copy_for("ro")["labels"]) == set(copy["labels"])
    assert set(copy_for("ro")["errors"]) == set(copy["errors"])
    assert all(isinstance(v, str) and v.strip() for v in copy["labels"].values())


def test_unknown_label_key_returns_nothing_rather_than_the_key():
    assert label("nu_exista", "ro") is None
    assert label("cart_total", "ro") == "Total"


def test_unknown_error_code_gets_generic_copy():
    assert error_message("cod-inventat", "ro") == error_message("processing_error", "ro")
    assert error_message("deadline_exceeded", "en").startswith("The reply took")


def test_no_results_classes_are_distinguishable():
    texts = {
        c: no_results_text(c, "ro")
        for c in ("no_match", "insufficient_data", "dependency_unavailable")
    }
    assert len(set(texts.values())) == 3  # trei „nu"-uri diferite, nu unul singur reformulat


# ── Nevoi afișabile ─────────────────────────────────────────────────────────────────────────
def test_only_needs_with_an_honest_display_form_are_rendered():
    assert format_need("budget_max", 200, "RON", "ro") == "Buget: până în 200,00 lei"
    assert format_need("brand", "LumaDerm", "RON", "ro") == "Brand: LumaDerm"
    assert format_need("concerns", "ten_gras", "RON", "ro") is None  # slug intern, nu copy
    assert format_need("budget_max", None, "RON", "ro") is None
