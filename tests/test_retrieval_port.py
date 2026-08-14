"""NX-238 — contractul `RetrievalPort`/`RetrievalBundle`: invarianți, nu doar forme."""

from __future__ import annotations

import time

import pytest

from src.agent.match_gate import ConstraintResult, FacetCoverage
from src.domain.search_entities import EvidenceReference
from src.retrieval.port import (
    BUNDLE_SCHEMA_VERSION,
    EvidenceBundle,
    RetrievalBundle,
    RetrievalCandidate,
    RetrievalDeadline,
    RetrievalError,
    RetrievalTiming,
    facets_by_key,
    query_count_bucket,
)


def _candidate(product_id: str, match_class: str, rank: int = 0) -> RetrievalCandidate:
    return RetrievalCandidate(product_id=product_id, rank=rank, match_class=match_class)


def test_bundle_partitions_candidates_into_disjoint_sets():
    bundle = RetrievalBundle(
        provider_version="test.v1",
        candidates=(
            _candidate("p-1", "exact", 0),
            _candidate("p-2", "alternative", 1),
            _candidate("p-3", "rejected", 2),
            _candidate("p-4", "exact", 3),
        ),
    )
    assert bundle.exact_ids == ("p-1", "p-4")
    assert bundle.alternative_ids == ("p-2",)
    assert bundle.rejected_ids == ("p-3",)
    # Disjuncte: niciun id nu apare în două mulțimi.
    assert not set(bundle.exact_ids) & set(bundle.alternative_ids) & set(bundle.rejected_ids)
    assert bundle.schema_version == BUNDLE_SCHEMA_VERSION


def test_enforced_bundle_cannot_contain_a_rejected_candidate():
    """Invariantul central: „am aplicat hard constraints" + „un rejected în rezultat" = imposibil.

    Cele două nu pot fi ambele adevărate; dacă ajung să fie, un rerank a reînviat un produs
    contrazis de date, iar bundle-ul minte despre propria lui garanție.
    """
    with pytest.raises(RetrievalError, match="rejected"):
        RetrievalBundle(
            provider_version="test.v1",
            candidates=(_candidate("p-1", "exact"), _candidate("p-bad", "rejected", 1)),
            constraints_enforced=True,
        )


def test_annotated_bundle_may_carry_rejected_candidates():
    """Traseul live ADNOTEAZĂ fără să excludă — asta e legal, atâta timp cât nu pretinde altceva."""
    bundle = RetrievalBundle(
        provider_version="current_live.v1",
        candidates=(_candidate("p-bad", "rejected"),),
        constraints_enforced=False,
    )
    assert bundle.rejected_ids == ("p-bad",)
    assert bundle.constraints_enforced is False


def test_unknown_is_not_mismatch_in_the_contract():
    """UNKNOWN → `alternative` + missing_information; NU `rejected`, NU `exact` (D7)."""
    bundle = RetrievalBundle(
        provider_version="test.v1",
        candidates=(
            RetrievalCandidate(
                product_id="p-1",
                rank=0,
                match_class="alternative",
                constraint_results=(
                    ConstraintResult(facet="concern", status="UNKNOWN", strength="hard"),
                ),
            ),
        ),
        coverage=(FacetCoverage(facet="concern", match=0, mismatch=0, unknown=1),),
        missing_information=("concern",),
    )
    assert bundle.alternative_ids == ("p-1",)
    assert bundle.rejected_ids == ()
    assert bundle.exact_ids == ()
    assert bundle.missing_information == ("concern",)


def test_safe_dict_carries_codes_and_numbers_but_no_free_text():
    bundle = RetrievalBundle(
        provider_version="test.v1",
        candidates=(_candidate("p-1", "exact"),),
        products=({"id": "p-1", "name": "Ser Hidratant Ion", "price": 99.0},),
        evidence=EvidenceBundle((EvidenceReference("ev-1", "p-1", "benefit"),)),
        coverage=(FacetCoverage(facet="price", match=1, mismatch=0, unknown=0),),
        degradations=("semantic_failed",),
        timing=RetrievalTiming(elapsed_ms=12, db_query_count=4, external_calls=1),
    )
    safe = bundle.to_safe_dict()
    blob = str(safe)

    # Numele/prețul produsului sunt în `products`, dar NU au voie în proiecția de telemetrie.
    assert "Ser Hidratant" not in blob
    assert "99.0" not in blob
    assert safe["counts"] == {
        "candidates": 1,
        "exact": 1,
        "alternative": 0,
        "rejected": 0,
        "evidence": 1,
    }
    assert safe["degradations"] == ["semantic_failed"]
    assert safe["timing"]["query_count_bucket"] == "3-5"  # bandă, nu numărul brut
    assert safe["candidate_ids"] == ["p-1"]


@pytest.mark.parametrize(
    ("count", "bucket"),
    [
        (0, "0"),
        (1, "1-2"),
        (2, "1-2"),
        (3, "3-5"),
        (5, "3-5"),
        (6, "6-10"),
        (10, "6-10"),
        (11, "10+"),
    ],
)
def test_query_count_bucket_is_low_cardinality(count, bucket):
    assert query_count_bucket(count) == bucket


def test_deadline_is_monotonic_and_never_reports_negative_remaining():
    deadline = RetrievalDeadline(budget_ms=50, started_at=time.monotonic() - 10)
    assert deadline.expired() is True
    assert deadline.remaining_ms() == 0  # niciodată negativ


def test_zero_budget_deadline_never_expires():
    """0 = kill-switch numeric (aceeași convenție ca `embed_timeout_ms`), nu „expiră imediat"."""
    deadline = RetrievalDeadline(budget_ms=0, started_at=time.monotonic() - 100)
    assert deadline.unbounded is True
    assert deadline.expired() is False
    assert deadline.remaining_ms() is None


def test_evidence_bundle_groups_by_product():
    bundle = EvidenceBundle(
        (
            EvidenceReference("ev-1", "p-1", "benefit"),
            EvidenceReference("ev-2", "p-2", "ingredient"),
            EvidenceReference("ev-3", "p-1", "usage"),
        )
    )
    assert [ref.evidence_id for ref in bundle.for_product("p-1")] == ["ev-1", "ev-3"]
    assert bundle.product_ids == ("p-1", "p-2")


def test_facets_by_key_is_empty_and_not_an_error_without_a_domain_pack():
    """Fără registru → UNKNOWN peste tot (fail-closed corect), nu excepție și nu MISMATCH."""

    class _Business:
        domain_pack = None

    assert facets_by_key(_Business()) == {}
    assert facets_by_key(None) == {}
