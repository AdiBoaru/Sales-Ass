"""NX-235 — vocabularul de NEVOI: ce poate memora conversația și sub ce formă canonică.

Problema pe care o rezolvă modulul: azi `constraints` și `search_constraints` sunt dicționare
libere. `constraints[field] = answer` scrie RĂSPUNSUL BRUT al clientului („pentru sora mea, are
tenul mixt, ceva sub 200") drept valoare de stare — adică exact utterance-ul, cu tot ce poate
conține el (nume, vârstă, detaliu medical). Nimic din formă nu spune dacă e o limită inviolabilă
sau o preferință, cine a afirmat-o și dacă mai e valabilă.

Aici valoarea trece printr-o poartă: ori se normalizează într-un TOKEN CANONIC din vocabularul
businessului, ori nu intră deloc ca fapt — devine `unknown`. `UNKNOWN != MISMATCH`: „nu știu ce a
vrut să zică" nu are voie să devină „a zis nu". Un răspuns liber nu se pierde pentru tur (agentul
îl vede în mesajul curent), doar nu devine MEMORIE.

**Vocabularul nu e hardcodat de vertical (P9/D3).** Nucleul universal de mai jos e valabil pe orice
comerț (buget, brand, mărime, destinatar); restul cheilor vin din `DomainPack` — fațete tipizate
(NX-186) și `searchable_facets`. Un vertical nou = config, nu deploy. Nicio enumerare de beauty în
kernel.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

from src.catalog.folding import fold_text

if TYPE_CHECKING:
    from src.domain.pack import DomainPack

# Caps (P4 — bugetul e în cod, nu în prompt). O valoare canonică e un token, nu o propoziție:
# peste atât înseamnă că am primit text liber și nu avem ce memora.
MAX_VALUE_CHARS = 48
MAX_VALUE_WORDS = 4
# Domeniul plauzibil al unei limite numerice de preț. În afara lui numărul nu e o măsurătoare, e
# altceva (un telefon, un an, un id) — aceeași logică ca la benzile din `query_spec` (NX-208).
MAX_NUMERIC_VALUE = 1_000_000.0

_TOKEN_RE = re.compile(r"^[a-z0-9][a-z0-9 _+/-]*$")
_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?")


class NeedKind(str, Enum):
    """Ce FEL de nevoie e — determină operatorul canonic și cum se normalizează valoarea."""

    NUMERIC_MAX = "numeric_max"  # plafon („sub 200") → operator `lte`
    NUMERIC_MIN = "numeric_min"  # prag („de la 100") → operator `gte`
    SCALAR = "scalar"  # o valoare din vocabular („brand: X") → `eq`
    LIST = "list"  # apartenență la o mulțime („concerns: acnee") → `contains`
    EXCLUSION = "exclusion"  # excludere („fără parfum") → `not_contains`
    BOOLEAN = "boolean"  # fanion („fragrance_free") → `eq`


# Operatorul canonic per fel de nevoie. Vocabular ÎNCHIS, comun cu `query_spec.Constraint.op`
# (NX-208) — o nevoie se proiectează în constrângere de căutare fără traducere ad-hoc.
OPERATOR_BY_KIND: Mapping[NeedKind, str] = {
    NeedKind.NUMERIC_MAX: "lte",
    NeedKind.NUMERIC_MIN: "gte",
    NeedKind.SCALAR: "eq",
    NeedKind.LIST: "contains",
    NeedKind.EXCLUSION: "not_contains",
    NeedKind.BOOLEAN: "eq",
}

# Tăriile permise. `hard` = inviolabil de model (D7); `soft` = preferință care influențează ranking.
HARD = "hard"
SOFT = "soft"


@dataclass(frozen=True)
class NeedSpec:
    """Contractul unei chei de nevoie: fel, tărie implicită, dacă e scope-uită pe subiect și —
    când businessul l-a declarat — vocabularul ÎNCHIS de valori.

    `scoped=True` înseamnă „valabilă doar cât timp vorbim despre categoria asta": un buget declarat
    pentru îngrijirea tenului nu plafonează un laptop. `scoped=False` = fapt despre PERSOANĂ
    (mărime, destinatar, restricție), care supraviețuiește schimbării de subiect.

    `default_strength=hard` DOAR pentru limite și excluderi — singurele care, încălcate, produc un
    răspuns greșit, nu doar unul mai puțin potrivit."""

    key: str
    kind: NeedKind
    default_strength: str = SOFT
    scoped: bool = True
    values: frozenset[str] = frozenset()  # gol = vocabular deschis (token canonic acceptat)
    aliases: Mapping[str, str] = field(default_factory=dict)

    @property
    def operator(self) -> str:
        return OPERATOR_BY_KIND[self.kind]


# Nucleul UNIVERSAL — valabil pe orice comerț, agnostic de vertical (P9). Cheile sunt aceleași pe
# care le folosesc deja triajul (`RouteDecision.filters`), clarify (`canonicalize_clarify_field`) și
# stiva NX-133, ca migrarea să fie o traducere, nu o reinventare de vocabular.
#: NX-331 (I25, partea de persistență): cheia semnalelor pe care nicio dimensiune nu le prinde
#: („bun pentru gaming" fără o fațetă de utilizare). Soft, cu scope pe subiect, cel mult
#: `MAX_UNMAPPED_PER_TOPIC` pe subiect (reducerul îl înlocuiește pe cel mai vechi).
UNMAPPED_KEY = "unmapped"
MAX_UNMAPPED_PER_TOPIC = 3

UNIVERSAL_SPECS: tuple[NeedSpec, ...] = (
    # NX-331 (contractul, runda 3): bugetul e al CONVERSAȚIEI. «Cremă de față sub 100 lei» →
    # «arată-mi ce ai la corp» păstrează plafonul; un buget nou explicit îl înlocuiește (supersede).
    NeedSpec("budget_max", NeedKind.NUMERIC_MAX, HARD, scoped=False),
    NeedSpec("budget_min", NeedKind.NUMERIC_MIN, HARD, scoped=False),
    NeedSpec("brand", NeedKind.SCALAR, SOFT, scoped=True),
    NeedSpec("suitable_for", NeedKind.SCALAR, SOFT, scoped=True),
    NeedSpec("concerns", NeedKind.LIST, SOFT, scoped=True),
    NeedSpec("restriction", NeedKind.EXCLUSION, HARD, scoped=False),
    NeedSpec("size", NeedKind.SCALAR, HARD, scoped=False),
    NeedSpec("recipient", NeedKind.SCALAR, SOFT, scoped=False),
    NeedSpec("use_case", NeedKind.SCALAR, SOFT, scoped=False),
    NeedSpec("style_pref", NeedKind.SCALAR, SOFT, scoped=False),
    NeedSpec("preferred_time", NeedKind.SCALAR, SOFT, scoped=False),
    NeedSpec(UNMAPPED_KEY, NeedKind.LIST, SOFT, scoped=True),
)

# Sinonime de CHEIE emise de triaj/clarify/model. Aliniate la `_CLARIFY_ALIASES` din
# `worker/canonicalize.py` — aceeași intrare trebuie să aterizeze pe același slot indiferent de
# stagiul care o propune, altfel „buget" și „budget_max" ar fi două memorii diferite.
KEY_ALIASES: Mapping[str, str] = {
    "budget": "budget_max",
    "budget_band": "budget_max",
    "price_max": "budget_max",
    "pret_maxim": "budget_max",
    "price_min": "budget_min",
    "preferred_brand": "brand",
    "fav_brands": "brand",
    "brand_preference": "brand",
    "concern": "concerns",
    "needs": "concerns",
    "skin_concerns": "concerns",
    "for": "suitable_for",
    "suitable": "suitable_for",
    "avoid": "restriction",
    "restrictions": "restriction",
    "dietary_restriction": "restriction",
    "allergen_free": "restriction",
    "occasion": "use_case",
    "purpose": "use_case",
    "gift_recipient": "recipient",
    "buying_for": "recipient",
    "style_preference": "style_pref",
    "time_preference": "preferred_time",
}

# Cheile care NU sunt nevoi: subiectul trăiește în `topic`, nu în `needs` (un singur proprietar
# per câmp, P3). Propunerile pe ele se resping explicit, ca să nu apară două surse de adevăr.
TOPIC_KEYS: frozenset[str] = frozenset({"category", "category_key", "product_type", "intent"})

#: NX-334 — dimensiunea universală a prețului (`interpretation.UNIVERSAL_DIMENSIONS`). Limitele ei
#: sunt cheile bugetului; o fațetă numerică `price` din pachet NU primește chei proprii, altfel
#: `price_max` ar fi umbrit aliasul `price_max → budget_max` al scriitorilor vechi.
PRICE_DIMENSION = "price"
#: Limitele prețului, (jos, sus): forma pe care o primesc toate dimensiunile numerice.
PRICE_BOUNDS: tuple[str, str] = ("budget_min", "budget_max")
#: Sufixele cheilor unei fațete numerice. Se folosesc DOAR la construcția vocabularului; cine vrea
#: dimensiunea unei chei o citește din tabel (`NeedVocabulary.dimension_of`), nu din sufix.
_LOW_SUFFIX = "_min"
_HIGH_SUFFIX = "_max"


def norm_text(text: object) -> str:
    """Lower + fără diacritice + spații colapsate. Aceeași normalizare pe ambele capete ale
    oricărei comparații (identică cu `reference_resolver.normalize_for_match`), ca „Ten Mixt" și
    „ten mixt" să fie același fapt, nu două."""
    if not isinstance(text, str):
        return ""
    return " ".join(fold_text(text.strip()).split())


