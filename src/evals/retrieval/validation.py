"""NX-203 — validare de integritate pentru qrels înainte de orice benchmark/gate."""

from __future__ import annotations

import re
from collections import defaultdict

from src.evals.retrieval.schema import Provenance, QrelsSet

_PII = re.compile(
    r"(?:\b[\w.+-]+@[\w-]+\.[\w.-]+\b|"
    r"\b(?:\+40|0040|0)7\d(?:[ .-]?\d){7}\b|"
    r"\b(?:\d[ -]?){12,18}\d\b)"
)


def integrity_issues(
    qset: QrelsSet,
    *,
    min_queries: int = 1,
    require_human_verified: bool = False,
    require_real_per_category: bool = False,
) -> list[str]:
    """Întoarce problemele ce blochează o rulare; nu schimbă setul de date."""
    issues: list[str] = []
    if len(qset.queries) < min_queries:
        issues.append(f"sunt {len(qset.queries)} query-uri, sub minimul cerut {min_queries}")

    real_by_category: dict[str | None, int] = defaultdict(int)
    for query in qset.queries:
        if not query.query.strip():
            issues.append(f"{query.id}: query gol")
        if _PII.search(query.query):
            issues.append(f"{query.id}: query-ul conține posibil PII")
        judged = [item.product_id for item in query.judgments]
        if len(judged) != len(set(judged)):
            issues.append(f"{query.id}: judgments conține product_id duplicat")
        if not any(int(item.relevance) > 0 for item in query.judgments):
            issues.append(f"{query.id}: lipsește cel puțin un produs relevant")
        if require_human_verified and not query.human_verified:
            issues.append(f"{query.id}: nu este marcat human_verified")
        if query.provenance is Provenance.real_sanitized:
            real_by_category[query.category] += 1

    if require_real_per_category:
        for category in sorted({query.category for query in qset.queries}, key=str):
            if real_by_category[category] == 0:
                label = category or "fără categorie"
                issues.append(f"{label}: lipsește un query real_sanitized")
    return issues
