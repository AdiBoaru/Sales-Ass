"""NX-203 — evaluarea `hard_constraints` contra catalogului, în TREI stări.

Sursa UNICĂ a acestei logici. Scriptul de migrare şi metrica de benchmark o importă amândouă:
două copii ar diverge, iar atunci gold-ul şi scorul ar vorbi despre reguli diferite fără ca
cineva să observe.

**De ce trei stări.** `satisfies` / `violates` / `unknown`. Absenţa unui atribut NU e
incompatibilitate: un produs fără `spf` declarat nu are „SPF prea mic", are SPF necunoscut. Fără
distincţia asta, orice dată incompletă din catalog devine eroare falsă — la `texture`, de pildă,
163 din 300 de produse n-au atributul deloc.

**Excepţia declarată.** `unknown_is_violation` per constrângere, pentru praguri de SIGURANŢĂ: la o
cerere de SPF 50, un produs cu SPF necunoscut NU satisface cerinţa. Politica se scrie în qrels,
lângă constrângere, ca să fie vizibilă — nu ascunsă în cod.

**Ce NU derivă violări.** `suitable_for` / `concerns` cu lista prezentă dar fără valoarea cerută:
e absenţa unei valori, nu o incompatibilitate declarată. Regula vine din cazul celor patru seruri
fără `oily`, care fuseseră marcate greşit ca interzise.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

SATISFIES = "satisfies"
VIOLATES = "violates"
UNKNOWN = "unknown"

#: Atribute scalare comparate direct (eq / in).
_SCALAR = ("finish", "coverage", "texture", "routine_step")
#: Liste unde absenţa valorii cerute rămâne `unknown`, nu `violates`.
_TOLERANT_LISTS = ("suitable_for", "concerns", "key_ingredients")


def evaluate(product: dict[str, Any], constraint: dict[str, Any]) -> str:
    """Starea unui produs faţă de O constrângere, cu politica de `unknown` aplicată."""
    state = _state(product, constraint["facet"], constraint.get("op", "eq"), constraint["value"])
    if state == UNKNOWN and constraint.get("unknown_is_violation"):
        return VIOLATES
    return state


def violates_any(product: dict[str, Any], constraints: Sequence[Any]) -> bool:
    """True dacă produsul încalcă EXPLICIT măcar o constrângere."""
    return any(evaluate(product, _as_dict(c)) == VIOLATES for c in constraints)


def _as_dict(c: Any) -> dict[str, Any]:
    """Acceptă şi modelul Pydantic `HardConstraint`, şi dict-ul brut din JSON."""
    if isinstance(c, dict):
        return c
    out = {"facet": c.facet, "op": c.op, "value": c.value}
    out["unknown_is_violation"] = getattr(c, "unknown_is_violation", False)
    return out


def _state(product: dict[str, Any], facet: str, op: str, value: Any) -> str:
    attrs = product.get("attributes") or {}

    if facet == "category":
        cat = product.get("category_slug")
        if not cat:
            return UNKNOWN
        return SATISFIES if (cat in value if op == "in" else cat == value) else VIOLATES

    if facet == "price":
        price = product.get("price")
        if price is None:
            return UNKNOWN
        return SATISFIES if float(price) <= float(value) else VIOLATES

    if facet == "spf":
        raw = attrs.get("spf")
        if raw in (None, ""):
            return UNKNOWN
        try:
            n = float(raw)
        except (TypeError, ValueError):
            return UNKNOWN
        return SATISFIES if n >= float(value) else VIOLATES

    if facet in _SCALAR:
        raw = attrs.get(facet)
        if raw in (None, ""):
            return UNKNOWN
        return SATISFIES if (raw in value if op == "in" else raw == value) else VIOLATES

    if facet == "fragrance_free":
        raw = attrs.get("fragrance_free")
        return UNKNOWN if raw is None else (SATISFIES if raw is value else VIOLATES)

    if facet in _TOLERANT_LISTS:
        return SATISFIES if value in (attrs.get(facet) or []) else UNKNOWN

    return UNKNOWN
