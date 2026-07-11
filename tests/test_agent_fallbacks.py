"""NX-143 — teste pentru replicile pure din `src/agent/fallbacks.py`.

Mesaje per-locale (niciodată tăcere, P6) + utilitare de produse. Zero LLM/DB.
"""

from src.agent.fallbacks import (
    _card_products,
    _cart_confirm_msg,
    _cheapest_already_msg,
    _dedupe,
    _deterministic_reply,
    _link_lead,
    _no_more_msg,
    _view_label,
)


def test_per_locale_fallback_messages():
    assert "ieftin" in _cheapest_already_msg("ro").lower()
    assert "cheapest" in _cheapest_already_msg("en").lower()
    assert _cheapest_already_msg("hu")  # HU are text
    # locale necunoscut → RO (fără tăcere)
    assert _cheapest_already_msg("de") == _cheapest_already_msg("ro")
    assert _cheapest_already_msg(None) == _cheapest_already_msg("ro")


def test_no_more_and_view_label_localized():
    assert _no_more_msg("en") != _no_more_msg("ro")
    assert _view_label("hu") and _view_label("en") == "View product"


def test_link_lead_singular_vs_plural():
    assert _link_lead("ro", many=False) != _link_lead("ro", many=True)


def test_cart_confirm_interpolates_name():
    msg = _cart_confirm_msg({"name": "Crema X"}, "ro")
    assert "Crema X" in msg
    # nume lipsă → fallback fără crash
    assert _cart_confirm_msg({}, "ro")


def test_deterministic_reply_lists_max_three():
    prods = [{"name": f"P{i}", "price": float(i)} for i in range(5)]
    reply = _deterministic_reply(prods)
    assert reply.count("•") == 3  # cap la 3


def test_card_products_shape_and_cap():
    prods = [
        {"id": f"p{i}", "name": f"N{i}", "price": float(i), "url": None, "image": None}
        for i in range(6)
    ]
    cards = _card_products(prods, n=4)
    assert len(cards) == 4
    assert set(cards[0]) == {"product_id", "name", "price", "url", "image"}


def test_card_products_include_variant_payload_with_oos_stock():
    cards = _card_products(
        [
            {
                "id": "p1",
                "name": "Foundation",
                "price": 89.0,
                "url": None,
                "image": None,
                "variants": [
                    {
                        "id": "v07",
                        "label": "Medium Warm 07",
                        "price": 89.0,
                        "stock": 8,
                        "color_hex": "#C89463",
                        "attributes": {"shade": "07", "undertone": "warm", "depth": "medium"},
                    },
                    {
                        "variant_id": "v08",
                        "label": "Tan Warm 08",
                        "price": 89.0,
                        "stock": 0,
                        "color_hex": "#A66B42",
                        "attributes": {"shade": "08", "undertone": "warm", "depth": "tan"},
                    },
                ],
            }
        ]
    )
    variants = cards[0]["variants"]
    assert variants[0]["variant_id"] == "v07"
    assert variants[0]["attributes"] == {"shade": "07", "undertone": "warm", "depth": "medium"}
    assert variants[1]["label"] == "Tan Warm 08" and variants[1]["stock"] == 0


def test_dedupe_keeps_order_and_caps():
    prods = [{"id": "a"}, {"id": "a"}, {"id": "b"}, {"id": "c"}]
    out = _dedupe(prods, cap=6)
    assert [p["id"] for p in out] == ["a", "b", "c"]
    assert len(_dedupe([{"id": str(i)} for i in range(10)], cap=6)) == 6
