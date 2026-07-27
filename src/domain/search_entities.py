"""NX-209 — contractul shadow pentru `search_entities`, fără a modifica tool-ul live.

Pipeline-ul de retrieval va furniza candidații ordonați și evidence hydration. Acest modul leagă
rezultatele de evaluatorul NX-187, păstrând verdictul per candidat și distribuția tri-state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from src.agent.match_gate import ConstraintResult, FacetCoverage, MatchSet, build_match_set
from src.agent.query_spec import Constraint
from src.domain.facets import TypedFacet


@dataclass(frozen=True)
class EvidenceReference:
    """Referință hidratată; conținutul complet rămâne în storage, nu în contractul de ranking."""

    evidence_id: str
    product_id: str
    role: str


@dataclass(frozen=True)
class SearchEntityCandidate:
    product_id: str
    match_class: str  # exact | alternative | rejected
    constraint_results: tuple[ConstraintResult, ...]
    soft_penalty: int
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class SearchEntitiesResult:
    """Output complet shadow; niciun câmp nu decide încă răspunsul agentului."""

    candidates: tuple[SearchEntityCandidate, ...]
    constraint_coverage: tuple[FacetCoverage, ...]
    missing_information: tuple[str, ...]
    needs_refinement: bool
    identifier_status: str | None
    evidence: tuple[EvidenceReference, ...]


def build_search_entities_result(
    products: Sequence[dict],
    constraints: Sequence[Constraint],
    facets_by_key: Mapping[str, TypedFacet],
    *,
    evidence_by_product: Mapping[str, Sequence[EvidenceReference]] | None = None,
    identifier_status: str | None = None,
    refinement_required: bool = False,
) -> SearchEntitiesResult:
    """Împachetează candidații în ordinea retrievalului, fără enforcement sau reranking nou."""
    match_set: MatchSet = build_match_set(list(products), tuple(constraints), dict(facets_by_key))
    evidence_by_product = evidence_by_product or {}
    evidence: list[EvidenceReference] = []
    candidates: list[SearchEntityCandidate] = []
    for verdict in match_set.verdicts:
        refs = tuple(evidence_by_product.get(verdict.product_id, ()))
        evidence.extend(refs)
        candidates.append(
            SearchEntityCandidate(
                product_id=verdict.product_id,
                match_class=verdict.match_class,
                constraint_results=verdict.constraint_results,
                soft_penalty=verdict.soft_penalty,
                evidence_ids=tuple(item.evidence_id for item in refs),
            )
        )

    missing = tuple(coverage.facet for coverage in match_set.coverage if coverage.unknown > 0)
    return SearchEntitiesResult(
        candidates=tuple(candidates),
        constraint_coverage=match_set.coverage,
        missing_information=missing,
        needs_refinement=bool(missing) or not match_set.exact or refinement_required,
        identifier_status=identifier_status,
        evidence=tuple(evidence),
    )
