"""NX-191 — parserul de config comercial e FAIL-SAFE: fiecare cifră de aici ajunge într-o frază
spusă clientului, deci o cheie stricată trebuie să cadă pe implicit, nu să arunce pe calea caldă."""

from __future__ import annotations

from src.commerce.config import load_commerce_config


def test_config_demo_complet():
    cfg = load_commerce_config(
        {
            "prices_include_vat": True,
            "shipping": {
                "cutoff_hour": 14,
                "working_days": [1, 2, 3, 4, 5],
                "cost": 19.99,
                "free_threshold": 199.0,
                "courier": "Cargus",
                "class_days": {"standard": [2, 4], "supplier": [5, 7]},
            },
            "returns": {"days": 14, "from": "delivery"},
            "payment": {"methods": ["card", "ramburs"]},
        }
    )
    assert cfg.prices_include_vat is True
    assert cfg.shipping.cutoff_hour == 14
    assert cfg.shipping.working_days == (1, 2, 3, 4, 5)
    assert cfg.shipping.cost == 19.99
    assert cfg.shipping.free_threshold == 199.0
    assert cfg.shipping.class_days["supplier"] == (5, 7)
    assert cfg.returns.days == 14 and cfg.returns.from_event == "delivery"
    assert cfg.payment.methods == ("card", "ramburs")


def test_settings_gol_nu_promite_nimic():
    """Fără config, botul NU trebuie să promită livrare a doua zi și nu cunoaște praguri."""
    cfg = load_commerce_config({})
    assert cfg.shipping.promises_next_day is False
    assert cfg.shipping.cutoff_hour is None
    assert cfg.shipping.cost is None
    assert cfg.shipping.free_threshold is None
    assert cfg.returns.days is None
    assert cfg.payment.methods == ()


def test_settings_none_si_tip_gresit():
    for bad in (None, [], "nope", 42):
        cfg = load_commerce_config(bad)  # type: ignore[arg-type]
        assert cfg.shipping.working_days == (1, 2, 3, 4, 5)
        assert cfg.shipping.promises_next_day is False


def test_chei_stricate_cad_pe_implicit():
    cfg = load_commerce_config(
        {
            "shipping": {
                "cutoff_hour": "paisprezece",  # ne-numeric
                "working_days": "luni-vineri",  # nu e listă
                "cost": -5,  # negativ
                "free_threshold": None,
                "class_days": {"standard": [4, 2], "supplier": "nope"},  # lo>hi / tip greșit
            },
            "returns": {"days": "paispe", "from": "cumva"},
            "payment": {"methods": [1, "", "card"]},
        }
    )
    assert cfg.shipping.cutoff_hour is None
    assert cfg.shipping.working_days == (1, 2, 3, 4, 5)
    assert cfg.shipping.cost is None
    assert cfg.shipping.class_days["standard"] == (2, 4)  # implicitul, nu [4,2]
    assert cfg.shipping.class_days["supplier"] == (5, 7)
    assert cfg.returns.days is None
    assert cfg.returns.from_event == "delivery"
    assert cfg.payment.methods == ("card",)


def test_ora_limita_in_afara_intervalului():
    assert load_commerce_config({"shipping": {"cutoff_hour": 25}}).shipping.cutoff_hour is None
    assert load_commerce_config({"shipping": {"cutoff_hour": -1}}).shipping.cutoff_hour is None
    assert load_commerce_config({"shipping": {"cutoff_hour": 0}}).shipping.cutoff_hour == 0


def test_zile_lucratoare_filtrate():
    cfg = load_commerce_config({"shipping": {"working_days": [1, 2, 9, "x", 6]}})
    assert cfg.shipping.working_days == (1, 2, 6)
