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
from src.domain.facets import FacetSpec, facet_value

MATCH, MISMATCH, UNKNOWN = "MATCH", "MISMATCH", "UNKNOWN"

_TRUE = {"true", "da", "yes", "1", "adevarat"}
_FALSE = {"false", "nu", "no", "0", "fals"}


def _norm(s: Any) -> str:
    d = unicodedata.normalize("NFKD", str(s or "").lower())
    return "".join(c for c in d if not unicodedata.combining(c))


def _as_bool(x: Any) -> bool | None:
    """Coerciție booleană robustă. String → DOAR tokeni cunoscuți (Codex: `bool('false')` e True →
    fals-pozitiv). Numeric → DOAR 0/1 (Codex: `2` NU e boolean → None, nu True). Orice necunoscut →
    None → verdict UNKNOWN (nu ghicim)."""
    if isinstance(x, bool):
        return x
    if isinstance(x, (int, float)) and not isinstance(x, bool):
        if x == 0:
            return False
        if x == 1:
            return True
        return None
    n = _norm(x)
    if n in _TRUE:
        return True
    if n in _FALSE:
        return False
    return None


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
        if c.op == "lte":
            return MATCH if float(v) <= float(c.value) else MISMATCH
        if c.op == "gte":
            return MATCH if float(v) >= float(c.value) else MISMATCH
        if c.op == "eq":
            if isinstance(v, bool) or isinstance(c.value, bool):
                bv, cv = _as_bool(v), _as_bool(c.value)
                if bv is None or cv is None:
                    return UNKNOWN
                return MATCH if bv == cv else MISMATCH
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
