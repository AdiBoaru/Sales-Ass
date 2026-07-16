"""NX-171a — randarea preț/unitate în _variant_view + validarea GTIN la seed."""

from scripts.seed_catalog_v2 import clean_gtin
from src.tools.catalog_tools import _variant_view


def test_clean_gtin_invalid_becomes_none():
    assert clean_gtin("4006381333931") == "4006381333931"  # GS1 valid → păstrat
    assert clean_gtin("BAD-123") is None  # invalid → NULL (nu scriem cod fals)
    assert clean_gtin("4006-3813-3393-1") is None  # cratime → invalid
    assert clean_gtin(None) is None


def _v(**over):
    v = {"id": "1", "variant_id": "1", "label": "Standard", "price": 25.0, "stock": 10}
    v.update(over)
    return v


def test_variant_view_renders_net_content_and_price_per_unit():
    s = _variant_view(
        [_v(net_content_value=50, net_content_unit="ml", price_per_unit=50.0)], limit=4
    )
    assert "50ml" in s  # gramaj
    assert "50.00 lei/100ml" in s  # preț/unitate (bază ml)


def test_variant_view_price_per_unit_grams():
    s = _variant_view(
        [_v(net_content_value=200, net_content_unit="g", price_per_unit=12.5)], limit=4
    )
    assert "200g" in s and "12.50 lei/100g" in s


def test_variant_view_without_net_content():
    s = _variant_view([_v(label="Bej 01")], limit=4)
    assert "lei/100" not in s  # fără gramaj → fără preț/unitate (degradează lin)


def test_variant_view_buc_no_price_per_unit():
    # bucăți: gramaj afișat dar FĂRĂ preț/unitate (price_per_unit NULL din DB)
    s = _variant_view(
        [_v(net_content_value=5, net_content_unit="buc", price_per_unit=None)], limit=4
    )
    assert "lei/100" not in s