def norm_key(key: object) -> str:
    """Cheie normalizată: lower, cratime/spații → underscore. `Budget Max` → `budget_max`."""
    return "_".join(norm_text(key).replace("-", " ").replace("_", " ").split())


@dataclass(frozen=True)
class NeedVocabulary:
    """Vocabularul de nevoi al UNUI business: nucleu universal + ce declară DomainPack-ul.

    Construit o dată per tur (`from_pack`) și pasat reducerului. Fără pack → doar nucleul, adică
    exact comportamentul agnostic; un tenant fără config nu rămâne fără memorie.

    NX-334: o dimensiune NUMERICĂ are două chei, una pentru limita de jos și una pentru cea de
    sus, ca bugetul (`budget_min` / `budget_max`). O singură cheie ar ține o singură direcție:
    „minim 256" ar fi ajuns plafon, iar a doua limită a unui interval ar fi înlocuit-o pe prima.
    `bounds` ține perechea per dimensiune (`None` pe o direcție pe care fațeta n-o declară)."""

    specs: Mapping[str, NeedSpec] = field(default_factory=dict)
    concern_map: Mapping[str, str] = field(default_factory=dict)
    bounds: Mapping[str, tuple[str | None, str | None]] = field(default_factory=dict)

    @classmethod
    def from_pack(cls, pack: DomainPack | None) -> NeedVocabulary:
        specs: dict[str, NeedSpec] = {s.key: s for s in UNIVERSAL_SPECS}
        bounds: dict[str, tuple[str | None, str | None]] = {}
        if pack is not None:
            for facet in getattr(pack, "facets", ()) or ():
                bound_specs = _bound_specs_from_facet(facet)
                if bound_specs is not None:
                    low, high = bound_specs
                    for spec in (low, high):
                        if spec is not None and spec.key not in TOPIC_KEYS:
                            specs[spec.key] = spec
                    bounds[norm_key(facet.key)] = (
                        low.key if low is not None else None,
                        high.key if high is not None else None,
                    )
                    continue
                spec = _spec_from_facet(facet)
                if spec is not None and spec.key not in TOPIC_KEYS:
                    # Fațeta tipizată bate nucleul: businessul a declarat explicit tipul/valorile.
                    specs[spec.key] = spec
            for raw_key in getattr(pack, "searchable_facets", ()) or ():
                key = norm_key(raw_key)
                if key and key not in specs and key not in TOPIC_KEYS:
                    specs[key] = NeedSpec(key, NeedKind.LIST, SOFT, scoped=True)
        return cls(
            specs=specs,
            concern_map={
                norm_text(k): v for k, v in (getattr(pack, "concern_map", None) or {}).items()
            },
            bounds=bounds,
        )

    def bounds_for(self, dimension: object) -> tuple[str | None, str | None] | None:
        """Dimensiunea → cheile limitelor ei, (jos, sus). `None` = dimensiunea nu e numerică.
        Prețul are mereu limitele bugetului, și cu un vocabular gol (nucleul le declară)."""
        key = norm_key(dimension)
        if key == PRICE_DIMENSION:
            return PRICE_BOUNDS
        return self.bounds.get(key)

    def dimension_of(self, key: object) -> str:
        """Cheia unei nevoi → dimensiunea ei (`budget_max` → `price`, `storage_min` →
        `storage`). O cheie care nu e o limită e propria ei dimensiune."""
        normalized = norm_key(key)
        if normalized in PRICE_BOUNDS:
            return PRICE_DIMENSION
        for dimension, pair in self.bounds.items():
            if normalized in pair:
                return dimension
        return normalized

    def opposite_bound(self, key: object) -> str | None:
        """Cheia limitei opuse pe aceeași dimensiune (`storage_min` → `storage_max`), sau
        `None` dacă cheia nu e o limită sau fațeta n-o declară pe cealaltă."""
        normalized = norm_key(key)
        pair = self.bounds_for(self.dimension_of(normalized))
        if pair is None or normalized not in pair:
            return None
        low, high = pair
        return high if normalized == low else low

    def spec_for(self, key: object) -> NeedSpec | None:
        """Cheia → contractul ei, prin alias dacă e nevoie. `None` = cheie necunoscută (reducerul
        respinge propunerea; memoria nu primește chei inventate de model)."""
        normalized = norm_key(key)
        if not normalized:
            return None
        if normalized in self.specs:
            return self.specs[normalized]
        aliased = KEY_ALIASES.get(normalized)
        return self.specs.get(aliased) if aliased else None

    def is_topic_key(self, key: object) -> bool:
        return norm_key(key) in TOPIC_KEYS


