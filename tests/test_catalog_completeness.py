"""NX-206 — poarta de completitudine: audit v3 PLUS contractul tipizat NX-205."""

from scripts.catalog_completeness import completeness_report, gate_violations


def _data(product):
    return {
        "brands": [{"slug": "velora", "name": "Velora"}],
        "categories": [
            {"slug": "machiaj", "name": "Machiaj"},
            {"slug": "fond-de-ten", "name": "Fond", "parentSlug": "machiaj"},
        ],
        "products": [product],
    }


def _foundation(slug="fond-ok"):
    return {
        "slug": slug,
        "name": "Velora Perfect Fond de ten",
        "brandSlug": "velora",
        "primaryCategorySlug": "fond-de-ten",
        "price": 59.9,
        "ai_summary": "Fond matifiant pentru ten gras.",
        "attributes": {
            "finish": "matte",
            "coverage": "full",
            "suitable_for": ["oily"],
            "texture": "fluid",
            "best_for": "ten gras",
            "concerns": ["oily"],
        },
        "variants": [{"label": "Bej 01", "sku": "FP-01", "price": 59.9, "stock": 10}],
    }


def test_complete_product_passes_and_is_counted_per_category():
    report = completeness_report(_data(_foundation()))
    assert report["violations"] == []
    assert report["categories"]["fond-de-ten"] == {
        "total": 1,
        "passed": 1,
        "blocked": 0,
        "issues": {},
        "blocked_products": [],
    }


def test_contract_contradiction_blocks_publication_even_when_v3_cannot_see_it():
    product = _foundation()
    product["attributes"]["not_recommended_for"] = [
        {"value": "oily", "level": "soft", "reason": "incompatibil"}
    ]
    report = completeness_report(_data(product))
    assert any(f["code"] == "facts_contract" for f in report["violations"])
    row = report["categories"]["fond-de-ten"]
    assert row["passed"] == 0 and row["blocked"] == 1
    assert row["issues"] == {"facts_contract": 1}


def test_existing_v3_violation_is_preserved_in_gate_and_report():
    product = _foundation()
    product["ai_summary"] = "Fond cu retinol pentru ten gras."
    report = completeness_report(_data(product))
    assert any(f["code"] == "ai_summary_unfounded" for f in report["violations"])
    assert gate_violations(_data(product)) == report["violations"]
    assert report["categories"]["fond-de-ten"]["issues"]["ai_summary_unfounded"] == 1


def test_global_schema_failure_blocks_every_product_fail_closed():
    product = _foundation()
    data = _data(product)
    del data["brands"]
    report = completeness_report(data)
    assert report["global_violations"]
    row = report["categories"]["fond-de-ten"]
    assert row["passed"] == 0 and row["blocked"] == 1
