"""NX-266 — o constrângere numerică e o VALOARE cu unitate, nu un cuvânt.

Azi constrângerile clientului sunt ETICHETE: `budget_max` are cod propriu, „SPF 50" e text care
ajunge în căutarea lexicală, „sub 60 cm" n-are unde să existe. Nicăieri în sistem nu există
noțiunea *valoare + unitate + operator*.

**De ce contează ÎNAINTE de reranker.** Un reranker citește text și dă un scor. Dacă „SPF minim 30"
e text, modelul va urca produse SPF 15 care vorbesc convingător despre protecție solară — scorul e
plauzibil, produsul e greșit, și **nicio poartă de adevăr nu-l prinde**: produsul chiar are SPF 15
și o spune cinstit. Un număr nu se negociază, deci se aplică în COD, de ambele părți ale
rerankerului: în SQL (ca să nu intre în pool) și peste lista finală (ca plasă, dacă rerankerul a
fost hrănit din altă cale).

Trei proprietăți nenegociabile:

1. **`Decimal`, nu `float`.** Prețul e bani, iar rotunjirea binară pe bani a fost deja o clasă de
   bug în proiect (NX-240 impune `Decimal` la afișare). `0.05 * 1000` în binar nu e 50.
2. **Unitate canonică per fațetă, cu tabelul de conversie în DATE.** `50 ml`, `0,05 l` și `5 cl`
   sunt aceeași constrângere. Factorii stau în pachetul de domeniu — un factor scris în cod ar fi
   scurgere de domeniu (P9, poarta NX-264) și, mai practic, ar presupune că toți tenanții măsoară
   la fel.
3. **`source`** — aceeași distincție ca la nevoi (NX-251): rostit de client (`user_explicit`) poate
   exclude; inferat de model (`model_inferred`) nu. Poarta e `corroborated_by`, refolosită, nu
   duplicată; aici doar transportăm verdictul ei.

**`UNKNOWN` nu exclude niciodată (D7).** Un produs care nu declară SPF nu e un produs cu SPF mic.
Politica pentru valoare lipsă e a FAȚETEI (`TypedFacet.missing_value`), nu a acestui modul: `skip`
înseamnă „fără valoare nu pot promite nimic" (prețul — un produs fără preț nu se poate vinde),
`unknown` înseamnă „nu știu, deci nu tai" (restul).

Totul aici e PUR: fără DB, fără I/O, fără ceas. SQL-ul îl construiește `db/queries/catalog.py` din
`BoundConstraint`, iar `src/tools/catalog_tools.py` aplică plasa de după rerankare.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from src.catalog.query_terms import comparators, fold
from src.domain.facets import FacetSource, FacetType, TypedFacet

log = logging.getLogger(__name__)

# Operatorii unei constrângeri numerice. `between` NU e un operator de fațetă (registrul NX-186 nu
# îl cunoaște) — e o COMPUNERE a două margini, verificată la legare ca `gte` ȘI `lte`.
OP_LTE = "lte"
OP_GTE = "gte"
OP_EQ = "eq"
OP_BETWEEN = "between"
OPS: frozenset[str] = frozenset({OP_LTE, OP_GTE, OP_EQ, OP_BETWEEN})

# De unde vine constrângerea. Aceeași scară ca la nevoi (NX-251), în aceeași ordine de tărie.
SOURCE_USER = "user_explicit"
SOURCE_MODEL = "model_inferred"
SOURCE_PAGE = "page_context"
SOURCES: frozenset[str] = frozenset({SOURCE_USER, SOURCE_MODEL, SOURCE_PAGE})
_SOURCE_RANK: Mapping[str, int] = {SOURCE_MODEL: 0, SOURCE_PAGE: 1, SOURCE_USER: 2}

# Motivele de RESPINGERE — vocabular ÎNCHIS. O constrângere respinsă nu se aplică aproximativ și
# nu dispare tăcut: apelantul primește motivul și îl poate raporta.
REASON_UNKNOWN_UNIT = "unknown_unit"
REASON_UNIT_AMBIGUOUS = "unit_ambiguous"
REASON_FACET_NOT_DECLARED = "facet_not_declared"
REASON_FACET_NOT_NUMERIC = "facet_not_numeric"
REASON_OP_NOT_ALLOWED = "operator_not_allowed"
REASON_NOT_A_NUMBER = "not_a_number"
REASON_CONFLICTING_BOUNDS = "conflicting_bounds"
REASON_INFERRED = "inferred_not_enforced"

# Sursele care au voie să EXCLUDĂ produse. Regula e a lui NX-251, refolosită: modelul transcrie,
# codul confirmă. O valoare pe care clientul n-a rostit-o e o inferență, iar o inferență n-are voie
# să șteargă produse din catalog — poate cel mult să le depuncteze la ranking.
ENFORCING_SOURCES: frozenset[str] = frozenset({SOURCE_USER, SOURCE_PAGE})

# Numerele dintr-un mesaj: „100", „89,90", „1.5". Separatorul zecimal poate fi virgulă (scriere
# românească) sau punct — dezambiguizarea e mai jos, în `_to_decimal`.
_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?")
# Un „cuvânt" de unitate lipit sau alăturat numărului (`50ml`, `50 ml`, `spf 30`).
_WORD_RE = re.compile(r"[a-z]+")
# Cât de departe de număr are voie să stea fraza de comparație. „sub 100 de lei" încape; o frază
# dintr-o propoziție anterioară, nu. Fereastra e în caractere, nu în cuvinte, ca să nu depindă de
# tokenizare.
_COMPARATOR_WINDOW = 24


class UnitConfigError(ValueError):
    """Config de unități invalid — intrarea e respinsă (fail-closed), nu corectată."""


@dataclass(frozen=True, slots=True)
class Rejection:
    """O constrângere care NU s-a aplicat, cu motivul. Există ca tip pentru că „respinsă tăcut" și
    „aplicată" arată identic în rezultate, iar diferența dintre ele e tot ce contează."""

    reason: str
    facet: str | None = None
    detail: str | None = None  # unitate/operator — vocabular de config, NICIODATĂ text de client


