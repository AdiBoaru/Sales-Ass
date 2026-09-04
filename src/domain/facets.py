"""NX-186 — Registru de fațete TIPIZATE (typed facet registry).

Extinde `DomainPack.searchable_facets` (azi `tuple[str]`) la fațete cu TIP, operatori și politică de
`missing_value` — contractul de care are nevoie QuerySpec (NX-208) ca să verifice constrângeri și
Match Gate (NX-187) ca să dea MATCH / MISMATCH / UNKNOWN. Aditiv: fațetele netipizate rămân ca azi
până sunt migrate.

**Siguranță (invariant central):** config-ul NU poate conține SQL arbitrar sau JSON paths
interpolate. Sursa unei fațete e validată contra unui **allowlist din COD**:
  - `column`    → doar coloane din `_SAFE_COLUMNS` (preț/rating/stoc), citite parametrizat;
  - `category`  → fix (primary_category_id → categories.slug);
  - `attribute` → orice cheie, dar citită parametrizat `attributes->$key`, iar cheia trebuie să
                  respecte `_KEY_RE` (litere/cifre/underscore) — niciun path, nicio expresie.
Orice fațetă cu sursă/tip/operator/cheie invalidă e **respinsă la load** (fail-closed): nu devine
TypedFacet, deci nu poate ajunge la enforcement. Nimic din config nu se interpolează în SQL.

Generic pe vertical (P9): fațetele vin din DomainPack (defaults JSON + override per-tenant), nu
hardcodate. Codul definește DOAR regulile de siguranță + tipurile, nu lista de fațete a unui pack.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

log = logging.getLogger(__name__)


class FacetType(str, Enum):
    BOOL = "bool"
    ENUM = "enum"
    NUMBER = "number"
    TEXT = "text"
    LIST = "list"


class FacetSource(str, Enum):
    COLUMN = "column"  # coloană din `products` (whitelist)
    CATEGORY = "category"  # primary_category_id → categories.slug
    ATTRIBUTE = "attribute"  # products.attributes->key (parametrizat)


# Coloane din `products` care pot fi fațete `column` (whitelist DUR — nimic altceva nu e citit ca
# coloană; orice altă „column" e respinsă). Preț-ul efectiv se calculează în SQL (sale_price), dar
# fațeta expune `price` ca semnal numeric.
_SAFE_COLUMNS: frozenset[str] = frozenset({"price", "sale_price", "rating", "stock_total"})

# Operatori permiși per tip (Match Gate-ul NX-187 nu poate cere un operator invalid pe un tip).
_OPERATORS_BY_TYPE: dict[FacetType, frozenset[str]] = {
    FacetType.BOOL: frozenset({"eq"}),
    FacetType.ENUM: frozenset({"eq"}),
    FacetType.NUMBER: frozenset({"eq", "lte", "gte"}),
    FacetType.TEXT: frozenset({"eq", "contains"}),
    FacetType.LIST: frozenset({"contains"}),
}

# Cheia unei surse (coloană / atribut) — DOAR identificatori siguri. Blochează JSON paths
# („a->b"), expresii, spații, ghilimele → niciun vector de interpolare SQL.
_KEY_RE = re.compile(r"^[a-z_][a-z0-9_]*$")

# Politici pentru valoare lipsă (D7: UNKNOWN ≠ MISMATCH).
_MISSING_POLICIES: frozenset[str] = frozenset({"unknown", "false", "skip"})

# Provenance-ul unei fațete (D5: confirmat ≠ derivat). `claim` = afirmație care cere sursă
# (fragrance_free, key_ingredients) → „verified" cere claim_provenance; `structural` = fapt
# de structură (preț/finish/spf) — self-evident, „verified" = „valid".
_PROVENANCE_KINDS: frozenset[str] = frozenset({"structural", "claim"})

# NX-257 — ce ÎNSEAMNĂ un MISMATCH pe fațeta asta. Singura declarație care decide dacă o valoare
# afirmată de client poate EXCLUDE produse, și e o proprietate a fațetei, nu a frazei sau a limbii:
#
#   • "partitioning" — cumpărătorul are exact UNA dintre valori (tip de ten, mărime, tip de motor,
#     sistem de operare, voltaj). Un produs cu altă valoare îl CONTRAZICE ⇒ excluderea e corectă.
#   • "additive"     — valoarea e un obiectiv/beneficiu/utilizare (riduri, luminozitate, „bun de
#     drum lung"). Un produs care n-o poartă nu contrazice nimic, doar nu o țintește ⇒ NICIODATĂ
#     exclus, doar depunctat la ranking.
#
# Default `additive`: o fațetă care nu declară nimic nu capătă putere de excludere prin tăcere.
_BINDING_KINDS: frozenset[str] = frozenset({"partitioning", "additive"})


class FacetConfigError(ValueError):
    """Config de fațetă invalid — fail-closed (fațeta e respinsă, nu încărcată)."""


@dataclass(frozen=True)
class TypedFacet:
    """O fațetă tipizată, validată la load. Imutabilă. `source_key` e garantat sigur (allowlist +
    `_KEY_RE`) — se poate folosi parametrizat în SQL fără risc de injecție."""

    key: str  # cheia canonică a fațetei (identificator)
    value_type: FacetType
    source: FacetSource
    source_key: str  # coloana / cheia de atribut de citit (validată)
    operators: tuple[str, ...]
    values: tuple[str, ...] = ()  # enum/list: valori canonice permise (gol = orice)
    aliases: dict[str, str] = field(default_factory=dict)  # alias normalizat → valoare canonică
    list_semantics: str = "any"  # list: "any" (oricare) | "all" (toate)
    missing_value: str = "unknown"  # unknown | false | skip
    provenance: str = "structural"  # structural | claim
    min_coverage: float = 0.0  # prag minim de coverage pt enforcement (NX-188)
    # NX-257: partitioning | additive — vezi `_BINDING_KINDS`. Doar `partitioning` poate exclude.
    binding: str = "additive"
    # NX-271: fațeta a trecut AUDITUL DE PRECIZIE (NX-268)? Default `False`, și defaultul e toată
    # ideea: o fațetă derivată dintr-un text nu capătă drept de excludere pentru că are acoperire
    # mare, ci pentru că un om a verificat un eșantion și precizia a trecut pragul preînregistrat.
    # Acoperirea spune câte produse poartă o valoare; precizia spune câte o poartă pe bună dreptate,
    # iar la excludere doar a doua contează: un filtru dur peste un atribut cu precizie 70% șterge
    # TĂCUT produse corecte, iar clientul nu vede o eroare, vede mai puține opțiuni și pleacă.
    enforce_ready: bool = False
    labels: dict[str, str] = field(
        default_factory=dict
    )  # locale → etichetă display (absoarbe NX-182)

    def label(self, locale: str, *, fallback_locale: str | None = None) -> str:
        """Etichetă de afișare per-locale. Fallback: `locale` → `fallback_locale` (dacă e dat) →
        cheia fațetei. NU hardcodează o limbă (P11/D3) — `fallback_locale` îl dă apelantul din
        `business.default_locale`, nu presupunem română în cod."""
        if locale in self.labels:
            return self.labels[locale]
        if fallback_locale and fallback_locale in self.labels:
            return self.labels[fallback_locale]
        return self.key

    def allows(self, op: str) -> bool:
        return op in self.operators


def _validate_source(source: FacetSource, source_key: str) -> None:
    """Fail-closed: sursa + cheia trebuie să fie sigure. Ridică FacetConfigError altfel."""
    if source in (FacetSource.COLUMN, FacetSource.ATTRIBUTE):
        if not isinstance(source_key, str) or not _KEY_RE.match(source_key):
            raise FacetConfigError(f"source_key nesigur (path/SQL/gol?): {source_key!r}")
    if source is FacetSource.COLUMN and source_key not in _SAFE_COLUMNS:
        raise FacetConfigError(
            f"coloană nelistată: {source_key!r} (permise: {sorted(_SAFE_COLUMNS)})"
        )


def _build_one(raw: dict[str, Any]) -> TypedFacet:
    """Construiește + validează O fațetă. Ridică FacetConfigError pe orice invaliditate."""
    key = raw.get("key")
    if not isinstance(key, str) or not _KEY_RE.match(key):
        raise FacetConfigError(f"key invalid: {key!r}")
    try:
        vtype = FacetType(raw.get("value_type"))
        source = FacetSource(raw.get("source"))
    except ValueError as e:
        raise FacetConfigError(f"value_type/source invalid pt {key!r}: {e}") from e

    source_key = raw.get("source_key") or key
    if source is FacetSource.CATEGORY:
        source_key = "primary_category_id"  # fix, nu din config (nu se interpolează)
    else:
        _validate_source(source, source_key)

    allowed_ops = _OPERATORS_BY_TYPE[vtype]
    ops_raw = raw.get("operators") or sorted(allowed_ops)
    if not isinstance(ops_raw, list) or not all(isinstance(o, str) for o in ops_raw):
        raise FacetConfigError(f"operators invalizi pt {key!r}")
    bad = [o for o in ops_raw if o not in allowed_ops]
    if bad:
        raise FacetConfigError(f"operatori nepermiși pe {vtype.value} ({key!r}): {bad}")

    missing = raw.get("missing_value", "unknown")
    if missing not in _MISSING_POLICIES:
        raise FacetConfigError(f"missing_value invalid pt {key!r}: {missing!r}")
    provenance = raw.get("provenance", "structural")
    if provenance not in _PROVENANCE_KINDS:
        raise FacetConfigError(f"provenance invalid pt {key!r}: {provenance!r}")
    binding = raw.get("binding", "additive")
    if binding not in _BINDING_KINDS:
        raise FacetConfigError(f"binding invalid pt {key!r}: {binding!r}")
    enforce_ready = raw.get("enforce_ready", False)
    if not isinstance(enforce_ready, bool):
        raise FacetConfigError(f"enforce_ready invalid pt {key!r}: {enforce_ready!r}")

    try:
        min_cov = float(raw.get("min_coverage", 0.0))
    except (TypeError, ValueError) as e:
        raise FacetConfigError(f"min_coverage invalid pt {key!r}") from e
    if not 0.0 <= min_cov <= 1.0:
        raise FacetConfigError(f"min_coverage în afara [0,1] pt {key!r}: {min_cov}")

    # Colecțiile trebuie să fie de tipul corect — altfel FacetConfigError (fațeta e DROPATĂ,
    # nu crapă tot load-ul). `.items()` pe un ne-dict ar arunca AttributeError = load întreg picat.
    values_raw = raw.get("values") or []
    if not isinstance(values_raw, list):
        raise FacetConfigError(f"values nu e listă pt {key!r}")
    values = tuple(v for v in values_raw if isinstance(v, str))
    aliases_raw = raw.get("aliases") or {}
    if not isinstance(aliases_raw, dict):
        raise FacetConfigError(f"aliases nu e dict pt {key!r}")
    aliases = {
        str(a): str(c) for a, c in aliases_raw.items() if isinstance(a, str) and isinstance(c, str)
    }
    list_sem = raw.get("list_semantics", "any")
    if list_sem not in ("any", "all"):
        raise FacetConfigError(f"list_semantics invalid pt {key!r}: {list_sem!r}")
    labels_raw = raw.get("labels") or {}
    if not isinstance(labels_raw, dict):
        raise FacetConfigError(f"labels nu e dict pt {key!r}")
    labels = {
        str(loc): str(txt)
        for loc, txt in labels_raw.items()
        if isinstance(loc, str) and isinstance(txt, str)
    }

    return TypedFacet(
        key=key,
        value_type=vtype,
        source=source,
        source_key=source_key,
        operators=tuple(ops_raw),
        values=values,
        aliases=aliases,
        list_semantics=list_sem,
        missing_value=missing,
        provenance=provenance,
        min_coverage=min_cov,
        binding=binding,
        enforce_ready=enforce_ready,
        labels=labels,
    )


def build_facets(raw: Any) -> tuple[TypedFacet, ...]:
    """Config listă → registru tipizat. **Fail-closed per fațetă:** o intrare invalidă e RESPINSĂ
    (logată, nu încărcată) — nu devine TypedFacet, deci nu ajunge la enforcement. Duplicat de cheie
    → păstrează prima. Gol/gunoi → `()` (fără fațete tipizate, comportament de azi, P6)."""
    if not isinstance(raw, list):
        return ()
    out: list[TypedFacet] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        try:
            spec = _build_one(entry)
        except Exception as e:  # noqa: BLE001 — fail-closed: nicio fațetă nu dărâmă load-ul pack-ului
            log.warning("facet respinsă la load (fail-closed): %s", e)
            continue
        if spec.key in seen:
            log.warning("facet duplicată ignorată: %s", spec.key)
            continue
        seen.add(spec.key)
        out.append(spec)
    return tuple(out)


def extract_value(spec: TypedFacet, product: dict[str, Any], attributes: dict[str, Any]) -> Any:
    """Citește valoarea BRUTĂ a fațetei dintr-un produs (coverage / evaluare). `None` = lipsă.
    `product` = rândul (`price`/`category_slug`/...), `attributes` = attributes deja parsat."""
    if spec.source is FacetSource.COLUMN:
        return product.get(spec.source_key)
    if spec.source is FacetSource.CATEGORY:
        return product.get("category_slug")
    return attributes.get(spec.source_key)


def is_valid_value(spec: TypedFacet, value: Any) -> bool:
    """True dacă valoarea e prezentă ȘI validă pentru tipul/valorile fațetei (nu doar prezentă)."""
    if value is None:
        return False
    if spec.value_type is FacetType.BOOL:
        return isinstance(value, bool)
    if spec.value_type is FacetType.NUMBER:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if spec.value_type is FacetType.LIST:
        if not isinstance(value, list) or not value:
            return False
        if spec.values:
            return all(isinstance(v, str) and v in spec.values for v in value)
        return all(isinstance(v, str) for v in value)
    # ENUM / TEXT
    if not isinstance(value, str) or not value.strip():
        return False
    if spec.value_type is FacetType.ENUM and spec.values:
        return value in spec.values
    return True
