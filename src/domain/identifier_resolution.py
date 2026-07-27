"""NX-209 — rezolvare exactă/fuzzy de produs, pură și folosită doar în shadow."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Sequence

from rapidfuzz.fuzz import ratio

from src.domain.normalize import normalize

ResolutionStatus = Literal["resolve", "clarify", "not_found"]
_RESOLVE_THRESHOLD = 92.0
_CLARIFY_THRESHOLD = 75.0
_AMBIGUITY_MARGIN = 4.0


@dataclass(frozen=True)
class IdentifierCandidate:
    product_id: str
    name: str
    skus: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class IdentifierResolution:
    status: ResolutionStatus
    product_id: str | None
    score: float
    candidate_ids: tuple[str, ...]


def _norm(value: str) -> str:
    return normalize(value).replace(" ", "")


def resolve_identifier(
    query: str, candidates: Sequence[IdentifierCandidate]
) -> IdentifierResolution:
    """Exact înainte de fuzzy; scor apropiat sau mediu cere clarify, nu alege arbitrar."""
    query_normalized = _norm(query)
    if not query_normalized:
        return IdentifierResolution("not_found", None, 0.0, ())

    exact = [
        candidate
        for candidate in candidates
        if query_normalized
        in {_norm(value) for value in (candidate.name, *candidate.skus, *candidate.aliases)}
    ]
    if len(exact) == 1:
        return IdentifierResolution("resolve", exact[0].product_id, 100.0, (exact[0].product_id,))
    if len(exact) > 1:
        return IdentifierResolution(
            "clarify", None, 100.0, tuple(item.product_id for item in exact)
        )

    scored = sorted(
        (
            (float(ratio(query_normalized, _norm(candidate.name))), candidate)
            for candidate in candidates
        ),
        key=lambda item: (-item[0], item[1].product_id),
    )
    if not scored or scored[0][0] < _CLARIFY_THRESHOLD:
        return IdentifierResolution("not_found", None, scored[0][0] if scored else 0.0, ())

    best_score, best = scored[0]
    close = tuple(
        candidate.product_id
        for score, candidate in scored
        if score >= _CLARIFY_THRESHOLD and best_score - score <= _AMBIGUITY_MARGIN
    )
    if best_score < _RESOLVE_THRESHOLD or len(close) > 1:
        return IdentifierResolution("clarify", None, best_score, close)
    return IdentifierResolution("resolve", best.product_id, best_score, (best.product_id,))
