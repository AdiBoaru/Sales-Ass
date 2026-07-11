import json
from pathlib import Path

from src.evals.web_response import validate_web_payload

FIXTURES = Path(__file__).parent / "fixtures" / "web_response" / "payloads.json"


SOURCE_PRODUCTS = {
    "p1": {
        "product_id": "p1",
        "name": "Fluid X",
        "price": 58.99,
        "url": "https://shop.example/p/fluid-x",
        "availability": "in_stock",
    },
    "p2": {
        "product_id": "p2",
        "name": "Crema Y",
        "price": 88.99,
        "url": "https://shop.example/p/crema-y",
    },
}


def _fixtures() -> dict:
    return json.loads(FIXTURES.read_text(encoding="utf-8"))["fixtures"]


def test_web_response_fixtures_cover_main_payload_shapes():
    fx = _fixtures()

    assert set(fx) == {
        "text_only",
        "products",
        "offer",
        "comparison",
        "no_match",
        "fallback_error",
        "rate_limit",
    }


def test_web_response_fixtures_are_contract_valid():
    for name, payload in _fixtures().items():
        result = validate_web_payload(
            payload, source_products=SOURCE_PRODUCTS, allow_delivery_claim=name == "offer"
        )
        assert result.passed, f"{name}: {result.failures}"


def test_web_response_checker_catches_invented_price():
    payload = {
        "content": "Iti recomand Fluid X la 999 lei.",
        "products": [{"product_id": "p1", "name": "Fluid X", "price": 58.99}],
        "suggestions": [],
    }

    result = validate_web_payload(payload, source_products=SOURCE_PRODUCTS)

    assert result.passed is False
    assert any("content price" in f for f in result.failures)


def test_web_response_checker_catches_unknown_product_id():
    payload = {
        "content": "Iti recomand Ser Fantoma la 42 lei.",
        "products": [{"product_id": "ghost", "name": "Ser Fantoma", "price": 42.0}],
        "suggestions": [],
    }

    result = validate_web_payload(payload, source_products=SOURCE_PRODUCTS)

    assert result.passed is False
    assert any("not in source" in f for f in result.failures)


def test_web_response_checker_accepts_variant_payload_with_oos_stock():
    payload = {
        "content": "Iti recomand Fluid X.",
        "products": [
            {
                "product_id": "p1",
                "name": "Fluid X",
                "price": 58.99,
                "variants": [
                    {"variant_id": "v07", "label": "Medium Warm 07", "price": 58.99, "stock": 3},
                    {"variant_id": "v08", "label": "Tan Warm 08", "price": 58.99, "stock": 0},
                ],
            }
        ],
        "suggestions": [],
    }

    result = validate_web_payload(payload, source_products=SOURCE_PRODUCTS)

    assert result.passed is True


def test_web_response_checker_catches_broken_variant_payload():
    payload = {
        "content": "Iti recomand Fluid X.",
        "products": [
            {
                "product_id": "p1",
                "name": "Fluid X",
                "price": 58.99,
                "variants": [{"label": "Tan Warm 08", "stock": -1}],
            }
        ],
        "suggestions": [],
    }

    result = validate_web_payload(payload, source_products=SOURCE_PRODUCTS)

    assert result.passed is False
    assert any("missing variant_id" in f for f in result.failures)
    assert any("stock must be" in f for f in result.failures)


def test_web_response_checker_catches_empty_or_invented_url():
    payload = {
        "content": "Vezi produsul aici: https://evil.example/p1",
        "products": [
            {
                "product_id": "p1",
                "name": "Fluid X",
                "price": 58.99,
                "url": "not-a-url",
            }
        ],
        "suggestions": [],
    }

    result = validate_web_payload(payload, source_products=SOURCE_PRODUCTS)

    assert result.passed is False
    assert any("invalid url" in f for f in result.failures)
    assert any("content URL" in f for f in result.failures)


def test_web_response_checker_requires_mentioned_product_to_be_in_cards():
    payload = {
        "content": "Fluid X e potrivit, iar Crema Y poate fi alternativa.",
        "products": [{"product_id": "p1", "name": "Fluid X", "price": 58.99}],
        "suggestions": [],
    }

    result = validate_web_payload(payload, source_products=SOURCE_PRODUCTS)

    assert result.passed is False
    assert any("mentions product" in f for f in result.failures)


def test_web_response_checker_catches_unsourced_stock_and_delivery_claims():
    stock_payload = {
        "content": "Fluid X este in stoc.",
        "products": [{"product_id": "p1", "name": "Fluid X", "price": 58.99}],
        "suggestions": [],
    }
    delivery_payload = {
        "content": "Livrare maine pentru Fluid X.",
        "products": [{"product_id": "p1", "name": "Fluid X", "price": 58.99}],
        "suggestions": [],
    }
    # livrare GENERICĂ (fără reper de timp) NU e un claim factual → nu se flageaza
    generic_delivery_payload = {
        "content": "Avem livrare rapida si transport gratuit.",
        "products": [{"product_id": "p1", "name": "Fluid X", "price": 58.99}],
        "suggestions": [],
    }

    stock_ok = validate_web_payload(stock_payload, source_products=SOURCE_PRODUCTS)
    delivery_bad = validate_web_payload(delivery_payload, source_products=SOURCE_PRODUCTS)
    delivery_ok = validate_web_payload(
        delivery_payload, source_products=SOURCE_PRODUCTS, allow_delivery_claim=True
    )
    generic_ok = validate_web_payload(generic_delivery_payload, source_products=SOURCE_PRODUCTS)

    assert stock_ok.passed is True
    assert delivery_bad.passed is False
    assert any("delivery ETA claim" in f for f in delivery_bad.failures)
    assert delivery_ok.passed is True
    assert generic_ok.passed is True


def test_web_response_checker_catches_broken_comparison_shape():
    payload = {
        "content": "Compar Fluid X cu Crema Y.",
        "products": [
            {"product_id": "p1", "name": "Fluid X", "price": 58.99},
            {"product_id": "p2", "name": "Crema Y", "price": 88.99},
        ],
        "suggestions": [],
        "comparison": {
            "columns": [
                {"product_id": "p1", "name": "Fluid X", "price": 58.99},
                {"product_id": "p2", "name": "Crema Y", "price": 88.99},
            ],
            "rows": [{"label": "Pret", "values": ["58.99 lei"]}],
        },
    }

    result = validate_web_payload(payload, source_products=SOURCE_PRODUCTS)

    assert result.passed is False
    assert any("values length" in f for f in result.failures)


def test_web_response_checker_catches_invented_price_in_text_only_reply():
    # fara carduri, dar cu sursa (ground truth) → pretul inventat din text tot e prins
    payload = {
        "content": "Iti recomand ceva la 999 lei.",
        "products": [],
        "suggestions": [],
    }

    result = validate_web_payload(payload, source_products=SOURCE_PRODUCTS)

    assert result.passed is False
    assert any("content price" in f for f in result.failures)


def test_web_response_checker_allows_empty_content_for_silent_handoff():
    # tacere intentionata (handoff / degradare) → payload gol e valid cand allow_empty=True
    payload = {"content": "", "products": [], "suggestions": []}

    strict = validate_web_payload(payload)
    lenient = validate_web_payload(payload, allow_empty=True)

    assert strict.passed is False
    assert any("content is empty" in f for f in strict.failures)
    assert lenient.passed is True
