"""NX-187 — Match Gate (shadow): verdict MATCH/MISMATCH/UNKNOWN per produs×constrângere → `MatchSet`
DISJUNCT (exact/alternatives/rejected). Pur, testabil pe dict-uri. ZERO enforcement (shadow); NX-188
aplică. `reason_codes` (NX-170) e pozitiv-only — Match Gate adaugă verdictul NEGATIV lipsă.

MatchSet — precedență STRICTĂ (Codex, mulțimi disjuncte):
  1. rejected      — ≥1 hard MISMATCH
  2. alternatives  — 0 hard MISMATCH, dar ≥1 hard UNKNOWN
  3. exact         — toate hard = MATCH
Soft constraints influențează DOAR scorul/ranking-ul, NU apartenența.
"""

from __future__ import annotations

import unicodedata
from typing import Any

from src.agent.query_spec import Constraint, QuerySpec
from src.domain.facets import FacetSpec, facet_value, is_valid_number, parse_bool

MATCH, MISMATCH, UNKNOWN = "MATCH", "MISMATCH", "UNKNOWN"


def _norm(s: Any) -> str:
    d = unicodedata.normalize("NFKD", str(s or "").lower())
    return "".join(c for c in d if not unicodedata.combining(c))


def _nonfinite(x: Any) -> bool:
    """Float NaN/inf? (un int nu poate fi non-finit). Codex R7: `NaN == NaN` prin `_norm` ('nan' ==
    'nan') devenea MATCH numeric — trebuie UNKNOWN."""
    return isinstance(x, float) and not is_valid_number(x)


def _product_value(product: dict[str, Any], facet: str, spec: FacetSpec | None) -> Any:
    """Valoarea fațetei din produs. Cu FacetSpec → `facet_value` (typed). Fără → fallback pe câmpuri
    cunoscute (price/brand top-level; concerns/suitable_for din attributes)."""
    if spec is not None:
        return facet_value(product, spec)
    if facet == "price":
        return product.get("price")
    if facet == "brand":
        return product.get("brand") or product.get("brand_name")
    attrs = product.get("attributes") if isinstance(product.get("attributes"), dict) else {}
    if facet in ("concerns", "suitable_for"):
        return (attrs.get("concerns") or []) + (attrs.get("suitable_for") or [])
    return product.get(facet, attrs.get(facet))


def evaluate_constraint(product: dict[str, Any], c: Constraint, spec: FacetSpec | None) -> str:
    """Verdict pentru o constrângere pe un produs. Valoare lipsă → UNKNOWN (nu MISMATCH — Codex).
    `op` ∈ {lte, gte, eq, contains, contains_any, contains_all}."""
    v = _product_value(product, c.facet, spec)
    if v is None or (isinstance(v, (list, tuple)) and not v):
        return UNKNOWN
    try:
        if c.op in ("lte", "gte"):
            if not is_valid_number(v) or not is_valid_number(c.value):
                return UNKNOWN  # NaN/inf/text → nu comparăm numeric (Codex: aliniat cu coverage)
            fv, fc = float(v), float(c.value)
            return MATCH if (fv <= fc if c.op == "lte" else fv >= fc) else MISMATCH
        if c.op == "eq":
            # Codex R7: eq NU mai ocolește helper-ele tipizate. NaN/inf → UNKNOWN (nu MATCH prin
            # _norm). BOOL (spec bool / bool real / fără spec, ambele tokeni bool) → parse_bool
            # (aliniat cu coverage: „true"/„da" = același verdict). NUMĂR → numeric (5==5.0).
            if _nonfinite(v) or _nonfinite(c.value):
                return UNKNOWN
            vt = spec.value_type if spec else None
            is_bool = (
                vt == "bool"
                or isinstance(v, bool)
                or isinstance(c.value, bool)
                or (vt is None and parse_bool(v) is not None and parse_bool(c.value) is not None)
            )
            if is_bool:
                bv, cv = parse_bool(v), parse_bool(c.value)
                if bv is None or cv is None:
                    return UNKNOWN
                return MATCH if bv == cv else MISMATCH
            if vt == "number" or (is_valid_number(v) and is_valid_number(c.value)):
                return MATCH if float(v) == float(c.value) else MISMATCH
            return MATCH if _norm(v) == _norm(c.value) else MISMATCH
        if c.op in ("contains", "contains_any", "contains_all"):
            vals = {_norm(x) for x in (v if isinstance(v, (list, tuple)) else [v])}
            wanted = (
                {_norm(x) for x in c.value}
                if isinstance(c.value, (list, tuple))
                else {_norm(c.value)}
            )
            if c.op == "contains_all":
                return MATCH if wanted <= vals else MISMATCH
            return MATCH if wanted & vals else MISMATCH
    except (TypeError, ValueError):
        return UNKNOWN
    return UNKNOWN


def classify_product(
    product: dict[str, Any], spec: QuerySpec, registry: dict[str, FacetSpec]
) -> tuple[str, dict[str, str]]:
    """(bucket, verdicts) pentru un produs vs constrângerile HARD. Soft = ignorat aici (ranking).
    Verdictele sunt cheie-uite pe `facet:op:value` — NU doar pe `facet` (Codex: două constrângeri pe
    aceeași fațetă, ex. concerns=oily ȘI concerns=sensitive, nu se suprascriu; MISMATCH-ul primei nu
    poate fi șters de un MATCH pe a doua)."""
    verdicts: dict[str, str] = {}
    for i, c in enumerate(spec.hard()):
        key = f"{c.facet}:{c.op}:{c.value}"
        if key in verdicts:
            key = f"{key}#{i}"
        verdicts[key] = evaluate_constraint(product, c, registry.get(c.facet))
    vals = set(verdicts.values())
    if MISMATCH in vals:
        return "rejected", verdicts
    if UNKNOWN in vals:
        return "alternatives", verdicts
    return "exact", verdicts


def match_set(
    products: list[dict[str, Any]], spec: QuerySpec, registry: dict[str, FacetSpec] | None = None
) -> dict[str, list[str]]:
    """Clasifică produsele în mulțimi DISJUNCTE (exact/alternatives/rejected), precedență strictă.
    Fără constrângeri hard → toate exact. `registry` opțional (fără → fallback pe câmpuri)."""
    reg = registry or {}
    out: dict[str, list[str]] = {"exact": [], "alternatives": [], "rejected": []}
    for p in products:
        pid = str(p.get("id") or p.get("product_id") or "")
        if not pid:
            continue
        bucket, _ = classify_product(p, spec, reg)
        out[bucket].append(pid)
    return out
