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
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

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
UNIVERSAL_SPECS: tuple[NeedSpec, ...] = (
    NeedSpec("budget_max", NeedKind.NUMERIC_MAX, HARD, scoped=True),
    NeedSpec("budget_min", NeedKind.NUMERIC_MIN, HARD, scoped=True),
    NeedSpec("brand", NeedKind.SCALAR, SOFT, scoped=True),
    NeedSpec("suitable_for", NeedKind.SCALAR, SOFT, scoped=True),
    NeedSpec("concerns", NeedKind.LIST, SOFT, scoped=True),
    NeedSpec("restriction", NeedKind.EXCLUSION, HARD, scoped=False),
    NeedSpec("size", NeedKind.SCALAR, HARD, scoped=False),
    NeedSpec("recipient", NeedKind.SCALAR, SOFT, scoped=False),
    NeedSpec("use_case", NeedKind.SCALAR, SOFT, scoped=False),
    NeedSpec("style_pref", NeedKind.SCALAR, SOFT, scoped=False),
    NeedSpec("preferred_time", NeedKind.SCALAR, SOFT, scoped=False),
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


def norm_text(text: object) -> str:
    """Lower + fără diacritice + spații colapsate. Aceeași normalizare pe ambele capete ale
    oricărei comparații (identică cu `reference_resolver.normalize_for_match`), ca „Ten Mixt" și
    „ten mixt" să fie același fapt, nu două."""
    if not isinstance(text, str):
        return ""
    decomposed = unicodedata.normalize("NFKD", text.strip().lower())
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(stripped.split())


def norm_key(key: object) -> str:
    """Cheie normalizată: lower, cratime/spații → underscore. `Budget Max` → `budget_max`."""
    return "_".join(norm_text(key).replace("-", " ").replace("_", " ").split())


@dataclass(frozen=True)
class NeedVocabulary:
    """Vocabularul de nevoi al UNUI business: nucleu universal + ce declară DomainPack-ul.

    Construit o dată per tur (`from_pack`) și pasat reducerului. Fără pack → doar nucleul, adică
    exact comportamentul agnostic; un tenant fără config nu rămâne fără memorie."""

    specs: Mapping[str, NeedSpec] = field(default_factory=dict)
    concern_map: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def from_pack(cls, pack: DomainPack | None) -> NeedVocabulary:
        specs: dict[str, NeedSpec] = {s.key: s for s in UNIVERSAL_SPECS}
        if pack is not None:
            for facet in getattr(pack, "facets", ()) or ():
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
        )

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


def _spec_from_facet(facet: Any) -> NeedSpec | None:
    """`TypedFacet` (NX-186) → `NeedSpec`. Tipul fațetei dă felul nevoii; prezența lui
    `not_contains` printre operatori o face excludere (deci `hard`)."""
    key = norm_key(getattr(facet, "key", ""))
    if not key:
        return None
    operators = tuple(getattr(facet, "operators", ()) or ())
    value_type = getattr(getattr(facet, "value_type", None), "value", None) or str(
        getattr(facet, "value_type", "")
    )
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
    return NeedSpec(key, kind, strength, scoped=True, values=values, aliases=aliases)


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
    "MAX_VALUE_CHARS",
    "MAX_VALUE_WORDS",
    "OPERATOR_BY_KIND",
    "SOFT",
    "TOPIC_KEYS",
    "UNIVERSAL_SPECS",
    "NeedKind",
    "NeedSpec",
    "NeedVocabulary",
    "NormalizedNeed",
    "norm_key",
    "norm_text",
    "normalize_need",
    "value_fingerprint",
]
