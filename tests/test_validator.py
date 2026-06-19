"""NX-91 — validator cifre bare (numere fără valută halucinate). Funcții PURE, zero LLM/DB.

Acoperă: extragerea numerelor permise din retrieval, prinderea cifrelor bare negroundate,
filtrul de zgomot (procente, numere mici de proză, whitelist), și kill-switch-ul fail-open.
"""

from types import SimpleNamespace

from src.worker.stages import agent as ag
from src.worker.stages.agent import _allowed_numbers, _bad_bare_numbers, _valid

PRODUCTS = [
    {
        "id": "p1",
        "name": "Crema A",
        "price": 82.99,
        "url": "https://shop/p1",
        "rating": 4.6,
        "stock": 12,
    },
    {
        "id": "p2",
        "name": "Ser B",
        "price": 120.50,
        "url": "https://shop/p2",
        "rating": 4.3,
        "stock": 5,
    },
]


def _enable(monkeypatch, on=True):
    monkeypatch.setattr(
        ag, "get_settings", lambda: SimpleNamespace(validator_bare_numbers_enabled=on)
    )


# --- _allowed_numbers --------------------------------------------------------


def test_allowed_numbers_extracts_product_fields():
    nums = _allowed_numbers(PRODUCTS, set())
    assert {82.99, 120.5, 4.6, 4.3, 12.0, 5.0} <= nums


def test_allowed_numbers_includes_variants_and_grounded():
    prods = [{"id": "p", "price": 10.0, "variants": [{"price": 9.5, "stock": 3}]}]
    nums = _allowed_numbers(prods, {99.0})
    assert {10.0, 9.5, 3.0, 99.0} <= nums


# --- Happy path (fără fals-pozitiv) ------------------------------------------


def test_price_with_currency_valid(monkeypatch):
    _enable(monkeypatch)
    assert _valid("Îți recomand Crema — 82.99 lei, in stock", PRODUCTS) is True


def test_real_rating_bare_valid(monkeypatch):
    _enable(monkeypatch)
    # 4.6 = rating retrievat (∈ _allowed_numbers), fără valută → acceptat
    assert _valid("Are rating 4.6 și mai sunt pe stoc", PRODUCTS) is True


# --- Edge: zgomot de proză ---------------------------------------------------


def test_small_prose_numbers_ignored(monkeypatch):
    _enable(monkeypatch)
    # 1 cifră („top 3", „pasul 2") → sub pragul regexului (≥2 cifre sau zecimale)
    assert _valid("Îți arăt top 3 produse, pasul 2 e simplu", PRODUCTS) is True


def test_percentages_not_caught(monkeypatch):
    _enable(monkeypatch)
    # „20%" exclus de lookahead (?![\\w%]) → e treaba NX-30 (promoții), nu a acestui validator
    assert _valid("Reducere 20% la a doua", PRODUCTS) is True


def test_safe_whitelist_hours(monkeypatch):
    _enable(monkeypatch)
    assert _valid("Revin în 24 de ore", PRODUCTS) is True  # 24 ∈ _SAFE_BARE


# --- Failure: cifre bare halucinate ------------------------------------------


def test_bare_price_hallucinated(monkeypatch):
    _enable(monkeypatch)
    assert _valid("Crema costă 89, super preț", PRODUCTS) is False  # 89 ∉ retrieval, fără valută
    assert _bad_bare_numbers("Crema costă 89, super preț", PRODUCTS, set()) == [89.0]


def test_bare_stock_hallucinated(monkeypatch):
    _enable(monkeypatch)
    # stocul retrievat e 12/5 → „47" e inventat
    assert _valid("Mai ai 47 bucăți pe stoc", PRODUCTS) is False


def test_grounded_sum_accepted(monkeypatch):
    _enable(monkeypatch)
    # 350 nu e în produse, DAR e o sumă grounded (total comandă/checkout) → acceptat
    assert _valid("Total 350", PRODUCTS, allowed_prices={350.0}) is True


# --- Kill-switch fail-open ---------------------------------------------------


def test_kill_switch_off_disables_bare_check(monkeypatch):
    _enable(monkeypatch, on=False)
    # cu kill-switch off, comportamentul revine la cel pre-NX-91 (doar preț cu valută + link)
    assert _valid("Crema costă 89, super preț", PRODUCTS) is True
    assert _bad_bare_numbers("Crema costă 89", PRODUCTS, set()) == []
