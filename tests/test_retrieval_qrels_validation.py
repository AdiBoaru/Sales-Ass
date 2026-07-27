from src.evals.retrieval.schema import Provenance, QrelJudgment, QrelsQuery, QrelsSet, Relevance
from src.evals.retrieval.validation import integrity_issues


def _query(**overrides):
    values = {
        "id": "q-1",
        "query": "ser pentru ten sensibil",
        "provenance": Provenance.real_sanitized,
        "category": "seruri",
        "catalog_version": "catalog-v1",
        "human_verified": True,
        "judgments": [QrelJudgment(product_id="product-1", relevance=Relevance.ideal)],
    }
    values.update(overrides)
    return QrelsQuery(**values)


def test_strict_integrity_accepts_verified_sanitized_qrels():
    qset = QrelsSet(business_id="business-1", queries=[_query()])

    assert integrity_issues(qset, require_human_verified=True, require_real_per_category=True) == []


def test_integrity_rejects_pii_and_duplicate_judgments():
    qset = QrelsSet(
        business_id="business-1",
        queries=[
            _query(
                query="suna-ma la 0712345678 pentru un ser",
                judgments=[
                    QrelJudgment(product_id="product-1", relevance=Relevance.ideal),
                    QrelJudgment(product_id="product-1", relevance=Relevance.relevant),
                ],
            )
        ],
    )

    issues = integrity_issues(qset)

    assert any("PII" in issue for issue in issues)
    assert any("duplicat" in issue for issue in issues)


def test_strict_integrity_requires_real_human_verified_query_per_category():
    qset = QrelsSet(
        business_id="business-1",
        queries=[
            _query(
                provenance=Provenance.synthetic,
                human_verified=False,
                category="creme",
            )
        ],
    )

    issues = integrity_issues(qset, require_human_verified=True, require_real_per_category=True)

    assert any("human_verified" in issue for issue in issues)
    assert any("real_sanitized" in issue for issue in issues)
