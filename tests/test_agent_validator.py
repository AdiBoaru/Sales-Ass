"""NX-142 — teste dedicate pentru `src/agent/validator.py` (validatorul de proză extras).

Cluster PUR, zero LLM/DB. Kill-switch-urile se citesc din `validator.get_settings` → le pate-uim
acolo. Acoperă: preț inventat → invalid, link din afara catalogului → invalid, claim medical →
invalid (kill-switch OFF → permis), stoc inexistent → invalid, plus `validate_prose` (ok + reasons)
și re-exportul backward-compat din `src.worker.stages.agent`.
"""

from types import SimpleNamespace

from src.agent import validator as val
from src.agent.validator import ValidationResult, _valid, validate_prose

PRODUCTS = [
    {
        "id": "p1",
        "name": "Crema A",
        "price": 82.99,
        "url": "https://shop/p1",
        "rating": 4.6,
        "stock": 12,
        "availability": "in_stock",
    },
    {
        "id": "p2",
        "name": "Ser B",
        "price": 120.50,
        "url": "https://shop/p2",
        "rating": 4.3,
        "stock": 5,
        "availability": "in_stock",
    },
]
_OUT_STOCK = [{"id": "p9", "name": "Crema Z", "price": 60.0, "availability": "out_of_stock"}]


def _settings(monkeypatch, *, bare=True, claims=True, stock=True, safety=True):
    monkeypatch.setattr(
        val,
        "get_settings",
        lambda: SimpleNamespace(
            validator_bare_numbers_enabled=bare,
            validator_claims_enabled=claims,
            validator_stock_claims_enabled=stock,
            safety_medical_guardrail_enabled=safety,
        ),
    )


# --- Happy ------------------------------------------------------------------ #


def test_validate_prose_ok_grounded_prices(monkeypatch):
    _settings(monkeypatch)
    res = validate_prose("Îți recomand Crema A — 82.99 lei", products=PRODUCTS)
    assert isinstance(res, ValidationResult)
    assert res.ok is True
    assert res.reasons == []


def test_grounded_bare_number_ok(monkeypatch):
    # rating 4.6 ∈ produse → număr fără valută grounded → nu e respins
    _settings(monkeypatch, claims=False, stock=False)
    assert validate_prose("Are rating 4.6", products=PRODUCTS).ok is True


# --- Edge ------------------------------------------------------------------- #


def test_percentage_not_flagged(monkeypatch):
    _settings(monkeypatch, claims=False, stock=False)
    assert validate_prose("Reducere 20% la a doua", products=PRODUCTS).ok is True


def test_safety_kill_switch_off_allows_medical_claim(monkeypatch):
    _settings(monkeypatch, safety=False)
    assert validate_prose("Crema A tratează acneea.", products=PRODUCTS).ok is True
    assert _valid("Crema A tratează acneea.", PRODUCTS) is True


# --- Failure ---------------------------------------------------------------- #


def test_invented_price_and_link_two_reasons(monkeypatch):
    _settings(monkeypatch, bare=False, claims=False, stock=False)
    res = validate_prose("Costă 999 lei, vezi https://evil/x", products=PRODUCTS)
    assert res.ok is False
    assert set(res.reasons) == {"ungrounded_price", "invented_link"}


def test_invented_price_invalid(monkeypatch):
    _settings(monkeypatch, bare=False, claims=False, stock=False)
    assert _valid("Crema costă 999 lei", PRODUCTS) is False


def test_link_outside_catalog_invalid(monkeypatch):
    _settings(monkeypatch, bare=False, claims=False, stock=False)
    assert _valid("Link inventat https://evil/x", PRODUCTS) is False


def test_medical_claim_invalid_when_switch_on(monkeypatch):
    _settings(monkeypatch, bare=False, claims=False, stock=False, safety=True)
    res = validate_prose("Crema A tratează acneea.", products=PRODUCTS)
    assert res.ok is False and res.reasons == ["medical_claim"]


def test_stock_claim_on_out_of_stock_invalid(monkeypatch):
    _settings(monkeypatch)
    res = validate_prose("Crema asta e pe stoc, ți-o recomand", products=_OUT_STOCK)
    assert res.ok is False and "stock_claim" in res.reasons
    assert _valid("Crema asta e pe stoc", _OUT_STOCK) is False


def test_ungrounded_bare_number_flagged(monkeypatch):
    _settings(monkeypatch, claims=False, stock=False)
    res = validate_prose("Mai ai 47 bucăți pe stoc", products=PRODUCTS)
    assert res.ok is False and "bare_number" in res.reasons


# --- ORDER path (check_bare/check_claims=False) ----------------------------- #


def test_order_path_skips_bare_and_claims(monkeypatch):
    _settings(monkeypatch)
    # fapte de livrare grounded, fără preț inventat → valid pe ruta ORDER
    ok = validate_prose(
        "Comanda ta, total 247.50 lei",
        products=[],
        grounded_prices={247.50},
        check_bare=False,
        check_claims=False,
    ).ok
    assert ok is True


# --- Backward-compat: funcțiile rămân exportate din agent.py ----------------- #


def test_reexported_from_agent_stage():
    from src.worker.stages.agent import (  # noqa: F401
        _allowed_numbers,
        _allowed_prices,
        _bad_bare_numbers,
        _bare_numbers_ok,
        _budget,
        _claims_ok,
        _links_ok,
        _prices_ok,
        _safety_ok,
        _stock_available,
        _stock_claim_ok,
        _valid,
        validate_prose,
    )

    assert _budget("sub 150 lei") == 150.0