def _facet_value_type(facet: Any) -> str:
    return getattr(getattr(facet, "value_type", None), "value", None) or str(
        getattr(facet, "value_type", "")
    )


def _bound_specs_from_facet(facet: Any) -> tuple[NeedSpec | None, NeedSpec | None] | None:
    """NX-334 — o fațetă NUMERICĂ → cheile limitelor ei (`<fațetă>_min`, `<fațetă>_max`).

    `None` = fațeta nu e numerică, sau e fațeta `price` (prețul are cheile bugetului). O direcție
    există dacă fațeta o declară în `operators` (`gte` jos, `lte` sus; `eq` le cere pe amândouă);
    fără `operators` declarați, amândouă (pachetele de azi nu rămân fără memorie).

    Tăria implicită urmează I8: `hard` doar pe o fațetă `enforce_ready`. Pe `main` orice fațetă
    numerică era `hard` implicit; pe calea interpretată tăria vine din `CheckedChange`, deci
    implicitul nu decidea, dar nu trebuie să poată decide greșit."""
    key = norm_key(getattr(facet, "key", ""))
    if not key or key == PRICE_DIMENSION or key in TOPIC_KEYS:
        return None
    if _facet_value_type(facet) not in ("number", "numeric", "int", "float"):
        return None
    operators = set(getattr(facet, "operators", ()) or ())
    declared = bool(operators)
    strength = HARD if getattr(facet, "enforce_ready", False) else SOFT
    scoped = getattr(facet, "scope", "topic") != "conversation"
    low = (
        NeedSpec(key + _LOW_SUFFIX, NeedKind.NUMERIC_MIN, strength, scoped=scoped)
        if not declared or operators & {"gte", "eq"}
        else None
    )
    high = (
        NeedSpec(key + _HIGH_SUFFIX, NeedKind.NUMERIC_MAX, strength, scoped=scoped)
        if not declared or operators & {"lte", "eq"}
        else None
    )
    if low is None and high is None:
        return None
    return low, high


