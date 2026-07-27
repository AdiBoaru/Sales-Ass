from src.domain.search_documents import DOCUMENT_VERSION, build_search_artifacts


def _product():
    return {
        "slug": "ser-niacinamida",
        "name": "Velora Ser Niacinamidă",
        "brandSlug": "velora",
        "primaryCategorySlug": "seruri-pentru-ten",
        "shortDescription": "Ser lejer pentru aspect uniform.",
        "description": "Niacinamida ajută la aspectul echilibrat al tenului.",
        "attributes": {
            "concerns": ["oily"],
            "suitable_for": ["oily"],
            "key_ingredients": ["niacinamidă"],
            "free_of": ["parfum"],
            "best_for": "ten mixt",
            "claim_provenance": [
                {
                    "kind": "ingredient",
                    "value": "niacinamidă",
                    "source": "INCI",
                    "source_ref": "etichetă",
                    "verified_at": "2026-07-27",
                }
            ],
            "not_recommended_for": [
                {"value": "sensitive", "level": "soft", "reason": "poate irita"}
            ],
        },
    }


def test_artifacts_are_deterministic_and_versioned():
    product = _product()
    first = build_search_artifacts(product, business_id="biz", locale="ro")
    second = build_search_artifacts(product, business_id="biz", locale="ro")
    assert first == second
    assert first.document_version == DOCUMENT_VERSION
    assert first.schema_version == 1
    assert first.content_hash


def test_positive_document_includes_confirmed_absence_but_never_negative_warning():
    artifacts = build_search_artifacts(_product(), business_id="biz", locale="ro")
    assert "fără parfum adăugat" in artifacts.positive_search_document
    assert "sensitive" not in artifacts.positive_search_document
    assert "contraindicație" not in artifacts.positive_search_document
    assert artifacts.evidence_chunks[-1].role == "warning"
    assert "sensitive" in artifacts.evidence_chunks[-1].text


def test_fts_weights_keep_identity_above_catalog_description():
    artifacts = build_search_artifacts(_product(), business_id="biz", locale="ro")
    assert artifacts.fts_document.a[:2] == ("Velora Ser Niacinamidă", "velora")
    assert "niacinamidă" in artifacts.fts_document.b
    assert artifacts.fts_document.c == (
        "Ser lejer pentru aspect uniform.",
        "Niacinamida ajută la aspectul echilibrat al tenului.",
    )


def test_hash_changes_only_when_artifact_content_changes():
    product = _product()
    before = build_search_artifacts(product, business_id="biz", locale="ro")
    product["attributes"]["free_of"] = ["alcool"]
    after = build_search_artifacts(product, business_id="biz", locale="ro")
    assert before.content_hash != after.content_hash