@dataclass(frozen=True, slots=True)
class UnitSpec:
    """Unitatea canonică a unei fațete + factorii de conversie, toți din DATE.

    `factors[alias] = câte unități canonice face un alias`. Canonicul are factorul 1 prin
    construcție (validat la load) — altfel conversia n-ar fi idempotentă."""

    facet: str
    canonical: str
    factors: Mapping[str, Decimal]
    # Operatorul folosit când clientul dă un număr FĂRĂ cuvânt de comparație. E o proprietate a
    # fațetei, nu a limbii: „crema 100 lei" înseamnă de obicei „până în", „SPF 30" înseamnă de
    # obicei „cel puțin". Declarat în pachet; fără el, `eq` (cea mai literală lectură).
    default_op: str = OP_EQ

    def to_canonical(self, value: Decimal, unit: str | None) -> Decimal | None:
        """Valoare + alias de unitate → valoare în unitatea canonică. `None` = alias necunoscut.

        `unit=None` înseamnă „număr fără unitate rostită" și se citește ca fiind deja canonic:
        „sub 100" într-un context de preț e 100 lei, nu o valoare fără dimensiune."""
        if unit is None:
            return value
        factor = self.factors.get(unit)
        if factor is None:
            return None
        return value * factor

    def from_canonical(self, value: Decimal, unit: str) -> Decimal | None:
        """Inversa — folosită de testul de round-trip și de orice afișare în unitatea rostită."""
        factor = self.factors.get(unit)
        if not factor:
            return None
        return value / factor


@dataclass(frozen=True, slots=True)
class UnitRegistry:
    """Tabelul de unități al tenantului + indexul invers alias → fațetă.

    Indexul invers e construit o dată, la load, și DROPează aliasurile ambigue (același cuvânt la
    două fațete): a ghici pe care o voia clientul ar produce un filtru greșit cu aer de certitudine.
    Un alias ambiguu iese ca `unit_ambiguous`, nu ca o alegere tăcută."""

    specs: Mapping[str, UnitSpec] = field(default_factory=dict)
    alias_facet: Mapping[str, str] = field(default_factory=dict)
    ambiguous: frozenset[str] = frozenset()

    def facet_for_unit(self, alias: str) -> str | None:
        return self.alias_facet.get(alias)


EMPTY_UNITS = UnitRegistry()


