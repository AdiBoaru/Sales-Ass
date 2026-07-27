"""NX-209 — decizia pură pentru rerank adaptiv, fără apel către vreun provider."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from src.agent.query_spec import Constraint


@dataclass(frozen=True)
class RerankDecision:
    """Decizie explicabilă, sigură de telemetrizat: conține numai coduri fixe, fără query/PII."""

    requested: bool
    reasons: tuple[str, ...]


def decide_adaptive_rerank(
    *,
    identifier_status: str | None,
    constraints: Sequence[Constraint],
    lexical_ids: Sequence[str],
    semantic_ids: Sequence[str],
    close_score_margin: float | None = None,
) -> RerankDecision:
    """Cere rerank numai când poate aduce valoare; SKU/nume exact nu trece niciodată prin el.

    Nu inspectează textul query-ului ori date de produs și nu alege un provider. Apelantul poate
    furniza ulterior doar marja numerică a scorurilor fuzionate, fără să expună conținut extern.
    """
    if identifier_status == "resolve":
        return RerankDecision(requested=False, reasons=("exact_identifier",))

    candidate_ids = set(lexical_ids) | set(semantic_ids)
    if len(candidate_ids) < 2:
        return RerankDecision(requested=False, reasons=("insufficient_candidates",))

    reasons: list[str] = []
    if len(constraints) >= 2:
        reasons.append("multiple_constraints")
    if lexical_ids and semantic_ids and lexical_ids[0] != semantic_ids[0]:
        reasons.append("lexical_semantic_disagreement")
    if close_score_margin is not None and 0 <= close_score_margin <= 0.03:
        reasons.append("close_scores")
    return RerankDecision(requested=bool(reasons), reasons=tuple(reasons))