def _spec_from_facet(facet: Any) -> NeedSpec | None:
    """`TypedFacet` (NX-186) → `NeedSpec`. Tipul fațetei dă felul nevoii; prezența lui
    `not_contains` printre operatori o face excludere (deci `hard`).

    O fațetă numerică ajunge aici doar când e fațeta `price` (restul primesc două chei, vezi
    `_bound_specs_from_facet`); ramura numerică de mai jos îi păstrează forma de dinainte."""
    key = norm_key(getattr(facet, "key", ""))
    if not key:
        return None
    operators = tuple(getattr(facet, "operators", ()) or ())
    value_type = _facet_value_type(facet)
    if "not_contains" in operators:
        kind, strength = NeedKind.EXCLUSION, HARD
    elif value_type in ("bool", "boolean"):
        kind, strength = NeedKind.BOOLEAN, SOFT
    elif value_type in ("number", "numeric", "int", "float"):
        kind, strength = NeedKind.NUMERIC_MAX, HARD
    elif value_type in ("list", "array"):
        kind, strength = NeedKind.LIST, SOFT
    else:
        kind, strength = NeedKind.SCALAR, SOFT
    values = frozenset(norm_text(v) for v in (getattr(facet, "values", ()) or ()) if norm_text(v))
    aliases = {
        norm_text(k): norm_text(v)
        for k, v in (getattr(facet, "aliases", None) or {}).items()
        if norm_text(k) and norm_text(v)
    }
    # NX-331: scope-ul e DATA fațetei (`TypedFacet.scope`), nu o constantă de cod.
    scoped = getattr(facet, "scope", "topic") != "conversation"
    return NeedSpec(key, kind, strength, scoped=scoped, values=values, aliases=aliases)


