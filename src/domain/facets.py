"""NX-186 — registru TIPIZAT de fațete + coverage (pur). Fundația pentru Match Gate (NX-187) și
enforcement (NX-188/189).

`searchable_facets` din DomainPack e azi doar `tuple[str]`. QuerySpec/Match Gate au nevoie de TIPURI
+ operatori + politica pentru valori lipsă. Registrul e din COD (allowlist de chei) — config NU
poate introduce SQL arbitrar sau JSON paths. Coverage-ul (per business+category+facet) se MĂSOARĂ
înainte de orice enforcement (o fațetă fără date → prea mult UNKNOWN → recall prăbușit).

Pur: `facet_value` (extractor din `attributes`) + `facet_coverage` (statistici) pe dict-uri.
"""

from __future__ import annotations

import math
import unicodedata
from dataclasses import dataclass, field
from typing import Any

_VALUE_TYPES = frozenset({"bool", "enum", "number", "text", "list"})
_OPERATORS = frozenset({"eq", "in", "gte", "lte", "contains_any", "contains_all"})
_MISSING = frozenset({"unknown", "fail"})

_BOOL_TRUE = frozenset({"true", "da", "yes", "1", "adevarat"})
_BOOL_FALSE = frozenset({"false", "nu", "no", "0", "fals"})


def _norm(s: str) -> str:
    d = unicodedata.normalize("NFKD", (s or "").lower())
    return "".join(c for c in d if not unicodedata.combining(c))


def parse_bool(x: Any) -> bool | None:
    """Parse canonic la bool: bool real, 0/1, sau token cunoscut (da/nu/true/false/yes/no). Altfel
    None (necunoscut). SURSĂ UNICĂ pentru coverage (validitate) ȘI Match Gate (verdict) — semantica
    TREBUIE să coincidă (Codex: coverage cerea bool real, dar Match Gate accepta stringuri bool)."""
    if isinstance(x, bool):
        return x
    if isinstance(x, (int, float)):
        if x == 0:
            return False
        if x == 1:
            return True
        return None
    n = _norm(str(x))
    if n in _BOOL_TRUE:
        return True
    if n in _BOOL_FALSE:
        return False
    return None


def is_valid_number(x: Any) -> bool:
    """Numeric FINIT (Codex: NaN/inf NU e un număr valid). bool exclus (True nu e „număr")."""
    if isinstance(x, bool):
        return False
    try:
        return math.isfinite(float(x))
    except (TypeError, ValueError):
        return False


@dataclass(frozen=True)
class FacetSpec:
    """Contractul TIPIZAT al unei fațete. `missing_policy` ∈ {unknown, fail}: cum tratăm valoarea
    lipsă (Match Gate). `min_coverage` = pragul sub care fațeta NU se enforce-uiește (NX-188)."""

    key: str
    value_type: str
    operators: tuple[str, ...]
    values: tuple[str, ...] = ()  # enum: valorile canonice permise
    aliases: dict[str, str] = field(default_factory=dict)  # alias normalizat → canonic
    missing_policy: str = "unknown"
    min_coverage: float = 0.5

    def __post_init__(self) -> None:
        if self.value_type not in _VALUE_TYPES:
            raise ValueError(f"value_type invalid: {self.value_type}")
        bad_op = set(self.operators) - _OPERATORS
        if bad_op:
            raise ValueError(f"operatori invalizi: {bad_op}")
        if self.missing_policy not in _MISSING:
            raise ValueError(f"missing_policy invalid: {self.missing_policy}")

    def canonicalize(self, value: Any) -> Any:
        """Normalizează la forma canonică (alias → canonic pt enum; bool/number ca atare)."""
        if self.value_type == "enum" and isinstance(value, str):
            return self.aliases.get(_norm(value), _norm(value))
        return value


def build_registry(specs: list[FacetSpec]) -> dict[str, FacetSpec]:
    """Registru {key: FacetSpec}, validat la construcție (fail-closed pe chei duplicate)."""
    reg: dict[str, FacetSpec] = {}
    for s in specs:
        if s.key in reg:
            raise ValueError(f"fațetă duplicată în registru: {s.key}")
        reg[s.key] = s
    return reg


def facet_value(product: dict[str, Any], spec: FacetSpec) -> Any:
    """Valoarea fațetei din `product` (top-level SAU `attributes`), canonicalizată. None = lipsă."""
    attrs = product.get("attributes") if isinstance(product.get("attributes"), dict) else {}
    raw = product.get(spec.key)
    if raw is None:
        raw = attrs.get(spec.key)
    if raw is None:
        return None
    if spec.value_type == "list" and isinstance(raw, (list, tuple)):
        return [spec.canonicalize(x) for x in raw]
    return spec.canonicalize(raw)


def _is_valid_value(spec: FacetSpec, v: Any) -> bool:
    """Valoarea (deja extrasă/canonicalizată) e VALIDĂ pentru TIPUL fațetei? (Codex: nu doar
    „prezentă"). enum→în `values`; bool→bool real (nu „necunoscut"/2); number→numeric;
    list→are elemente; text→string nevid."""
    if spec.value_type == "enum":
        return v in spec.values
    if spec.value_type == "bool":
        return parse_bool(v) is not None  # aliniat cu Match Gate (string bool cunoscut = valid)
    if spec.value_type == "number":
        return is_valid_number(v)  # numeric finit; NaN/inf/bool/text → invalid
    if spec.value_type == "list":
        return isinstance(v, (list, tuple)) and len(v) > 0
    return isinstance(v, str) and bool(v.strip())


def facet_coverage(products: list[dict[str, Any]], spec: FacetSpec) -> dict[str, Any]:
    """Coverage-ul unei fațete pe un set de produse (pur). Distinge (Codex): valoare PREZENTĂ vs
    VALIDĂ pentru tipul fațetei (`_is_valid_value`, nu doar enum). `pct_present`/`pct_valid` +
    `enforceable` (peste `min_coverage` + numărul minim de produse). Denominator explicit."""
    n = len(products)
    present = valid = 0
    for p in products:
        v = facet_value(p, spec)
        if v is None or (isinstance(v, (list, tuple)) and not v):
            continue
        present += 1
        valid += 1 if _is_valid_value(spec, v) else 0
    pct_present = round(present / n, 3) if n else 0.0
    pct_valid = round(valid / n, 3) if n else 0.0
    return {
        "facet": spec.key,
        "n": n,
        "present": present,
        "valid": valid,
        "pct_present": pct_present,
        "pct_valid": pct_valid,
        # enforceable pe date VALIDE, nu doar PREZENTE (Codex): 10 produse cu enum invalid
        # (finish="necunoscut") au valid=0 → NU enforceable, deși sunt prezente. Peste prag ȘI
        # cu suficiente produse (evită „100%" pe 2 produse).
        "enforceable": n >= 10 and pct_valid >= spec.min_coverage,
    }
