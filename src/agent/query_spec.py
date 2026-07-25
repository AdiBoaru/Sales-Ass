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
DROPează tot textul liber (inclusiv numele de produs-referință din `reference_terms`). Proiecția
validează **fiecare câmp** contra unui `SafeVocabulary` (vocabular controlat) — nu doar `value`:
`facet`, `op`, `intent`, `category`, `locale` sunt string-uri la fel de capabile să poarte PII.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict


def _safe_str(value: object, allowed: Collection[str] | None) -> bool:
    """True dacă un `value` de tip str poate ieși în SafeQuerySpec: DOAR dacă e într-un vocabular
    CONTROLAT (`allowed`). Fără listă de valori permise → nu iese niciun string. Un heuristic de
    „token" NU e suficient: un slug-PII (`ion_popescu_0722…`) ar trece; apartenența la un vocabular
    canonic o închide structural."""
    return isinstance(value, str) and allowed is not None and value in allowed


# Vocabular ÎNGUST de operatori/tării, comun cu qrels-ul (retrieval_qrels_compound.json) și NX-185.
Op = str  # "eq" | "lte" | "gte" | "contains" (validat de consumatori; ținut lax pt forward-compat)
Strength = str  # "hard" | "soft"

# --- Vocabulare ÎNCHISE, definite în COD (nu vin de la apelant, nu pot purta text liber) ---
# Câmpurile de mai jos sunt produse de cod determinist (rescriere / stagii), nu extrase din mesaj —
# deci mulțimea lor e finită și verificabilă aici. Orice valoare din afara mulțimii = DROP/None.
_OPS: frozenset[str] = frozenset({"eq", "lte", "gte", "contains"})
_STRENGTHS: frozenset[str] = frozenset({"hard", "soft"})
_SOURCES: frozenset[str] = frozenset({"current_turn", "state", "derived"})
_INTENTS: frozenset[str] = frozenset({"recommend", "compare", "find_alternative"})
_SORTS: frozenset[str] = frozenset({"relevance", "price_asc", "price_desc"})


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


@dataclass(frozen=True, slots=True)
class SafeVocabulary:
    """Vocabularul CONTROLAT contra căruia se validează proiecția Safe — singurul lucru care poate
    lăsa date să iasă din tur.

    Toate câmpurile vin din surse CONTROLATE (registrul de fațete / catalog / config), niciodată
    din mesajul clientului. Ce nu e aici nu iese: fail-closed pe toată suprafața, nu doar `value`.

    - `facet_values`   — fațetă → valorile ei canonice de tip string (ENUM/LIST/TEXT controlat).
    - `numeric_facets` — fațetele unde o valoare NUMERICĂ e legitimă (preț, rating). Un număr NU e
      automat sigur: un telefon e tot număr. Numericul iese DOAR sub o fațetă declarată numerică.
    - `bool_facets`    — fațetele unde un boolean e legitim (fragrance_free etc.).
    - `categories`     — slug-uri canonice de categorie (`category` + `reference_categories`).
    - `locales`        — locale-urile suportate de business (`supported_locales`), nu din cod.
    """

    facet_values: Mapping[str, Collection[str]] = field(default_factory=dict)
    numeric_facets: Collection[str] = ()
    bool_facets: Collection[str] = ()
    categories: Collection[str] = ()
    locales: Collection[str] = ()

    def knows_facet(self, facet: object) -> bool:
        """True dacă numele fațetei însuși e dintr-un vocabular controlat. Numele de fațetă e un
        câmp string ca oricare altul: nevalidat, ar putea purta text de utilizator."""
        return isinstance(facet, str) and (
            facet in self.facet_values or facet in self.numeric_facets or facet in self.bool_facets
        )


_EMPTY_VOCAB = SafeVocabulary()


class SafeQuerySpec(BaseModel):
    """Reprezentarea CANONICĂ persistabilă/telemetrizabilă — SINGURA care iese din tur.

    Nu are `raw_query`, `normalized_query`, `search_text` sau `reference_terms`: `extra="forbid"`
    respinge orice câmp în plus, deci textul liber (potențial PII) e imposibil de strecurat aici —
    garanție de tip, nu de disciplină. Vezi testul `test_query_spec_no_raw_leak`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int = 1
    schema_version: int = 1  # D3: prezent în toate contractele, chiar dacă pilotul e ro-RO
    # None = locale neverificat contra `supported_locales` (fail-closed, ca orice alt string).
    locale: str | None = None
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

    def to_safe(self, vocab: SafeVocabulary | None = None) -> SafeQuerySpec:
        """Proiecție canonică fără text liber — SINGURA cale spre telemetrie/persistență.

        **Regula (re-review #246): FIECARE câmp al proiecției e verificat contra unui vocabular,
        nu doar `value`.** Un `Constraint` e construit de apelant: `facet`, `op`, `source` sunt
        string-uri la fel de capabile să poarte PII ca `value` — a valida doar valoarea lasă ușa
        deschisă (`facet="ion_popescu_0722123456"`). Iar numericul NU e sigur prin natura lui:
        un telefon e tot un număr.

        - `facet` necunoscut vocabularului → constrângerea e **DROPATĂ integral** (nu redactată:
          numele fațetei e el însuși payload-ul).
        - `op` / `strength` / `source` în afara vocabularelor ÎNCHISE din cod → **DROP**.
        - `value`: string → doar din `facet_values[facet]`; număr → doar sub o fațetă din
          `numeric_facets`; boolean → doar sub o fațetă din `bool_facets`; altfel **None**.
        - `intent` / `sort` → doar din vocabularele închise din cod; `locale` → doar din
          `vocab.locales` (`supported_locales`, nu o listă hardcodată); `category` /
          `reference_categories` → doar slug-uri din `vocab.categories`.

        Fără `vocab` → proiecție GOALĂ de string-uri și fără constrângeri (fail-closed complet)."""
        v = vocab or _EMPTY_VOCAB

        def _value_of(c: Constraint) -> str | float | int | bool | None:
            if isinstance(c.value, bool):
                return c.value if c.facet in v.bool_facets else None
            if isinstance(c.value, (int, float)):
                # Numărul nu e sigur prin natura lui (un telefon e număr) — iese DOAR sub o fațetă
                # declarată numerică de vocabular.
                return c.value if c.facet in v.numeric_facets else None
            return c.value if _safe_str(c.value, v.facet_values.get(c.facet)) else None

        safe_constraints = tuple(
            c.model_copy(update={"value": _value_of(c)})
            for c in self.constraints
            if v.knows_facet(c.facet)
            and c.op in _OPS
            and c.strength in _STRENGTHS
            and c.source in _SOURCES
        )
        return SafeQuerySpec(
            locale=self.locale if _safe_str(self.locale, v.locales) else None,
            intent=self.intent if _safe_str(self.intent, _INTENTS) else None,
            category=self.category if _safe_str(self.category, v.categories) else None,
            constraints=safe_constraints,
            reference_categories=tuple(
                rc for rc in self.reference_categories if _safe_str(rc, v.categories)
            ),
            sort=self.sort if _safe_str(self.sort, _SORTS) else "relevance",
        )