@dataclass(frozen=True)
class NormalizedNeed:
    """Rezultatul normalizării. `value is None` ⇒ nu avem un fapt: nevoia se înregistrează cu
    `status=unknown`, NU se aruncă (ca modelul să vadă că s-a discutat cheia și să nu o
    re-întrebe la infinit) și NU se transformă în negație."""

    key: str
    operator: str
    value: str | float | bool | None
    kind: NeedKind
    default_strength: str
    scoped: bool
    reason: str = "ok"  # ok | free_text | out_of_vocabulary | out_of_range | empty


def _numeric(value: object) -> float | None:
    """Număr dintr-o valoare de propunere. Acceptă `int/float` direct și extrage PRIMUL număr
    dintr-un string („200 lei" → 200). În afara domeniului plauzibil → None (vezi
    `MAX_NUMERIC_VALUE`): un număr de 9 cifre nu e un buget."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        match = _NUMBER_RE.search(norm_text(value))
        if match is None:
            return None
        number = float(match.group(0).replace(",", "."))
    if number <= 0 or number > MAX_NUMERIC_VALUE:
        return None
    return number


def _canonical_token(
    value: object, spec: NeedSpec, vocab: NeedVocabulary
) -> tuple[str | None, str]:
    """Valoare textuală → token canonic, sau `(None, motiv)`.

    Trei porți, în ordine: aliasurile fațetei / `concern_map`-ul businessului → vocabularul ÎNCHIS
    (dacă a fost declarat) → forma de token (fără propoziții). A treia e cea care ține text liber
    afară din memorie: „ceva pentru sora mea care are tenul mixt" nu e o valoare canonică."""
    text = norm_text(value)
    if not text:
        return None, "empty"
    text = spec.aliases.get(text, text)
    if spec.key == "concerns":
        text = vocab.concern_map.get(text, text)
    if spec.values:
        return (text, "ok") if text in spec.values else (None, "out_of_vocabulary")
    if len(text) > MAX_VALUE_CHARS or len(text.split()) > MAX_VALUE_WORDS:
        return None, "free_text"
    if not _TOKEN_RE.match(text):
        return None, "free_text"
    return text, "ok"


def positive_facet_keys(vocab: NeedVocabulary) -> frozenset[str]:
    """Cheile care, în stiva v1, înseamnă o cerință POZITIVĂ pe o valoare închisă (ex. tipul de
    ten). PUR (NX-355).

    Stiva v1 pierde operatorul: o EXCLUDERE arată la fel ca o cerere. Doar fațetele scalare cu
    vocabular ÎNCHIS sunt sigur „vreau valoarea asta”; restul (excluderi, limite numerice,
    booleeni, valori deschise) nu se cară în căutare și nu se afișează ca cerere."""
    return frozenset(
        spec.key for spec in vocab.specs.values() if spec.kind is NeedKind.SCALAR and spec.values
    )