def _to_decimal(raw: str) -> Decimal | None:
    """Text de număr → `Decimal`. Virgula e separator ZECIMAL (scriere românească); punctul la fel.

    Nu tratăm „1.500" ca mie cinci sute: într-o constrângere, lectura greșită schimbă filtrul cu
    trei ordine de mărime. `corroborated_by` (NX-251) își permite ambiguitatea fiindcă doar
    confirmă că numărul a fost ROSTIT; aici numărul se EXECUTĂ."""
    try:
        return Decimal(raw.replace(",", "."))
    except (InvalidOperation, ValueError):
        return None


def _decimal_or_none(value: object) -> Decimal | None:
    """Orice valoare de config/produs → `Decimal`, sau `None` dacă nu e un număr.

    `float` trece prin `str` deliberat: `Decimal(0.05)` e 0.05000000000000000277…, iar
    `Decimal("0.05")` e 0,05. Pe bani diferența nu e teoretică."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        try:
            return Decimal(str(value))
        except InvalidOperation:
            return None
    if isinstance(value, str):
        text = value.strip()
        return _to_decimal(text) if _NUMBER_RE.fullmatch(text.replace(",", ".")) else None
    return None


def _build_unit_spec(facet: str, raw: Mapping[str, Any]) -> UnitSpec:
    canonical = raw.get("canonical")
    if not isinstance(canonical, str) or not canonical.strip():
        raise UnitConfigError(f"`canonical` lipsă pentru {facet!r}")
    canonical = fold(canonical)
    factors_raw = raw.get("factors")
    if not isinstance(factors_raw, Mapping) or not factors_raw:
        raise UnitConfigError(f"`factors` lipsă pentru {facet!r}")
    factors: dict[str, Decimal] = {}
    for alias, factor in factors_raw.items():
        if not isinstance(alias, str) or not alias.strip():
            continue
        dec = _decimal_or_none(factor)
        if dec is None or dec <= 0:
            raise UnitConfigError(f"factor invalid pentru {facet!r}/{alias!r}: {factor!r}")
        factors[fold(alias)] = dec
    if factors.get(canonical) != Decimal(1):
        raise UnitConfigError(
            f"unitatea canonică {canonical!r} a lui {facet!r} trebuie să aibă factorul 1 "
            "(altfel conversia nu e idempotentă)"
        )
    default_op = raw.get("default_op", OP_EQ)
    if default_op not in (OP_LTE, OP_GTE, OP_EQ):
        raise UnitConfigError(f"`default_op` invalid pentru {facet!r}: {default_op!r}")
    return UnitSpec(facet=facet, canonical=canonical, factors=factors, default_op=default_op)


def build_units(raw: Any) -> UnitRegistry:
    """Config → registru de unități. **Fail-closed per intrare:** o fațetă cu tabel invalid e
    respinsă (logată), restul rămân. Gol/gunoi → registru gol, adică extracția nu produce nimic și
    comportamentul e cel de azi (P6)."""
    if not isinstance(raw, Mapping):
        return EMPTY_UNITS
    specs: dict[str, UnitSpec] = {}
    for facet, entry in raw.items():
        if not isinstance(facet, str) or facet.startswith("_") or not isinstance(entry, Mapping):
            continue
        try:
            specs[facet] = _build_unit_spec(facet, entry)
        except UnitConfigError as e:  # fail-closed: o intrare stricată nu dărâmă tabelul
            log.warning("unități respinse la load (fail-closed): %s", e)
    alias_facet: dict[str, str] = {}
    ambiguous: set[str] = set()
    for facet, spec in specs.items():
        for alias in spec.factors:
            if alias in ambiguous:
                continue
            if alias in alias_facet and alias_facet[alias] != facet:
                del alias_facet[alias]
                ambiguous.add(alias)
                log.warning("alias de unitate AMBIGUU, dropat: %r", alias)
                continue
            alias_facet[alias] = facet
    return UnitRegistry(specs=specs, alias_facet=alias_facet, ambiguous=frozenset(ambiguous))


@dataclass(frozen=True, slots=True)
class TypedConstraint:
    """O constrângere numerică, normalizată la unitatea canonică a fațetei.

    `value` e marginea; pentru `between` e marginea de JOS, iar `value_max` cea de sus. Un singur
    câmp n-ar fi ajuns: „peste 30, sub 50" e o cerere, nu două, iar tratarea lor ca două
    constrângeri independente ar fi lăsat ultima să câștige."""

    facet: str
    op: str
    value: Decimal
    unit: str
    source: str = SOURCE_MODEL
    value_max: Decimal | None = None

    def describe(self, label: str | None = None) -> str:
        """Descriere scurtă, pentru nota către model. SIMBOLICĂ, nu în cuvinte: „≤ 100 lei" e
        lizibil în orice locale, iar „cel mult" ar fi hardcodat româna într-un modul care nu are
        voie s-o cunoască (P11). Conține numărul CLIENTULUI, fiindcă nota merge la model, nu în
        analytics (P12 privește evenimentele)."""
        name = label or self.facet
        if self.op == OP_BETWEEN and self.value_max is not None:
            return f"{name} {_fmt(self.value)}..{_fmt(self.value_max)} {self.unit}"
        symbol = {OP_LTE: "≤", OP_GTE: "≥", OP_EQ: "="}[self.op]
        return f"{name} {symbol} {_fmt(self.value)} {self.unit}"


def _fmt(value: Decimal) -> str:
    """Zecimalele care nu spun nimic se taie („50.00" → „50"), restul rămân exacte."""
    return format(value.normalize(), "f")


@dataclass(frozen=True, slots=True)
class BoundConstraint:
    """O constrângere LEGATĂ de fațeta ei: unde stă valoarea în produs și ce se face când lipsește.

    Legarea e locul unde o constrângere devine executabilă, și tot aici se opresc cele care nu pot
    fi: fațetă nedeclarată, fațetă ne-numerică, operator nepermis. Din acest tip citesc și SQL-ul,
    și plasa de după rerankare — o singură definiție, două puncte de aplicare."""

    constraint: TypedConstraint
    source_kind: str  # FacetSource.COLUMN.value | FacetSource.ATTRIBUTE.value
    source_key: str  # coloană sau cheie de `attributes` (validată de registrul de fațete)
    missing_policy: str  # unknown | skip | false — de la `TypedFacet.missing_value`

    @property
    def facet(self) -> str:
        return self.constraint.facet

    @property
    def keeps_unknown(self) -> bool:
        """D7 literal: doar `unknown` păstrează produsele fără valoare. `skip`/`false` sunt
        declarații ale fațetei că absența valorii e ea însăși descalificantă (prețul)."""
        return self.missing_policy == "unknown"


# --- extragere din mesaj -----------------------------------------------------------------------


def _comparator_before(
    text: str, start: int, phrases: Sequence[tuple[str, str]]
) -> tuple[str, int] | None:
    """Operatorul rostit ÎNAINTEA numărului → `(op, poziția lui în text)`.

    Doar înainte: în română comparația precedă valoarea („sub 100 lei"), iar acceptarea celei de
    după ar face ca „100 lei, sub ce ai mai ieftin?" să devină o constrângere pe alt număr. Cazurile
    postpuse sunt rare și degradează spre operatorul implicit, nu spre unul greșit.

    Poziția e întoarsă fiindcă unitatea poate sta ÎNAINTEA comparatorului („spf minim 30"): fără
    ea, căutarea unității ar da peste cuvântul de comparație și s-ar opri."""
    left = max(0, start - _COMPARATOR_WINDOW)
    window = text[left:start]
    best: tuple[int, str] | None = None
    for phrase, op in phrases:  # deja sortate lung → scurt
        idx = window.rfind(phrase)
        if idx < 0:
            continue
        # granițe de cuvânt: „subtil 100" nu e „sub 100"
        after = idx + len(phrase)
        if after < len(window) and window[after].isalnum():
            continue
        if idx > 0 and window[idx - 1].isalnum():
            continue
        if best is None or idx > best[0]:
            best = (idx, op)
    return (best[1], left + best[0]) if best else None


def _alias_at(text: str, units: UnitRegistry) -> tuple[str | None, bool]:
    """Ultimul cuvânt al unui fragment, dacă e alias de unitate → `(alias, ambiguu)`."""
    trimmed = text.rstrip()
    reversed_word = _WORD_RE.search(trimmed[::-1])
    if not reversed_word or reversed_word.start() != 0:
        return None, False
    alias = reversed_word.group(0)[::-1]
    if alias in units.ambiguous:
        return None, True
    return (alias, False) if units.facet_for_unit(alias) else (None, False)


def _unit_near(
    text: str, start: int, end: int, head_end: int, units: UnitRegistry
) -> tuple[str | None, bool]:
    """Aliasul de unitate lipit de număr → `(alias, ambiguu)`.

    Se caută în ambele direcții pentru că limba le pune în ambele: „50 ml" (după) și „spf 30"
    (înainte). Cea de după se încearcă prima — e forma dominantă. `head_end` marchează unde se
    termină partea utilă din stânga: dacă între unitate și număr s-a interpus un cuvânt de
    comparație („spf minim 30"), căutarea sare peste el."""
    tail = _WORD_RE.match(text[end:].lstrip())
    if tail:
        alias = tail.group(0)
        if alias in units.ambiguous:
            return None, True
        if units.facet_for_unit(alias):
            return alias, False
    alias, ambiguous = _alias_at(text[:head_end], units)
    if alias or ambiguous:
        return alias, ambiguous
    # `head_end < start` doar când s-a sărit peste un comparator; încercăm și lipirea directă.
    return _alias_at(text[:start], units) if head_end != start else (None, False)


def extract_constraints(
    message: str,
    *,
    units: UnitRegistry,
    locale: str | None,
    source: str = SOURCE_USER,
) -> tuple[tuple[TypedConstraint, ...], tuple[Rejection, ...]]:
    """Mesajul BRUT → constrângeri tipizate. Determinist, agnostic de limbă, fără LLM.

    Rețeta e „număr + unitate + cuvânt de comparație", unde fiecare bucată vine din altă sursă:
    numărul din mesaj, unitatea din pachetul tenantului, comparația din vocabularul locale-i
    (`catalog.query_terms`). Niciuna nu e scrisă în modulul ăsta — de aia ține pe orice vertical.

    Un număr fără unitate recunoscută NU produce constrângere: „am 2 copii" nu e un buget. Asta e
    și motivul pentru care registrul de unități e cheia întregii extrageri — fără el nu putem
    distinge o valoare dintr-o cifră."""
    if not message or not units.specs:
        return (), ()
    text = fold(message)
    phrases = comparators(locale)
    found: list[TypedConstraint] = []
    rejected: list[Rejection] = []
    for match in _NUMBER_RE.finditer(text):
        raw_value = _to_decimal(match.group(0))
        if raw_value is None:
            continue
        comparator = _comparator_before(text, match.start(), phrases)
        head_end = comparator[1] if comparator else match.start()
        alias, ambiguous = _unit_near(text, match.start(), match.end(), head_end, units)
        if ambiguous:
            rejected.append(Rejection(REASON_UNIT_AMBIGUOUS))
            continue
        if alias is None:
            continue
        facet = units.facet_for_unit(alias)
        spec = units.specs.get(facet or "")
        if spec is None:  # index invers desincronizat de specs — imposibil azi, fail-closed oricum
            rejected.append(Rejection(REASON_UNKNOWN_UNIT, facet=facet, detail=alias))
            continue
        canonical_value = spec.to_canonical(raw_value, alias)
        if canonical_value is None:
            rejected.append(Rejection(REASON_UNKNOWN_UNIT, facet=spec.facet, detail=alias))
            continue
        op = comparator[0] if comparator else spec.default_op
        found.append(
            TypedConstraint(
                facet=spec.facet,
                op=op,
                value=canonical_value,
                unit=spec.canonical,
                source=source,
            )
        )
    return tuple(found), tuple(rejected)


def constraint_from_value(
    facet: str,
    op: str,
    value: object,
    *,
    units: UnitRegistry,
    source: str,
) -> tuple[TypedConstraint | None, Rejection | None]:
    """Un număr deja STRUCTURAT (slot de triaj, `price_max` de la model, o valoare din contextul
    paginii) → constrângere tipizată în unitatea canonică a fațetei.

    Valoarea vine gata extrasă, deci nu trece prin parsare de text — dar trece prin ACELEAȘI
    validări: fațeta trebuie să aibă unitate declarată, iar numărul trebuie să fie un număr. Un
    `price_max` care ajunge aici ca „ieftin" e respins, nu convertit în zero."""
    spec = units.specs.get(facet)
    if spec is None:
        return None, Rejection(REASON_FACET_NOT_DECLARED, facet=facet, detail="units")
    if op not in OPS:
        return None, Rejection(REASON_OP_NOT_ALLOWED, facet=facet, detail=str(op))
    number = _decimal_or_none(value)
    if number is None:
        return None, Rejection(REASON_NOT_A_NUMBER, facet=facet)
    if source not in SOURCES:
        source = SOURCE_MODEL
    return TypedConstraint(facet, op, number, spec.canonical, source), None


def merge_constraints(
    constraints: Iterable[TypedConstraint],
) -> tuple[tuple[TypedConstraint, ...], tuple[Rejection, ...]]:
    """Mai multe constrângeri pe aceeași fațetă → UNA. „peste 30, sub 50" e `between`, nu „sub 50".

    Reguli, în ordine:
      • `eq` bate marginile — e cea mai specifică lectură a cererii;
      • marginile se strâng (cel mai mare `gte`, cel mai mic `lte`): două praguri rostite sunt
        cumulative, nu alternative;
      • `gte > lte` = cerere imposibilă → RESPINSĂ cu motiv, nu „aproximată" spre una dintre ele;
      • sursa rezultatului e cea mai TARE dintre cele compuse: dacă o margine a fost rostită de
        client, constrângerea rezultată e a clientului."""
    by_facet: dict[str, list[TypedConstraint]] = {}
    for c in constraints:
        if c.op not in OPS or c.source not in SOURCES:
            continue
        by_facet.setdefault(c.facet, []).append(c)

    out: list[TypedConstraint] = []
    rejected: list[Rejection] = []
    for facet in sorted(by_facet):
        group = by_facet[facet]
        unit = group[0].unit
        source = max((c.source for c in group), key=lambda s: _SOURCE_RANK[s])
        eq = next((c for c in group if c.op == OP_EQ), None)
        if eq is not None:
            out.append(
                TypedConstraint(facet, OP_EQ, eq.value, unit, source) if source != eq.source else eq
            )
            continue
        lows = [c.value for c in group if c.op in (OP_GTE, OP_BETWEEN)]
        highs = [c.value_max for c in group if c.op == OP_BETWEEN and c.value_max is not None]
        highs += [c.value for c in group if c.op == OP_LTE]
        lo = max(lows) if lows else None
        hi = min(highs) if highs else None
        if lo is not None and hi is not None:
            if lo > hi:
                rejected.append(Rejection(REASON_CONFLICTING_BOUNDS, facet=facet))
                continue
            out.append(TypedConstraint(facet, OP_BETWEEN, lo, unit, source, value_max=hi))
        elif lo is not None:
            out.append(TypedConstraint(facet, OP_GTE, lo, unit, source))
        elif hi is not None:
            out.append(TypedConstraint(facet, OP_LTE, hi, unit, source))
    return tuple(out), tuple(rejected)


# --- legarea de fațete -------------------------------------------------------------------------


def _ops_required(op: str) -> tuple[str, ...]:
    """Ce operatori de FAȚETĂ cere un operator de constrângere. `between` e o compunere, deci cere
    ambele margini — o fațetă care permite doar `lte` nu poate purta un interval."""
    return (OP_GTE, OP_LTE) if op == OP_BETWEEN else (op,)


def bind_constraints(
    constraints: Iterable[TypedConstraint], facets: Sequence[TypedFacet]
) -> tuple[tuple[BoundConstraint, ...], tuple[Rejection, ...]]:
    """Constrângeri → constrângeri EXECUTABILE, contra registrului de fațete (NX-186).

    Aici se opresc cererile care n-au unde să fie evaluate. Fiecare oprire are motiv, fiindcă un
    filtru care „n-a rulat" tăcut e mai periculos decât unul care a rulat greșit: clientul primește
    rezultate care arată exact ca cele corecte."""
    by_key = {f.key: f for f in facets}
    bound: list[BoundConstraint] = []
    rejected: list[Rejection] = []
    for c in constraints:
        spec = by_key.get(c.facet)
        if spec is None:
            rejected.append(Rejection(REASON_FACET_NOT_DECLARED, facet=c.facet))
            continue
        if spec.value_type is not FacetType.NUMBER:
            rejected.append(
                Rejection(REASON_FACET_NOT_NUMERIC, facet=c.facet, detail=spec.value_type.value)
            )
            continue
        if any(not spec.allows(op) for op in _ops_required(c.op)):
            rejected.append(Rejection(REASON_OP_NOT_ALLOWED, facet=c.facet, detail=c.op))
            continue
        if spec.source is FacetSource.CATEGORY:  # o categorie nu e o măsurătoare
            rejected.append(Rejection(REASON_FACET_NOT_NUMERIC, facet=c.facet, detail="category"))
            continue
        bound.append(
            BoundConstraint(
                constraint=c,
                source_kind=spec.source.value,
                source_key=spec.source_key,
                missing_policy=spec.missing_value,
            )
        )
    return tuple(bound), tuple(rejected)


# --- evaluarea peste produse (plasa de după rerankare) -------------------------------------------

MATCH = "match"
MISMATCH = "mismatch"
UNKNOWN = "unknown"


def product_value(bound: BoundConstraint, product: Mapping[str, Any]) -> Decimal | None:
    """Valoarea fațetei pe un produs, ca `Decimal`. `None` = necunoscută SAU necitibilă ca număr.

    Un atribut care există dar nu e număr („SPF: «ridicat»") întoarce `None` deliberat: nu e o
    valoare mică, e o valoare pe care n-o putem compara. D7 se aplică la fel."""
    if bound.source_kind == FacetSource.COLUMN.value:
        return _decimal_or_none(product.get(bound.source_key))
    attributes = product.get("attributes")
    if not isinstance(attributes, Mapping):
        return None
    return _decimal_or_none(attributes.get(bound.source_key))


def evaluate(bound: BoundConstraint, product: Mapping[str, Any]) -> str:
    """`match` / `mismatch` / `unknown` — tri-state, niciodată boolean.

    Boolean-ul ar fi fost minciuna: ar fi forțat „nu știu" să devină ori „da" (promitem ce nu
    știm), ori „nu" (aruncăm produse bune). Verdictul rămâne tri-state până la ultimul consumator,
    care decide după `missing_policy` ce face cu al treilea."""
    value = product_value(bound, product)
    if value is None:
        return UNKNOWN
    c = bound.constraint
    if c.op == OP_LTE:
        ok = value <= c.value
    elif c.op == OP_GTE:
        ok = value >= c.value
    elif c.op == OP_EQ:
        ok = value == c.value
    else:  # between
        ok = value >= c.value and (c.value_max is None or value <= c.value_max)
    return MATCH if ok else MISMATCH


def apply_constraints(
    products: Sequence[Mapping[str, Any]], bounds: Sequence[BoundConstraint]
) -> tuple[list[Mapping[str, Any]], dict[str, dict[str, int]]]:
    """Plasa de DUPĂ rerankare: scoate produsele care contrazic un număr, păstrează necunoscutele.

    Rulează peste lista FINALĂ, chiar dacă SQL-ul a filtrat deja același lucru. Nu e redundanță:
    candidații pot veni din altă cale (rehidratare, pool de sesiune, substituți), iar un reranker
    are voie să reordoneze, nu să anuleze o constrângere. Costul e o trecere liniară.

    Întoarce și numărătoarea per fațetă — `matched/mismatched/unknown` — pentru evenimentul
    `constraint_applied`. Fără ea, un filtru care taie tot ar arăta ca un catalog gol."""
    stats: dict[str, dict[str, int]] = {
        b.facet: {MATCH: 0, MISMATCH: 0, UNKNOWN: 0} for b in bounds
    }
    if not bounds:
        return list(products), stats
    kept: list[Mapping[str, Any]] = []
    for product in products:
        drop = False
        for bound in bounds:
            verdict = evaluate(bound, product)
            stats[bound.facet][verdict] += 1
            if verdict == MISMATCH or (verdict == UNKNOWN and not bound.keeps_unknown):
                drop = True
        if not drop:
            kept.append(product)
    return kept, stats
