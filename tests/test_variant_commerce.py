"""NX-171a — randarea preț/unitate în _variant_view (coloanele comerciale de variantă)."""

from src.tools.catalog_tools import _variant_view


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
