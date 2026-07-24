"""NX-208 — Contractul `QuerySpec` pe 3 reprezentări (ADR D6, reconciliază NX-185).

Trei reprezentări coexistă pentru „ce cere clientul", folosite ÎN PARALEL la căutare
(canonicalizarea adaugă precizie, nu înlocuiește textul):

  1. `raw_query`        — exact ce a scris clientul.
  2. `normalized_query` — lower + fără diacritice + trim (cheie de lookup determinist).
  3. `constraints[]`    — fațete canonice (hard/soft), plus `search_text` expandat pentru retrieval.

**Separarea OBLIGATORIE Runtime/Safe (D6) — invariant de confidențialitate:**

- `RuntimeQuerySpec` — conține `raw_query` + textul expandat. Un `@dataclass` frozen, **fără nicio
  cale de serializare** (nu are `model_dump`/`json`). Trăiește DOAR în memoria turului. `raw_query`
  poate conține nume / telefon / adresă / date medicale — de aceea NU iese niciodată din obiect ca
  text liber.
- `SafeQuerySpec` — canonical constraints + metadate normalizate, **FĂRĂ** raw/text liber, fără PII.
  Model Pydantic cu `extra="forbid"`: absența oricărui câmp de text liber e o garanție
  STRUCTURALĂ (nu o convenție) — `raw_query` n-are unde să intre.

`RuntimeQuerySpec.to_safe()` e **SINGURA** punte de la runtime la persistență/telemetrie și
DROPează tot textul liber (inclusiv numele de produs-referință din `reference_terms`).
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict


def _safe_str(value: object, allowed: Collection[str] | None) -> bool:
    """True dacă un `value` de tip str poate ieși în SafeQuerySpec: DOAR dacă e într-un vocabular
    CONTROLAT (`allowed`) pasat de apelant din registru/catalog. Fără listă de valori permise → nu
    iese niciun string. Un heuristic de „token" NU e suficient: un slug-PII (`ion_popescu_0722…`)
    ar trece; apartenența la un vocabular canonic o închide structural."""
    return isinstance(value, str) and allowed is not None and value in allowed


# Vocabular ÎNGUST de operatori/tării, comun cu qrels-ul (retrieval_qrels_compound.json) și NX-185.
Op = str  # "eq" | "lte" | "gte" | "contains" (validat de consumatori; ținut lax pt forward-compat)
Strength = str  # "hard" | "soft"


class Constraint(BaseModel):
    """O constrângere canonică pe o fațetă. `strength=hard` = inviolabilă de model (D7: buget,
    negație, brand exclus, safety); `soft` = preferință/ranking. `source` = de unde vine (turul
    curent / state moștenit / derivată de rescriere) — pentru telemetria de dezacord (NX-185)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    facet: str
    op: Op
    value: str | float | int | bool | None  # None = valoare REDACTATĂ în proiecția Safe
    strength: Strength = "hard"
    source: str = "current_turn"


class SafeQuerySpec(BaseModel):
    """Reprezentarea CANONICĂ persistabilă/telemetrizabilă — SINGURA care iese din tur.

    Nu are `raw_query`, `normalized_query`, `search_text` sau `reference_terms`: `extra="forbid"`
    respinge orice câmp în plus, deci textul liber (potențial PII) e imposibil de strecurat aici —
    garanție de tip, nu de disciplină. Vezi testul `test_query_spec_no_raw_leak`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int = 1
    schema_version: int = 1  # D3: prezent în toate contractele, chiar dacă pilotul e ro-RO
    locale: str = "ro"
    intent: str | None = None
    category: str | None = None  # slug canonic din catalog (nu text brut)
    constraints: tuple[Constraint, ...] = ()
    # Categoriile canonice ale produselor-referință dintr-un „ca X dar mai ieftin" (slug, FĂRĂ
    # numele/brandul din raw). Numele brut al referinței trăiește doar în RuntimeQuerySpec.
    reference_categories: tuple[str, ...] = ()
    sort: str = "relevance"


@dataclass(frozen=True, slots=True)
class RuntimeQuerySpec:
    """Reprezentarea de RUNTIME — DOAR în memoria turului, niciodată serializată.

    Conține `raw_query` (intact, pentru căutarea în paralel pe cele 3 reprezentări) și `search_text`
    (expandat determinist de `query_rewrite`). NU expune model_dump/json: unica punte spre exterior
    e `to_safe()`. Textul liber (raw / normalized / search_text / reference_terms) rămâne aici."""

    raw_query: str
    normalized_query: str
    search_text: str
    intent: str | None = None
    category: str | None = None
    constraints: tuple[Constraint, ...] = ()
    # Nume/brand de produs extrase din „ca X …" — pot conține text brut, NU se persistă.
    reference_terms: tuple[str, ...] = ()
    reference_categories: tuple[str, ...] = ()
    sort: str = "relevance"
    locale: str = "ro"

    def to_safe(self, allowed_values: Mapping[str, Collection[str]] | None = None) -> SafeQuerySpec:
        """Proiecție canonică fără text liber — SINGURA cale spre telemetrie/persistență.

        SANITIZEAZĂ valorile pe VOCABULAR CONTROLAT (fail-closed): un `value` string iese DOAR dacă
        e în `allowed_values[facet]` (valori canonice ale fațetei, date de apelant din registru).
        Numerele/boolean-urile trec (nu pot fi PII). Orice string neverificat e **REDACTAT la None**
        (facet/op/strength rămân pt telemetrie). `category`/`reference_categories` validate contra
        `allowed_values["category"]`. Fără `allowed_values` → NICIUN string nu iese: nu ne bazăm pe
        un heuristic de token (un slug-PII `ion_popescu_…` ar trece; vocabularul nu)."""
        av = allowed_values or {}

        def _redact(facet: str, value: object) -> str | float | int | bool | None:
            if isinstance(value, bool) or isinstance(value, (int, float)):
                return value  # numeric/bool: nu pot purta PII
            return value if _safe_str(value, av.get(facet)) else None  # string neverificat → None

        safe_constraints = tuple(
            c.model_copy(update={"value": _redact(c.facet, c.value)}) for c in self.constraints
        )
        cat_ok = av.get("category")
        return SafeQuerySpec(
            locale=self.locale,
            intent=self.intent,
            category=self.category if _safe_str(self.category, cat_ok) else None,
            constraints=safe_constraints,
            reference_categories=tuple(
                rc for rc in self.reference_categories if _safe_str(rc, cat_ok)
            ),
            sort=self.sort,
        )