def rehome_list_value(key: object, value: object, vocab: NeedVocabulary) -> tuple[str, str] | None:
    """O valoare de listă care aparține ALTEI fațete → `(cheia ei, valoarea canonică)`. PUR.

    NX-355: pe calea v1 modelul pune tot ce descrie clientul în argumentul `concerns` (unealta n-are
    un câmp per fațetă), iar căutarea îl rezolvă corect peste TOATE dimensiunile: «ten uscat» ajunge
    `skin_type=dry`. Memoria însă îl normaliza doar în interiorul lui `concerns`: `concern_map`-ul
    tenantului îl traduce în `dry`, `dry` nu e o valoare de `concerns`, deci nevoia ieșea
    `out_of_vocabulary` și dispărea la turul următor (conversația `1748f988`, 2026-09-29). Pe
    `sole-ro`, 24 din cele 87 de fraze ale hărții țintesc `skin_type`.

    Se mută doar ce are UN singur proprietar: valoarea (după harta tenantului) nu e a lui `concerns`
    și e în vocabularul ÎNCHIS al exact unei alte fațete. Zero sau mai mulți proprietari ⇒ `None`,
    iar apelantul păstrează comportamentul de dinainte (nicio ghicire)."""
    list_spec = vocab.spec_for(key)
    if list_spec is None or list_spec.kind is not NeedKind.LIST:
        return None
    text = norm_text(value)
    if not text:
        return None
    text = list_spec.aliases.get(text, text)
    mapped = vocab.concern_map.get(text, text) if list_spec.key == "concerns" else text
    if list_spec.values and mapped in list_spec.values:
        return None
    owners = [
        spec.key
        for spec in vocab.specs.values()
        if spec.key != list_spec.key
        and spec.kind is NeedKind.SCALAR
        and spec.values
        and mapped in spec.values
    ]
    return (owners[0], mapped) if len(owners) == 1 else None


def normalize_need(key: object, value: object, vocab: NeedVocabulary) -> NormalizedNeed | None:
    """Cheie + valoare brută → nevoie canonică, sau `None` dacă cheia nu e în vocabular.

    PUR și determinist. Cheia necunoscută întoarce `None` (reducerul respinge: memoria nu primește
    slot-uri inventate); valoarea nenormalizabilă întoarce o nevoie cu `value=None` — semnalul de
    `unknown`, care e diferit de o negație."""
    spec = vocab.spec_for(key)
    if spec is None:
        return None

    if spec.kind in (NeedKind.NUMERIC_MAX, NeedKind.NUMERIC_MIN):
        number = _numeric(value)
        return NormalizedNeed(
            spec.key,
            spec.operator,
            number,
            spec.kind,
            spec.default_strength,
            spec.scoped,
            "ok" if number is not None else "out_of_range",
        )

    if spec.kind is NeedKind.BOOLEAN:
        flag = value if isinstance(value, bool) else _bool_token(value)
        return NormalizedNeed(
            spec.key,
            spec.operator,
            flag,
            spec.kind,
            spec.default_strength,
            spec.scoped,
            "ok" if flag is not None else "out_of_vocabulary",
        )

    token, reason = _canonical_token(value, spec, vocab)
    return NormalizedNeed(
        spec.key, spec.operator, token, spec.kind, spec.default_strength, spec.scoped, reason
    )


_TRUE_TOKENS = frozenset({"true", "da", "yes", "igen", "1"})
_FALSE_TOKENS = frozenset({"false", "nu", "no", "nem", "0"})


def _bool_token(value: object) -> bool | None:
    text = norm_text(value)
    if text in _TRUE_TOKENS:
        return True
    if text in _FALSE_TOKENS:
        return False
    return None


