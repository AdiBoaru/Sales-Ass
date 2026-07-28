import pytest
from pydantic import ValidationError

from src.evals.retrieval.schema import Provenance, QrelJudgment, QrelsQuery, QrelsSet, Relevance
from src.evals.retrieval.splits import Split, assign_split
from src.evals.retrieval.validation import MIN_HOLDOUT_SLICE, integrity_issues


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


def _id_in(split: Split, taken: set[str]) -> str:
    """Un id care aterizează determinist în felia cerută (split-urile vin din hash pe id)."""
    for n in range(10_000):
        candidate = f"q-{n}"
        if candidate not in taken and assign_split(candidate) is split:
            taken.add(candidate)
            return candidate
    raise AssertionError(f"niciun id pentru {split}")


def test_same_query_text_in_two_splits_is_contamination():
    """Un query NU poate fi în două felii (atribuirea e determinist derivată din id). Ce POATE, în
    schimb, e ca același text să primească două id-uri și să ajungă în tuning ȘI în holdout —
    atunci gate-ul măsoară pe ce a văzut deja la tuning, iar holdout-ul nu mai e independent."""
    taken: set[str] = set()
    tuning_id = _id_in(Split.tuning, taken)
    holdout_id = _id_in(Split.holdout_h2, taken)
    qset = QrelsSet(
        business_id="business-1",
        queries=[
            _query(id=tuning_id, query="ser pentru ten sensibil"),
            _query(id=holdout_id, query="Ser pentru TEN sensibil "),  # parafrază după normalizare
        ],
    )

    issues = integrity_issues(qset)

    assert any("holdout contaminat" in issue for issue in issues)


def test_duplicate_ids_are_rejected_by_the_schema_not_the_validator():
    """Verificarea trăiește într-UN singur loc: `QrelsSet` respinge duplicatele la construcție, deci
    `integrity_issues` nu are ce să mai verifice — o a doua copie ar fi cod care nu poate rula."""
    with pytest.raises(ValidationError, match="id-uri de query duplicate"):
        QrelsSet(business_id="b", queries=[_query(id="q-1"), _query(id="q-1", query="altceva")])


def test_judgments_must_reference_products_that_exist():
    """Un qrels care judecă produse inexistente produce metrici care arată bine și nu înseamnă
    nimic: recall-ul se calculează contra unui adevăr care nu mai e în catalog."""
    qset = QrelsSet(business_id="b", queries=[_query()])

    assert integrity_issues(qset, catalog_product_ids=["product-1"]) == []
    assert any(
        "absente din catalog" in issue
        for issue in integrity_issues(qset, catalog_product_ids=["altul"])
    )


def test_holdout_slices_too_small_to_measure_are_rejected():
    """Sub prag, un singur query greșit mișcă metrica cu zeci de puncte — „a trecut gate-ul"
    devine zgomot prezentat ca dovadă."""
    taken: set[str] = set()
    qset = QrelsSet(
        business_id="b",
        queries=[_query(id=_id_in(Split.holdout_h1, taken), query=f"q {i}") for i in range(3)],
    )

    issues = integrity_issues(qset, require_split_sizes=True)

    assert any(f"sub pragul de {MIN_HOLDOUT_SLICE}" in issue for issue in issues)
    assert any("holdout_h2" in issue for issue in issues)  # feliile goale se raportează, nu se sar
