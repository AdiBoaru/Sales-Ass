import pytest

from src.safety.external_data import external_query_text


@pytest.mark.parametrize(
    "query",
    [
        "ser 0712 345 678 pentru ten gras",
        "scrie la ion@example.ro pentru o crema",
        "iban ro49aaaa1b31007593840000 crema",
        "strada florilor 5 caut un ser",
        "ma numesc ion popescu caut o crema",
        "sunt insarcinata caut o crema",
    ],
)
def test_external_query_rejects_sensitive_free_text(query):
    assert external_query_text(query) is None


def test_external_query_normalizes_and_allows_product_intent_only():
    assert (
        external_query_text("Cremă MATIFIANTĂ pentru ten gras")
        == "crema matifianta pentru ten gras"
    )