#: Sub atâtea caractere un token trebuie să se potrivească EXACT; peste, acceptăm potrivirea pe
#: prefix. Româna flexionează substantivele („ten" → „tenul"), deci o egalitate strictă ar rata
#: exact cazul normal; un prefix de două litere ar potrivi orice.
_MIN_PREFIX_CHARS = 3


def _message_numbers(message: str) -> set[float]:
    """Numerele dintr-un mesaj, în ambele lecturi ale separatorului. „1.500" e o mie cinci sute în
    scriere românească și unu virgulă cinci în scriere zecimală — nu putem ști care, deci le
    acceptăm pe amândouă: coroborarea confirmă că numărul a fost ROSTIT, nu care e valoarea lui."""
    out: set[float] = set()
    for match in _NUMBER_RE.finditer(message):
        raw = match.group(0)
        for candidate in (raw.replace(",", "."), raw.replace(",", "").replace(".", "")):
            try:
                out.add(float(candidate))
            except ValueError:
                continue
    return out


def _prefix_match(token: str, word: str) -> bool:
    """Un token canonic și un cuvânt din mesaj sunt aceeași noțiune? Prefixul e permis în ambele
    sensuri (clientul poate scrie forma articulată sau cea scurtă), dar NUMAI peste pragul de
    lungime — altfel un „g" din mesaj ar corobora „gras"."""
    if word.startswith(token):
        return True
    return len(word) >= _MIN_PREFIX_CHARS and token.startswith(word)


def corroborated_by(message: object, value: object) -> bool:
    """Mesajul BRUT al turului susține valoarea asta? PURĂ, deterministă, agnostică de limbă.

    Asta e poarta prin care o valoare propusă de model devine afirmație a CLIENTULUI. Modelul
    transcrie, codul confirmă: dacă „200" sau „ten gras" chiar apar în ce a scris clientul acum,
    faptul e al lui și poate deveni `hard` (D7); dacă nu apar, rămâne o inferență și rămâne `soft`.
    Fără poarta asta, singura alternativă la „modelul își declară singur sursa" ar fi să nu mai
    existe deloc constrângeri hard în momentul în care extracția de sloturi nu mai vine din triaj.

    Conservatoare prin construcție: orice dubiu întoarce `False`, iar consecința unui `False` e o
    nevoie mai slabă, niciodată una mai tare."""
    text = norm_text(message)
    if not text or value is None or isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return any(abs(number - float(value)) <= 0.005 for number in _message_numbers(text))

    wanted = norm_text(str(value).replace("_", " ").replace("-", " "))
    if not wanted:
        return False
    words = text.replace("-", " ").split()
    for token in wanted.split():
        if len(token) < _MIN_PREFIX_CHARS:
            if token not in words:
                return False
        elif not any(_prefix_match(token, word) for word in words):
            return False
    return True


def value_fingerprint(value: object) -> str:
    """Amprenta STABILĂ a unei valori revocate — ce se păstrează în tombstone.

    Nu e valoarea: e destul cât să recunoaștem că „acesta e faptul pe care l-a retras", fără să
    reținem ce anume era. Deterministă (fără sare aleatoare) ca replay-ul să dea același rezultat,
    și scurtă ca tombstone-ul să nu crească starea."""
    import hashlib

    if isinstance(value, bool):
        payload = "b:" + ("1" if value else "0")
    elif isinstance(value, (int, float)):
        payload = f"n:{float(value):.6g}"
    else:
        payload = "s:" + norm_text(value)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


__all__ = [
    "HARD",
    "KEY_ALIASES",
    "MAX_NUMERIC_VALUE",
    "MAX_UNMAPPED_PER_TOPIC",
    "MAX_VALUE_CHARS",
    "MAX_VALUE_WORDS",
    "OPERATOR_BY_KIND",
    "PRICE_BOUNDS",
    "PRICE_DIMENSION",
    "SOFT",
    "TOPIC_KEYS",
    "UNIVERSAL_SPECS",
    "UNMAPPED_KEY",
    "NeedKind",
    "NeedSpec",
    "NeedVocabulary",
    "NormalizedNeed",
    "corroborated_by",
    "norm_key",
    "norm_text",
    "normalize_need",
    "value_fingerprint",
]
