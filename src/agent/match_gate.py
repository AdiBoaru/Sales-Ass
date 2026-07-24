"""NX-187 — Match Gate (shadow, post-retrieval). PUR, testabil pe dict, ZERO enforcement.

Per produs × constrângere → verdict tri-state **MATCH | MISMATCH | UNKNOWN** (enum canonic UNIC în
tot sistemul, D7: UNKNOWN ≠ MISMATCH). Produsele intră într-un `MatchSet` cu **mulțimi DISJUNCTE**,
precedență strictă:

    1. `rejected`    — ≥1 hard MISMATCH (datele CONTRAZIC o constrângere dură);
    2. `alternative` — zero hard MISMATCH, dar ≥1 hard UNKNOWN (nu putem confirma → clarificare);
    3. `exact`       — TOATE hard constraints sunt MATCH.

**Soft constraints influențează DOAR scorul (`soft_penalty`), NU apartenența** — un produs
all-hard-MATCH cu soft mismatch rămâne `exact` (rang penalizat), nu `alternative`.

Consumă QuerySpec (NX-208: `Constraint`) + registrul tipizat (NX-186: `TypedFacet`) — tipul/op
vin din registru, verdictul nu se ghicește. Post-retrieval, in-memory; NU rescrie retrieval (SQL
tri-state = NX-189). Verdictul per candidat se PĂSTREAZĂ (NX-209 îl consumă).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.agent.query_spec import Constraint
from src.domain.facets import FacetType, TypedFacet, extract_value, is_valid_value
from src.domain.normalize import normalize

Verdict = str  # "MATCH" | "MISMATCH" | "UNKNOWN"  (enum canonic UNIC, D7)
MatchClass = str  # "exact" | "alternative" | "rejected"

# Cheile de constrângere pot apărea la singular (QuerySpec/qrels) față de registru (plural catalog).
_FACET_KEY_ALIASES: dict[str, str] = {"concern": "concerns", "key_ingredient": "key_ingredients"}


@dataclass(frozen=True)
class ConstraintResult:
    """Verdictul unei constrângeri pe un produs. `evidence_id` = de unde vine faptul (sursa)."""

    facet: str
    status: Verdict
    strength: str  # "hard" | "soft"
    evidence_id: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class CandidateVerdict:
    """Verdictele unui produs + clasa derivată. `soft_penalty` = nr. de soft MISMATCH (ranking;
    NU schimbă apartenența)."""

    product_id: str
    constraint_results: tuple[ConstraintResult, ...]
    match_class: MatchClass
    soft_penalty: int


@dataclass(frozen=True)
class MatchSet:
    """Mulțimi DISJUNCTE de product_id + verdicte per candidat (păstrate) + acoperirea agregată."""

    exact: tuple[str, ...]
    alternatives: tuple[str, ...]
    rejected: tuple[str, ...]
    verdicts: tuple[CandidateVerdict, ...]
    coverage: tuple[ConstraintResult, ...]  # agregat per fațetă hard (același enum tri-state)


def _facet_for(facets_by_key: dict[str, TypedFacet], key: str) -> TypedFacet | None:
    return facets_by_key.get(key) or facets_by_key.get(_FACET_KEY_ALIASES.get(key, key))


def evaluate(facet: TypedFacet, op: str, value: Any, product_value: Any) -> Verdict:
    """Tri-state pentru O constrângere pe un produs (D7). Valoare lipsă/nevalidă = UNKNOWN, nu
    MISMATCH — „nu știm" ≠ „contrazice". Tipul/operatorul vin din `facet` (registrul NX-186)."""
    if product_value is None or not is_valid_value(facet, product_value):
        return "UNKNOWN"
    if facet.value_type is FacetType.NUMBER:
        try:
            pv, val = float(product_value), float(value)
        except (TypeError, ValueError):
            return "UNKNOWN"
        ok = {"lte": pv <= val, "gte": pv >= val, "eq": pv == val}.get(op, pv == val)
        return "MATCH" if ok else "MISMATCH"
    if facet.value_type is FacetType.BOOL:
        return "MATCH" if bool(product_value) == bool(value) else "MISMATCH"
    if facet.value_type is FacetType.LIST and isinstance(product_value, list):
        vals = {normalize(str(x)) for x in product_value}
        return "MATCH" if normalize(str(value)) in vals else "MISMATCH"
    return "MATCH" if normalize(str(product_value)) == normalize(str(value)) else "MISMATCH"


def classify_product(
    product: dict[str, Any],
    constraints: list[Constraint] | tuple[Constraint, ...],
    facets_by_key: dict[str, TypedFacet],
) -> CandidateVerdict:
    """Un produs → verdicte per constrângere + clasa DISJUNCTĂ (rejected→alternative→exact).
    Constrângere pe o fațetă necunoscută registrului → UNKNOWN (conservator, nu MATCH)."""
    attrs = product.get("attributes") if isinstance(product.get("attributes"), dict) else {}
    results: list[ConstraintResult] = []
    hard_mismatch = hard_unknown = soft_penalty = 0
    for c in constraints:
        facet = _facet_for(facets_by_key, c.facet)
        if facet is None:
            status: Verdict = "UNKNOWN"  # fără tip → nu putem confirma
        else:
            status = evaluate(facet, c.op, c.value, extract_value(facet, product, attrs))
        results.append(
            ConstraintResult(facet=c.facet, status=status, strength=c.strength, evidence_id=c.facet)
        )
        if c.strength == "hard":
            if status == "MISMATCH":
                hard_mismatch += 1
            elif status == "UNKNOWN":
                hard_unknown += 1
        elif status == "MISMATCH":
            soft_penalty += 1  # soft = doar scor, nu apartenență

    if hard_mismatch:
        match_class = "rejected"
    elif hard_unknown:
        match_class = "alternative"
    else:
        match_class = "exact"
    return CandidateVerdict(
        product_id=str(product.get("id") or product.get("product_id") or ""),
        constraint_results=tuple(results),
        match_class=match_class,
        soft_penalty=soft_penalty,
    )


def _aggregate_coverage(
    verdicts: list[CandidateVerdict], constraints: list[Constraint] | tuple[Constraint, ...]
) -> tuple[ConstraintResult, ...]:
    """Acoperirea AGREGATĂ per fațetă hard: MISMATCH dacă vreun produs contrazice, altfel MATCH
    dacă vreunul confirmă, altfel UNKNOWN. Semnalul „cât UNKNOWN produce catalogul"."""
    out: list[ConstraintResult] = []
    for c in constraints:
        if c.strength != "hard":
            continue
        statuses = {
            r.status
            for v in verdicts
            for r in v.constraint_results
            if r.facet == c.facet and r.strength == "hard"
        }
        agg = (
            "MISMATCH"
            if "MISMATCH" in statuses
            else ("MATCH" if "MATCH" in statuses else "UNKNOWN")
        )
        out.append(ConstraintResult(facet=c.facet, status=agg, strength="hard"))
    return tuple(out)


def build_match_set(
    products: list[dict[str, Any]],
    constraints: list[Constraint] | tuple[Constraint, ...],
    facets_by_key: dict[str, TypedFacet],
) -> MatchSet:
    """Candidați → `MatchSet` disjunct. Ordinea în fiecare mulțime = ordinea de intrare (stabil)."""
    verdicts = [classify_product(p, constraints, facets_by_key) for p in products]
    exact = tuple(v.product_id for v in verdicts if v.match_class == "exact")
    alternatives = tuple(v.product_id for v in verdicts if v.match_class == "alternative")
    rejected = tuple(v.product_id for v in verdicts if v.match_class == "rejected")
    return MatchSet(
        exact=exact,
        alternatives=alternatives,
        rejected=rejected,
        verdicts=tuple(verdicts),
        coverage=_aggregate_coverage(verdicts, constraints),
    )
