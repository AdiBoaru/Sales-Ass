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
class FacetCoverage:
    """Acoperirea AGREGATĂ per fațetă hard — DISTRIBUȚIA completă (Review #248: nu comprimăm
    MATCH+UNKNOWN într-un singur status; distribuția UNKNOWN e exact ce cere DoD-ul + NX-188)."""

    facet: str
    match: int
    mismatch: int
    unknown: int


@dataclass(frozen=True)
class MatchSet:
    """Mulțimi DISJUNCTE de product_id + verdicte per candidat (păstrate) + acoperirea agregată."""

    exact: tuple[str, ...]
    alternatives: tuple[str, ...]
    rejected: tuple[str, ...]
    verdicts: tuple[CandidateVerdict, ...]
    coverage: tuple[FacetCoverage, ...]  # distribuție MATCH/MISMATCH/UNKNOWN per fațetă hard


def _facet_for(facets_by_key: dict[str, TypedFacet], key: str) -> TypedFacet | None:
    return facets_by_key.get(key) or facets_by_key.get(_FACET_KEY_ALIASES.get(key, key))


def canonical_facet_key(facets_by_key: dict[str, TypedFacet], key: str) -> str:
    """Cheia CANONICĂ a unei fațete (cea din registru). `concern` (singular, din QuerySpec/qrels) și
    `concerns` (plural, din catalog) sunt ACEEAȘI fațetă — agregarea trebuie să le vadă la fel, nu
    să producă două rânduri. Fațetă necunoscută registrului → cheia dată (după alias), ca să rămână
    vizibilă în coverage."""
    facet = _facet_for(facets_by_key, key)
    return facet.key if facet else _FACET_KEY_ALIASES.get(key, key)


def resolve_query_value(facet: TypedFacet, value: Any) -> Any:
    """Valoarea DIN QUERY proiectată pe vocabularul fațetei: rezolvă aliasul (`gras` → `oily`) și
    cere apartenența la valorile canonice, unde fațeta le declară (ENUM/LIST).

    Review re-review #248: o valoare de query pe care fațeta NU o cunoaște (typo, sinonim
    nemapat, vocabular vechi) NU e o contrazicere — e o necunoscută. A întoarce MISMATCH ar
    declara produsele incompatibile pe baza unei constrângeri pe care nu o putem interpreta;
    D7 cere UNKNOWN. `None` = „nu putem interpreta valoarea"."""
    if not isinstance(value, str):
        return value
    resolved = facet.aliases.get(normalize(value), value)
    if facet.values and resolved not in facet.values:
        return None
    return resolved


def evaluate(facet: TypedFacet, op: str, value: Any, product_value: Any) -> Verdict:
    """Tri-state pentru O constrângere pe un produs (D7). Valoare/operator lipsă sau nevalid =
    UNKNOWN, NICIODATĂ MATCH/MISMATCH — „nu putem evalua" ≠ „contrazice". Tipul/operatorii permiși
    și VOCABULARUL valorilor vin din `facet` (registrul NX-186)."""
    # Review #248: operator neacceptat de fațetă, valoare de query lipsă SAU de TIP incompatibil cu
    # fațeta (True pt NUMBER, 123 pt TEXT) NU trebuie să producă un MATCH accidental → UNKNOWN.
    if op not in facet.operators or value is None:
        return "UNKNOWN"
    # …iar o valoare de query în afara vocabularului fațetei nu poate produce nici MISMATCH.
    value = resolve_query_value(facet, value)
    if value is None:
        return "UNKNOWN"
    if product_value is None or not is_valid_value(facet, product_value):
        return "UNKNOWN"
    if facet.value_type is FacetType.NUMBER:
        # bool e subtip de int (float(True)=1.0) → l-am respinge; cerem un număr propriu-zis.
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return "UNKNOWN"
        pv, val = float(product_value), float(value)
        if op == "lte":
            return "MATCH" if pv <= val else "MISMATCH"
        if op == "gte":
            return "MATCH" if pv >= val else "MISMATCH"
        if op == "eq":
            return "MATCH" if pv == val else "MISMATCH"
        return "UNKNOWN"  # niciun default tăcut la eq
    if facet.value_type is FacetType.BOOL:
        if not isinstance(value, bool):  # valoare de query invalidă pt bool → UNKNOWN
            return "UNKNOWN"
        return "MATCH" if bool(product_value) == value else "MISMATCH"
    # ENUM / TEXT / LIST — valoarea de query trebuie să fie string (123 pt TEXT → UNKNOWN).
    if not isinstance(value, str):
        return "UNKNOWN"
    if facet.value_type is FacetType.LIST and isinstance(product_value, list):
        vals = {normalize(str(x)) for x in product_value}
        return "MATCH" if normalize(value) in vals else "MISMATCH"
    return "MATCH" if normalize(str(product_value)) == normalize(value) else "MISMATCH"


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
    verdicts: list[CandidateVerdict],
    constraints: list[Constraint] | tuple[Constraint, ...],
    facets_by_key: dict[str, TypedFacet],
) -> tuple[FacetCoverage, ...]:
    """DISTRIBUȚIA MATCH/MISMATCH/UNKNOWN per fațetă hard (numere, nu status comprimat — un
    unknown>0 = „cât UNKNOWN produce catalogul", NX-188). Review #248: UN SINGUR rând per fațetă
    (deduplicat) și UN SINGUR vot per produs — două constrângeri hard pe aceeași fațetă NU dublează
    nici rândurile, nici contoarele. Per produs, statusul pe fațetă agregă cu precedența
    MISMATCH > UNKNOWN > MATCH (aceeași ca la clasa produsului).

    Re-review #248: dedupe-ul se face pe cheia CANONICĂ din registru — `concern` (alias) și
    `concerns` (cheia catalogului) sunt aceeași fațetă, deci un singur rând, nu două."""
    hard_facets: list[str] = []
    for c in constraints:
        key = canonical_facet_key(facets_by_key, c.facet)
        if c.strength == "hard" and key not in hard_facets:
            hard_facets.append(key)  # unic pe cheia canonică, ordine stabilă
    out: list[FacetCoverage] = []
    for facet in hard_facets:
        counts = {"MATCH": 0, "MISMATCH": 0, "UNKNOWN": 0}
        for v in verdicts:
            statuses = [
                r.status
                for r in v.constraint_results
                if canonical_facet_key(facets_by_key, r.facet) == facet and r.strength == "hard"
            ]
            if not statuses:
                continue
            agg = (
                "MISMATCH"
                if "MISMATCH" in statuses
                else ("UNKNOWN" if "UNKNOWN" in statuses else "MATCH")
            )
            counts[agg] += 1  # un singur vot per produs, nu per constrângere
        out.append(
            FacetCoverage(
                facet=facet,
                match=counts["MATCH"],
                mismatch=counts["MISMATCH"],
                unknown=counts["UNKNOWN"],
            )
        )
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
        coverage=_aggregate_coverage(verdicts, constraints, facets_by_key),
    )
