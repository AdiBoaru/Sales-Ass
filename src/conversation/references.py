"""Kernel `kernel.v1.0`, pasul 2 (NX-329) — resolverul de referințe v2. PUR.

Contractul (§„Ownership", rândul „Product and variant ids": modelul *never*, codul *owns*): modelul
spune CE FEL de referință a văzut („al doilea", „ăsta", „Samsung-ul", „varianta neagră", „cel mai
ieftin", „celălalt", „cel de mai devreme"); codul decide spre ce produs arată. Aici se decide.

Trei faze, iar I/O-ul stă în afara modulului:

    plan_lookup(refs, sources)                  → ce id-uri și ce nume trebuie citite din catalog
    (apelantul citește catalogul: `src/catalog/reference_facts.py`, UN checkout)
    resolve_references(refs, sources, facts)    → un `ResolvedRef` per referință
    gate_act_targets(acts, resolved)            → I24 (ținta numește produse), I10 (mutația: exact)

Faptele intră ca argument, deci proprietatea I1 e verificabilă direct: niciun id din ieșire nu
lipsește din `facts.products`, adică din ce a întors catalogul în acest tur, pe `business_id = $1`.

Resolverul citește DOAR câmpurile structurate ale unei `Reference` (`kind`, `ordinal`, `name`,
`dimension`, `value`, `direction`). `Reference.text` e textul brut al clientului: intră în trace, nu
în decizie (poarta „no raw-user-text branching" din `tests/test_kernel_contract.py`).

Ce NU face: nu cheamă modelul (I13), nu scrie stare (I3; `selected_product` ca efect al țintei e al
reducerului, pasul 3), nu caută aproximativ în catalog (o căutare după nume e a plannerului)."""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from src.catalog.query_terms import (
    any_locale_stopwords,
    inflection_suffixes,
    stopwords,
    tokens,
)
from src.catalog.vocabulary import (
    CatalogVocabulary,
    Resolution,
    ResolutionStatus,
    facet_overlays,
    resolve,
    resolve_any,
)
from src.conversation.interpretation import Act, Reference, ResolvedRef
from src.domain.pack import DEFAULT_REFERENCE_DIMENSIONS

if TYPE_CHECKING:
    from src.conversation.state_v2 import ConversationStateV2

Outcome = Literal["exact", "ambiguous", "not_found", "stale"]
Source = Literal["action", "shown_now", "shown_earlier", "parked", "page", "catalog"]
Thread = Literal["continue", "aside", "resume"]

#: Vocabularul ÎNCHIS al lui `ResolvedRef.reason`. Un test cere ca fiecare motiv emis să fie aici:
#: un motiv nou fără loc în listă ar fi o valoare de telemetrie pe care nimeni n-o așteaptă.
REASONS: tuple[str, ...] = (
    # exact
    "action_anchor",
    "ordinal_in_set",
    "page_deictic",
    "focus",
    "single_in_set",
    "named",
    "named_with_qualifier",
    "attribute_match",
    "ordinal_in_zoomed_list",
    "extreme",
    "the_other",
    "earlier_single",
    "unavailable",
    "price_changed",
    # ambiguous
    "ordinal_out_of_range",
    "no_anchor",
    "name_shared",
    "catalog_tie",
    "attribute_shared",
    "extreme_unknown_value",
    "extreme_tie",
    "not_a_pair",
    "no_focus",
    "earlier_unspecified",
    # not_found
    "no_set",
    "invalid_reference",
    "name_not_found",
    "attribute_not_in_sets",
    "denotes_property",
    "not_orderable",
    "no_earlier",
    "vocabulary_unavailable",
    # stale
    "anchor_invalid",
    "anchor_stale",
    "not_in_catalog",
)

#: Dimensiunile ordonabile care nu stau în `attributes`: coloane ale produsului, pe orice vertical.
PRICE_DIMENSION = "price"
RATING_DIMENSION = "rating"
BRAND_DIMENSION = "brand"
#: Etichetele de variantă: mereu de referință, fiindcă sunt ale produsului prin construcție.
VARIANT_DIMENSION = "variant"
_COLUMN_DIMENSIONS = frozenset({PRICE_DIMENSION, RATING_DIMENSION})

#: Un cuvânt mai scurt de atât nu numește un produs („la", „cu", „92"), ca la NX-326.
MIN_NAME_TOKEN = 3
#: Plafonul id-urilor revalidate: 6 pe ecran, 2 × 6 arătate mai devreme, 6 parcate, pagina,
#: acțiunea, focusul. Peste el nu există o sursă legitimă în contract.
MAX_LOOKUP_IDS = 30
#: Runtime cap-ul contractului pe referințe per tur (§„Review decisions", punctul 3).
MAX_NAMES = 6
#: Două prețuri care diferă sub un ban sunt același preț (rotunjire float).
_PRICE_EPSILON = 0.005


# --- intrările -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class ShownItem:
    """Un produs arătat, ca ref (P8): exact ce ține starea azi."""

    product_id: str
    name: str = ""
    price: float | None = None


@dataclass(frozen=True)
class ActionRef:
    """Referința purtată de tokenul acțiunii apăsate (NX-236). `valid` e verdictul criptografic
    al lui NX-236; `revision` e revizia listei peste care a fost emisă."""

    product_id: str
    revision: int = 0
    valid: bool = True


@dataclass(frozen=True)
class ReferenceSources:
    """Unde poate trăi un produs la care se referă clientul. `shown_earlier` și `parked` vin din
    starea v2 (`references.recent_sets`, `parked.shown`, NX-331); pe calea v1 sunt goale."""

    shown_now: tuple[ShownItem, ...] = ()
    shown_earlier: tuple[tuple[ShownItem, ...], ...] = ()  # cel mai recent primul
    parked: tuple[ShownItem, ...] = ()
    page: ShownItem | None = None
    action: ActionRef | None = None
    focus: str | None = None  # `selected_product` (v2); None pe v1
    displayed_revision: int = 0
    thread: Thread = "continue"
    #: `kernel.v6.0` (NX-364): ordinalul pe lista „în care s-a intrat” (`_zoomed_list`). Îl pune
    #: DOAR `sources_from_state`, adică calea kernelului; scurtăturile căii v1 își construiesc
    #: sursele separat și rămân pe ecran (I16, recenzia v6.0).
    zoom_ordinals: bool = False

    @property
    def focus_source(self) -> Source:
        """Setul în focus: cel parcat când clientul revine la el (`resume`), altfel ecranul.
        Contractul a scos `within` (runda 3, punctul 15): unde trăiește obiectul e al codului."""
        return "parked" if self.thread == "resume" else "shown_now"

    @property
    def focus_set(self) -> tuple[ShownItem, ...]:
        return self.parked if self.thread == "resume" else self.shown_now

    def sets_in_order(self) -> list[tuple[Source, tuple[ShownItem, ...]]]:
        """Seturile, în ordinea în care se caută un nume sau un atribut: focusul, apoi ce s-a
        arătat mai devreme (recent → vechi), apoi celălalt dintre ecran și parcat."""
        out: list[tuple[Source, tuple[ShownItem, ...]]] = [(self.focus_source, self.focus_set)]
        out += [("shown_earlier", s) for s in self.shown_earlier]
        other: tuple[Source, tuple[ShownItem, ...]] = (
            ("shown_now", self.shown_now) if self.thread == "resume" else ("parked", self.parked)
        )
        out.append(other)
        return [(src, items) for src, items in out if items]

    def all_ids(self) -> list[str]:
        ids: list[str] = []
        if self.action is not None:
            ids.append(self.action.product_id)
        if self.page is not None:
            ids.append(self.page.product_id)
        if self.focus:
            ids.append(self.focus)
        # Seturile în ordinea căutării (focusul întâi): peste `MAX_LOOKUP_IDS` se taie ce e mai
        # departe de focus. Pe `resume` focusul e setul PARCAT (NX-331), deci nu el cade primul.
        for _, items in self.sets_in_order():
            ids += [it.product_id for it in items]
        return list(dict.fromkeys(i for i in ids if i))

    def shown_prices(self) -> dict[str, float]:
        """Prețul pe care l-a VĂZUT clientul, primul întâlnit (ecranul înaintea istoricului)."""
        out: dict[str, float] = {}
        items = [*self.shown_now, *(i for s in self.shown_earlier for i in s), *self.parked]
        if self.page is not None:
            items.append(self.page)
        for it in items:
            if it.price is not None and it.product_id not in out:
                out[it.product_id] = float(it.price)
        return out


@dataclass(frozen=True)
class ProductFacts:
    """Un produs RECITIT din catalog în acest tur. `available=None` = necunoscut, nu fals."""

    product_id: str
    name: str = ""
    price: float | None = None
    available: bool | None = None
    rating: float | None = None
    brand: str | None = None
    attributes: Mapping[str, object] = field(default_factory=dict)
    variant_labels: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReferenceFacts:
    """Ce a întors catalogul în tur. `named`: cheia unui nume cerut (`name_key`) → perechi
    `(id, lungimea numelui distinctiv)`, cel mai lung primul."""

    products: Mapping[str, ProductFacts] = field(default_factory=dict)
    named: Mapping[str, tuple[tuple[str, int], ...]] = field(default_factory=dict)
    snapshot: str = ""
    #: NX-332: cheile din `attributes` pe care le-a CERUT citirea (`CatalogLookup.attributes`).
    #: Un atribut necerut lipsește din fapte fără să fie necunoscut pe produs: cine numără pe el
    #: (poarta de ambiguitate) trebuie să știe diferența. `None` = toate (fixture fără lookup).
    attributes_read: tuple[str, ...] | None = None


@dataclass(frozen=True)
class CatalogLookup:
    """Ce trebuie citit din catalog: id-urile de revalidat, numele de căutat (așa cum le-a dat
    referința; faptele le indexează pe `name_key`), atributele de adus."""

    ids: tuple[str, ...] = ()
    names: tuple[str, ...] = ()
    attributes: tuple[str, ...] = ()


# --- utilitare de potrivire ----------------------------------------------------------------------


def name_key(name: str | None) -> str:
    """Cheia unui nume cerut: tokenii lui, uniți. Aceeași pe ambele capete ale căutării."""
    return " ".join(tokens(name or ""))


def _stop(locale: str | None) -> frozenset[str]:
    return stopwords(locale) if locale else any_locale_stopwords()


def _content(words: Sequence[str], stop: Collection[str]) -> tuple[str, ...]:
    """Cuvintele care pot numi un produs. O CIFRĂ rămâne oricât de scurtă ar fi: „Phone 9" și
    „Phone 3" diferă doar prin ea, iar pragul de lungime ar face din ele același produs."""
    return tuple(w for w in words if (len(w) >= MIN_NAME_TOKEN or w.isdigit()) and w not in stop)


def _contains_seq(hay: Sequence[str], needle: Sequence[str]) -> bool:
    n = len(needle)
    return bool(n) and any(tuple(hay[i : i + n]) == tuple(needle) for i in range(len(hay) - n + 1))


def _unique(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


@dataclass(frozen=True)
class _Hit:
    outcome: Outcome
    ids: tuple[str, ...]


def _same_word(asked: str, carried: str, suffixes: Collection[str]) -> bool:
    """Același cuvânt, eventual flexionat: egal, sau unul e celălalt plus un sufix de flexiune al
    locale-i («serul» = „ser" + „ul"). Tulpina are cel puțin `MIN_NAME_TOKEN` litere, altfel un
    sufix scurt ar lega cuvinte fără legătură."""
    if asked == carried:
        return True
    short, long_ = (asked, carried) if len(asked) < len(carried) else (carried, asked)
    return (
        len(short) >= MIN_NAME_TOKEN and long_[len(short) :] in suffixes and long_.startswith(short)
    )


def _carries(name_words: Collection[str], word: str, suffixes: Collection[str]) -> bool:
    return word in name_words or (
        bool(suffixes) and any(_same_word(word, w, suffixes) for w in name_words)
    )


def match_name_in_set(
    name: str,
    items: Sequence[ShownItem],
    stop: Collection[str],
    suffixes: Collection[str] = (),
) -> _Hit | None:
    """Produsul (sau produsele) din `items` pe care `name` îl numește. None = setul ratează.

    Patru trepte, de la cea mai precisă, moștenite de la NX-326/NX-318: (1) numele ÎNTREG al unui
    produs apare în fraza cerută; (2) fraza cerută apare întreagă în numele unui produs; (3)
    produsele care conțin TOATE cuvintele de conținut; (4) cuvintele purtate de UN singur produs din
    set. O treaptă cu ≥ 2 produse e ambiguitate pe EXACT acele produse: a coborî la treapta
    următoare ar lărgi setul (sonda NX-329: «GESKE Sonic Facial Roller 4 in 1» cu trei carduri
    identice și două «… Facial and Body Roller 4 in 1» ieșea ambiguu pe toate cinci). Fără scor pe
    tokeni: un cuvânt comun („cremă") ar alege un produs pe care clientul nu l-a numit. Cuvintele se
    compară modulo flexiune (`suffixes`, tabelul locale-i), frazele strict."""
    words = tuple(tokens(name))
    content = _content(words, stop)
    if not content or not items:
        return None
    named = [(it.product_id, tuple(tokens(it.name))) for it in items]

    # (1) Numele întreg în cerere. Când două nume încap, câștigă cel mai LUNG: „Dokdo Cream" e
    # conținut în «ROUND LAB 1025 Dokdo Cream» exact cât produsul cu numele lung, dar clientul l-a
    # scris pe acela întreg. Nume identice (familii) au aceeași lungime ⇒ ambiguitate între ele.
    whole = [(pid, len(nw)) for pid, nw in named if nw and _contains_seq(words, nw)]
    longest = max((n for _, n in whole), default=0)
    # (2) Cererea întreagă în numele produsului.
    for phrase in (
        _unique(pid for pid, n in whole if n == longest),
        _unique(pid for pid, nw in named if nw and _contains_seq(nw, words)),
    ):
        if len(phrase) == 1:
            return _Hit("exact", phrase)
        if phrase:
            return _Hit("ambiguous", phrase)

    full = _unique(pid for pid, nw in named if all(_carries(nw, w, suffixes) for w in content))
    if len(full) == 1:
        return _Hit("exact", full)
    if len(full) >= 2:
        return _Hit("ambiguous", full)

    owners = {w: [pid for pid, nw in named if _carries(nw, w, suffixes)] for w in content}
    if any(not o for o in owners.values()):
        # Un cuvânt pe care nu-l poartă niciun produs din set („Xiaomi" lângă un „Samsung Phone")
        # spune că numele nu e al setului. Fără regula asta, un cuvânt comun („phone") ar deveni
        # „distinctiv" pe un set mic și ar alege un produs pe care clientul nu l-a numit.
        return None
    distinct = _unique(o[0] for o in owners.values() if len(o) == 1)
    if len(distinct) == 1:
        return _Hit("exact", distinct)
    if len(distinct) >= 2:
        return _Hit("ambiguous", distinct)
    shared = _unique(pid for o in owners.values() if 1 < len(o) < len(named) for pid in o)
    if shared:
        return _Hit("ambiguous", shared)
    return None


def _values_of(facts: ProductFacts, dimension: str) -> list[str]:
    """Valorile afișabile ale unui produs pe o dimensiune de referință.

    Etichetele de variantă intră pe orice dimensiune în afară de marcă: sunt valorile axelor de
    variantă (culoare, mărime), deci «varianta neagră» găsește canapeaua gri care vine și în
    negru."""
    if dimension == VARIANT_DIMENSION:
        return [v for v in facts.variant_labels if v]
    raw: list[object] = []
    if dimension == BRAND_DIMENSION and facts.brand:
        raw.append(facts.brand)
    value = facts.attributes.get(dimension)
    if isinstance(value, (list, tuple)):
        raw += list(value)
    elif value is not None:
        raw.append(value)
    if dimension != BRAND_DIMENSION:
        raw += list(facts.variant_labels)
    return [str(v) for v in raw if v is not None and not isinstance(v, bool)]


def _numeric(facts: ProductFacts, dimension: str) -> float | None:
    if dimension == PRICE_DIMENSION:
        return facts.price
    if dimension == RATING_DIMENSION:
        return facts.rating
    value = facts.attributes.get(dimension)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


# --- rezultatul intermediar ----------------------------------------------------------------------


@dataclass(frozen=True)
class _Raw:
    kind: str
    outcome: Outcome
    ids: tuple[str, ...]
    source: Source
    reason: str


class _Resolver:
    """Starea unei rezolvări: sursele, faptele, vocabularul și ce s-a rezolvat deja în turul ăsta
    (`the_other` citește focusul din referințele anterioare ale aceleiași interpretări)."""

    def __init__(
        self,
        sources: ReferenceSources,
        facts: ReferenceFacts,
        *,
        vocab: CatalogVocabulary | None,
        pack: object | None,
        locale: str | None,
    ) -> None:
        self.sources = sources
        self.facts = facts
        self.vocab = vocab if vocab is not None and not vocab.is_empty() else None
        self.stop = _stop(locale)
        self.suffixes = inflection_suffixes(locale)
        self.reference_dims = _reference_dims(pack)
        self.overlays = facet_overlays(pack, self.vocab.facet_names) if self.vocab else None
        self.exact_before: list[str] = []

    # --- felurile ---------------------------------------------------------------------------------

    def ordinal(self, ref: Reference) -> _Raw:
        src, items = self.sources.focus_source, self.sources.focus_set
        found = "ordinal_in_set"
        zoomed = self._zoomed_list(items)
        if zoomed is not None:
            src, items, found = "shown_earlier", zoomed, "ordinal_in_zoomed_list"
        n = ref.ordinal
        if n is None or n < 1:
            return _Raw("ordinal", "not_found", (), src, "invalid_reference")
        if not items:
            return _Raw("ordinal", "not_found", (), src, "no_set")
        if n > len(items):
            ids = tuple(it.product_id for it in items)
            return _Raw("ordinal", "ambiguous", ids, src, "ordinal_out_of_range")
        return _Raw("ordinal", "exact", (items[n - 1].product_id,), src, found)

    def _zoomed_list(self, items: tuple[ShownItem, ...]) -> tuple[ShownItem, ...] | None:
        """`kernel.v6.0` (NX-364): un singur produs pe ecran, care face parte din cea mai recentă
        listă de cel puțin două, înseamnă că clientul a intrat în DETALIUL unui produs din listă.
        Ordinalele de după («compară prima cu a treia») numără lista, nu ecranul de un card: pe
        conversația `0e88752a` ecranul detaliului făcea din «a treia» `ordinal_out_of_range`.

        Criteriul e structural (apartenența la listă), nu textual. Un produs unic venit dintr-o
        căutare nouă nu e în lista de dinainte, deci ordinalul rămâne pe ecran, ca înainte. Pe
        `resume` focusul e setul parcat, cu propria listă, deci nu se aplică."""
        if (
            not self.sources.zoom_ordinals
            or len(items) != 1
            or self.sources.thread == "resume"
            or not self.sources.shown_earlier
        ):
            return None
        newest = self.sources.shown_earlier[0]
        if len(newest) < 2:
            return None
        if items[0].product_id not in {it.product_id for it in newest}:
            return None
        return newest

    def deictic(self, ref: Reference) -> _Raw:
        s = self.sources
        if s.action is not None:
            anchor = s.action
            if not anchor.valid:
                return _Raw("deictic", "stale", (), "action", "anchor_invalid")
            if anchor.revision and s.displayed_revision and anchor.revision != s.displayed_revision:
                return _Raw("deictic", "stale", (), "action", "anchor_stale")
            return _Raw("deictic", "exact", (anchor.product_id,), "action", "action_anchor")
        if s.page is not None:
            return _Raw("deictic", "exact", (s.page.product_id,), "page", "page_deictic")
        if s.focus:
            src: Source = (
                "shown_now"
                if any(it.product_id == s.focus for it in s.shown_now)
                else "shown_earlier"
            )
            return _Raw("deictic", "exact", (s.focus,), src, "focus")
        items = s.focus_set
        if len(items) == 1:
            return _Raw("deictic", "exact", (items[0].product_id,), s.focus_source, "single_in_set")
        if items:
            ids = tuple(it.product_id for it in items)
            return _Raw("deictic", "ambiguous", ids, s.focus_source, "no_anchor")
        return _Raw("deictic", "not_found", (), s.focus_source, "no_set")

    def name(self, ref: Reference) -> _Raw:
        name = ref.name or ref.value or ""
        if not _content(tokens(name), self.stop):
            return _Raw("name", "not_found", (), self.sources.focus_source, "invalid_reference")
        searched: list[tuple[Source, tuple[ShownItem, ...]]] = self.sources.sets_in_order()
        if self.sources.page is not None:
            searched.append(("page", (self.sources.page,)))
        for src, items in searched:
            hit = match_name_in_set(name, items, self.stop, self.suffixes)
            if hit is not None:
                reason = "named" if hit.outcome == "exact" else "name_shared"
                return _Raw("name", hit.outcome, hit.ids, src, reason)
        catalog = self.facts.named.get(name_key(name), ())
        if catalog:
            top = catalog[0][1]
            tied = _unique(pid for pid, length in catalog if length == top)
            if len(tied) == 1:
                return _Raw("name", "exact", tied, "catalog", "named")
            return _Raw("name", "ambiguous", tied, "catalog", "catalog_tie")
        for src, items in searched:
            hit = self._described_match(name, items)
            if hit is not None:
                reason = "named_with_qualifier" if hit.outcome == "exact" else "name_shared"
                return _Raw("name", hit.outcome, hit.ids, src, reason)
        return self._reclassify(name)

    def _described_match(self, name: str, items: tuple[ShownItem, ...]) -> _Hit | None:
        """Un nume cu cuvinte care DESCRIU produsul, nu îl numesc: «BELIF pentru ten uscat»,
        «crema Dokdo» lângă un card „ROUND LAB 1025 Dokdo Cream". None = setul ratează.

        Regula „un cuvânt pe care nu-l poartă niciun produs scoate setul din joc" (felia 2a) e
        scrisă pentru un nume CONCURENT («Xiaomi» lângă «Samsung Phone»). Sonda NX-329 a arătat că
        pe chip-urile noastre de comparație cuvântul străin e aproape mereu o DESCRIERE: o nevoie
        („pentru ten uscat") sau tipul produsului în limba clientului („crema", pe un nume în
        engleză). Vocabularul le deosebește. Fiecare secvență de cuvinte pe care nu le poartă
        niciun nume din set se rezolvă ÎNTREAGĂ: o proprietate (dimensiune care nu numește produse)
        se ignoră; o valoare de referință (tip, marcă) devine filtru pe faptele RECITITE ale
        produsului; o secvență necunoscută e un nume concurent și scoate setul, ca înainte. Restul
        numelui se potrivește pe produsele rămase. Rulează doar după ce numele întreg a ratat toate
        sursele și catalogul, deci nu poate înlocui o potrivire mai precisă. Fără vocabular, nimic.
        """
        if self.vocab is None or not items:
            return None
        names = [tuple(tokens(it.name)) for it in items]
        runs: list[list[str]] = []  # secvențele de cuvinte pe care nu le poartă niciun nume
        kept: list[str] = []  # restul numelui
        current: list[str] = []
        gap: list[str] = []  # cuvinte goale în așteptare: leagă două cuvinte străine, altfel rămân
        for word in tokens(name):
            if not _content((word,), self.stop):
                gap.append(word)
            elif any(_carries(nw, word, self.suffixes) for nw in names):
                if current:
                    runs.append(current)
                    current = []
                kept += gap + [word]
                gap = []
            else:
                if current:
                    current += gap
                else:
                    kept += gap
                current.append(word)
                gap = []
        if current:
            runs.append(current)
        kept += gap
        if not runs or not _content(kept, self.stop):
            return None
        allowed = list(items)
        for run in runs:
            res = self._value_of(" ".join(run))
            if res is None:
                return None
            if res.dimension in self.reference_dims:
                wanted = {name_key(k) for k in res.constraint_keys}
                allowed = [
                    it
                    for it in allowed
                    if self._known(it)
                    and any(
                        name_key(v) in wanted
                        for v in _values_of(self.facts.products[it.product_id], res.dimension)
                    )
                ]
        return match_name_in_set(" ".join(kept), allowed, self.stop, self.suffixes)

    def _value_of(self, phrase: str) -> Resolution | None:
        """Valoarea din vocabular pe care `phrase` o numește ÎNTREAGĂ, sau None."""
        if self.vocab is None:
            return None
        res = resolve_any(self.vocab, phrase, overlays=self.overlays)
        if res.status is ResolutionStatus.UNKNOWN:
            return None
        if res.matched_by == "tokens":
            # Vocabularul potrivește și pe SUBMULȚIME de cuvinte: „serul cu vitamina c" lovește
            # valoarea „vitamina c". Dar fraza spune mai mult decât valoarea (are un cap nominal,
            # „serul"), deci DESCRIE un produs, nu numește o proprietate. Respinsă ca proprietate
            # (I24), ar fi blocat căutarea după nume pe care contractul o cere pentru `not_found`.
            entry = {w for key in res.constraint_keys for w in tokens(key)}
            if set(_content(tokens(phrase), self.stop)) - entry:
                return None
        return res

    def attribute(self, ref: Reference) -> _Raw:
        value = ref.value or ref.name or ""
        dim = ref.dimension
        if not dim:
            raw = self._reclassify(value)
            if raw.reason == "name_not_found":
                return _Raw("attribute", "not_found", (), raw.source, "attribute_not_in_sets")
            return raw
        if dim not in self.reference_dims and dim != VARIANT_DIMENSION:
            return _Raw("attribute", "not_found", (), self.sources.focus_source, "denotes_property")
        direct = self._members_by_words(value, (dim,))
        if direct is not None:
            return direct
        if self.vocab is None:
            return _Raw(
                "attribute", "not_found", (), self.sources.focus_source, "vocabulary_unavailable"
            )
        res = resolve(self.vocab, value, dim, overlay=(self.overlays or {}).get(dim))
        keys = res.constraint_keys
        if not keys:
            return _Raw(
                "attribute", "not_found", (), self.sources.focus_source, "attribute_not_in_sets"
            )
        return self._members_by_keys(dim, keys)

    def extreme(self, ref: Reference) -> _Raw:
        src, items = self.sources.focus_source, self.sources.focus_set
        dim = ref.dimension or PRICE_DIMENSION
        if ref.direction not in ("min", "max"):
            return _Raw("extreme", "not_found", (), src, "invalid_reference")
        if not items:
            return _Raw("extreme", "not_found", (), src, "no_set")
        members = [self.facts.products[it.product_id] for it in items if self._known(it)]
        if not members:
            return _Raw("extreme", "stale", (), src, "not_in_catalog")
        values = {f.product_id: _numeric(f, dim) for f in members}
        if len(members) < len(_unique(it.product_id for it in items)) and any(
            v is not None for v in values.values()
        ):
            # Un produs pe care clientul l-a VĂZUT a dispărut din catalog: valoarea lui e
            # necunoscută, iar extremul celor rămași poate să nu fie cel la care se referă
            # clientul (dacă cel șters era cel mai ieftin). Nu ghicim: ambiguu, cu cei rămași.
            return _Raw("extreme", "ambiguous", tuple(values), src, "extreme_unknown_value")
        if dim not in _COLUMN_DIMENSIONS and all(v is None for v in values.values()):
            # Nicio valoare numerică pe nimeni: dimensiunea nu se ordonează (sau nu există).
            return _Raw("extreme", "not_found", (), src, "not_orderable")
        if any(v is None for v in values.values()):
            # O valoare necunoscută pe UN candidat ⇒ nu știm care e extremul. Nu ghicim.
            return _Raw("extreme", "ambiguous", tuple(values), src, "extreme_unknown_value")
        known = {pid: v for pid, v in values.items() if v is not None}
        best = min(known.values()) if ref.direction == "min" else max(known.values())
        tied = tuple(pid for pid, v in known.items() if v == best)
        if len(tied) > 1:
            return _Raw("extreme", "ambiguous", tied, src, "extreme_tie")
        return _Raw("extreme", "exact", tied, src, "extreme")

    def the_other(self, ref: Reference) -> _Raw:
        src, items = self.sources.focus_source, self.sources.focus_set
        if not items:
            return _Raw("the_other", "not_found", (), src, "no_set")
        ids = tuple(it.product_id for it in items)
        if len(items) != 2:
            return _Raw("the_other", "ambiguous", ids, src, "not_a_pair")
        focus = next((pid for pid in reversed(self.exact_before) if pid in ids), None)
        if focus is None and self.sources.focus in ids:
            focus = self.sources.focus
        if focus is None:
            return _Raw("the_other", "ambiguous", ids, src, "no_focus")
        other = ids[1] if ids[0] == focus else ids[0]
        return _Raw("the_other", "exact", (other,), src, "the_other")

    def earlier(self, ref: Reference) -> _Raw:
        sets: list[tuple[Source, tuple[ShownItem, ...]]] = [
            ("shown_earlier", s) for s in self.sources.shown_earlier if s
        ]
        if self.sources.parked and self.sources.thread != "resume":
            sets.append(("parked", self.sources.parked))
        if not sets:
            return _Raw("earlier", "not_found", (), "shown_earlier", "no_earlier")
        if ref.name:
            for src, items in sets:
                hit = match_name_in_set(ref.name, items, self.stop, self.suffixes)
                if hit is not None:
                    reason = "named" if hit.outcome == "exact" else "name_shared"
                    return _Raw("earlier", hit.outcome, hit.ids, src, reason)
            return _Raw("earlier", "not_found", (), sets[-1][0], "name_not_found")
        src, newest = sets[0]
        if ref.ordinal is not None:
            if ref.ordinal < 1:
                return _Raw("earlier", "not_found", (), src, "invalid_reference")
            if ref.ordinal > len(newest):
                ids = tuple(it.product_id for it in newest)
                return _Raw("earlier", "ambiguous", ids, src, "ordinal_out_of_range")
            return _Raw(
                "earlier", "exact", (newest[ref.ordinal - 1].product_id,), src, "ordinal_in_set"
            )
        if ref.value:
            dims = (ref.dimension,) if ref.dimension else tuple(self.reference_dims)
            raw = self._members_by_words(ref.value, dims, sets=sets)
            if raw is not None:
                return _replace_kind(raw, "earlier")
            return _Raw("earlier", "not_found", (), src, "attribute_not_in_sets")
        if len(newest) == 1:
            return _Raw("earlier", "exact", (newest[0].product_id,), src, "earlier_single")
        ids = tuple(it.product_id for it in newest)
        return _Raw("earlier", "ambiguous", ids, src, "earlier_unspecified")

    # --- atribute ---------------------------------------------------------------------------------

    def _known(self, item: ShownItem) -> bool:
        return item.product_id in self.facts.products

    def _members(
        self,
        matches: Callable[[ProductFacts], bool],
        sets: Sequence[tuple[Source, tuple[ShownItem, ...]]] | None = None,
    ) -> _Raw | None:
        """Primul set cu membri care îndeplinesc `matches(facts)`: unul ⇒ exact, mai mulți ⇒
        ambiguu. None = niciun set nu are membri."""
        for src, items in sets if sets is not None else self.sources.sets_in_order():
            members = _unique(
                it.product_id
                for it in items
                if self._known(it) and matches(self.facts.products[it.product_id])
            )
            if len(members) == 1:
                return _Raw("attribute", "exact", members, src, "attribute_match")
            if members:
                return _Raw("attribute", "ambiguous", members, src, "attribute_shared")
        return None

    def _members_by_words(
        self,
        value: str,
        dims: Iterable[str],
        *,
        sets: Sequence[tuple[Source, tuple[ShownItem, ...]]] | None = None,
    ) -> _Raw | None:
        """Membrii ale căror valori pe `dims` conțin TOATE cuvintele de conținut din `value`
        («Samsung-ul» ⇒ marca Samsung), fără vocabular."""
        content = set(_content(tokens(value), self.stop))
        if not content:
            return None
        dims = tuple(dims)

        def matches(f: ProductFacts) -> bool:
            return any(content <= set(tokens(v)) for d in dims for v in _values_of(f, d))

        return self._members(matches, sets)

    def _members_by_keys(self, dim: str, keys: Iterable[str]) -> _Raw:
        wanted = {name_key(k) for k in keys}

        def matches(f: ProductFacts) -> bool:
            return any(name_key(v) in wanted for v in _values_of(f, dim))

        found = self._members(matches)
        if found is not None:
            return found
        return _Raw(
            "attribute", "not_found", (), self.sources.focus_source, "attribute_not_in_sets"
        )

    def _reclassify(self, value: str) -> _Raw:
        """Un nume care nu numește niciun produs: îl numește o VALOARE? Pe o dimensiune de
        referință ⇒ referință de tip atribut, rezolvată pe seturi. Pe altă dimensiune (o nevoie, un
        raft) ⇒ `denotes_property`, pe care `gate_act_targets` îl respinge (I24)."""
        focus = self.sources.focus_source
        direct = self._members_by_words(value, (*sorted(self.reference_dims), VARIANT_DIMENSION))
        if direct is not None:
            return direct
        res = self._value_of(value)
        if res is None:
            return _Raw("name", "not_found", (), focus, "name_not_found")
        if res.dimension not in self.reference_dims:
            return _Raw("attribute", "not_found", (), focus, "denotes_property")
        return self._members_by_keys(res.dimension, res.constraint_keys)

    # --- revalidarea (I1) -------------------------------------------------------------------------

    def finalize(self, ref: Reference, raw: _Raw) -> ResolvedRef:
        ids = _unique(pid for pid in raw.ids if pid in self.facts.products)
        outcome, reason = raw.outcome, raw.reason
        if outcome == "exact":
            if not ids:
                outcome, reason, ids = "stale", "not_in_catalog", ()
            else:
                ids = ids[:1]
                facts = self.facts.products[ids[0]]
                shown = self.sources.shown_prices().get(ids[0])
                if facts.available is False:
                    reason = "unavailable"
                elif (
                    shown is not None
                    and facts.price is not None
                    and abs(float(facts.price) - shown) > _PRICE_EPSILON
                ):
                    reason = "price_changed"
                self.exact_before.append(ids[0])
        elif outcome == "ambiguous":
            if raw.ids and not ids:
                outcome, reason = "stale", "not_in_catalog"
        else:
            ids = ()
        return ResolvedRef(
            ref_id=ref.id,
            kind=raw.kind,
            outcome=outcome,
            product_ids=list(ids),
            source=raw.source,
            reason=reason,
        )


def _reference_dims(pack: object | None) -> frozenset[str]:
    """Dimensiunile care numesc produse (I24): ale pachetului, altfel implicitul lui."""
    dims = getattr(pack, "reference_dimensions", None)
    return frozenset(dims if dims is not None else DEFAULT_REFERENCE_DIMENSIONS)


def _replace_kind(raw: _Raw, kind: str) -> _Raw:
    return _Raw(kind, raw.outcome, raw.ids, raw.source, raw.reason)


# --- API-ul public -------------------------------------------------------------------------------


def plan_lookup(
    refs: Sequence[Reference],
    sources: ReferenceSources,
    *,
    pack: object | None = None,
    locale: str | None = None,
    extra_attributes: Collection[str] = (),
) -> CatalogLookup:
    """Ce trebuie citit din catalog ca `resolve_references` să poată decide. PUR.

    `extra_attributes` (NX-332): atributele pe care le cere alt consumator al ACELEIAȘI citiri
    (poarta de ambiguitate, `ambiguity_gate.lookup_attributes`), ca turul să rămână la un checkout.

    Toate id-urile din surse se revalidează (I1: și cele parcate sau vechi, nu doar ținta). Un nume
    se caută în catalog doar dacă nu-l numește niciun set: pe ecran, numele bate catalogul."""
    stop = _stop(locale)
    suffixes = inflection_suffixes(locale)
    ids = sources.all_ids()[:MAX_LOOKUP_IDS]
    searched = [items for _, items in sources.sets_in_order()]
    if sources.page is not None:
        searched.append((sources.page,))
    names: dict[
        str, str
    ] = {}  # cheie → numele așa cum l-a dat referința (punctuația contează în SQL)
    for ref in refs:
        if ref.kind != "name":
            continue
        name = ref.name or ref.value or ""
        key = name_key(name)
        if not key or not _content(tokens(name), stop) or key in names:
            continue
        if any(match_name_in_set(name, items, stop, suffixes) is not None for items in searched):
            continue
        names[key] = name
    attrs = set(_reference_dims(pack))
    attrs |= {
        r.dimension for r in refs if r.kind in ("attribute", "extreme", "earlier") and r.dimension
    }
    attrs |= {a for a in extra_attributes if a}
    attrs -= _COLUMN_DIMENSIONS | {VARIANT_DIMENSION}
    return CatalogLookup(
        ids=tuple(ids),
        names=tuple(list(names.values())[:MAX_NAMES]),
        attributes=tuple(sorted(attrs)),
    )


def sources_from_state(state: ConversationStateV2, thread: str) -> ReferenceSources:
    """Sursele resolverului din starea REDUSĂ (NX-335, promovat din helperul de test NX-331):
    ecranul (`displayed_products`), seturile de mai devreme (`recent_sets`), setul parcat și
    focusul (`selected_product`). PUR.

    Ecranul are O singură sursă, aceeași din care adaptorul de interpretare randează `#i`, deci
    poziția din prompt și `ordinal=i` de aici nu pot diverge. Pagina și acțiunea nu sunt în stare:
    le adaugă apelantul care le are (pe calea v2, `deterministic._state_v2_sources`)."""

    def items(refs: Iterable[object]) -> tuple[ShownItem, ...]:
        return tuple(
            ShownItem(d.product_id, d.name, d.price)  # type: ignore[attr-defined]
            for d in refs
        )

    parked = state.parked
    return ReferenceSources(
        shown_now=items(state.references.displayed_products),
        shown_earlier=tuple(items(s) for s in state.references.recent_sets),
        parked=items(parked.shown) if parked is not None else (),
        focus=state.references.selected_product,
        thread=thread,  # type: ignore[arg-type]
        zoom_ordinals=True,
    )


def resolve_references(
    refs: Sequence[Reference],
    sources: ReferenceSources,
    facts: ReferenceFacts,
    *,
    vocab: CatalogVocabulary | None = None,
    pack: object | None = None,
    locale: str | None = None,
) -> list[ResolvedRef]:
    """Un `ResolvedRef` per referință, în ordinea lor (`the_other` depinde de o țintă anterioară).

    Garanții, testate: fiecare id din ieșire e cheie în `facts.products` (I1); o egalitate nu iese
    niciodată `exact`; un motiv e mereu din `REASONS`. Vocabular absent ⇒ numele găsite pe seturi se
    rezolvă normal, iar o valoare care nu se poate judeca iese `not_found`."""
    resolver = _Resolver(sources, facts, vocab=vocab, pack=pack, locale=locale)
    handlers = {
        "ordinal": resolver.ordinal,
        "deictic": resolver.deictic,
        "name": resolver.name,
        "attribute": resolver.attribute,
        "extreme": resolver.extreme,
        "the_other": resolver.the_other,
        "earlier": resolver.earlier,
    }
    return [resolver.finalize(ref, handlers[ref.kind](ref)) for ref in refs]


TargetVerdict = Literal["ok", "invalid_reference_target", "not_exact", "unknown_reference"]
#: Actele care MUTĂ ceva (I10). Checkout-ul trece prin coș, deci nu e un act separat.
MUTATING_ACTS: frozenset[str] = frozenset({"cart"})


@dataclass(frozen=True)
class TargetCheck:
    act_index: int
    target: str
    verdict: TargetVerdict


def gate_act_targets(acts: Sequence[Act], resolved: Sequence[ResolvedRef]) -> list[TargetCheck]:
    """Poarta țintelor de act. PURĂ.

    I24 întâi: o țintă care numește o proprietate („link la roșeață") e respinsă pe ORICE act,
    fiindcă e o schimbare de stare, nu o țintă. Apoi I10: un act care mută ceva cere țintă
    `exact` (un „cel mai ieftin" rezolvat exact e o țintă validă chiar și pentru coș). O țintă
    nedeclarată iese `unknown_reference`: validarea completă I22 e a pasului 5, aici poarta e
    doar totală."""
    by_id = {r.ref_id: r for r in resolved}
    out: list[TargetCheck] = []
    for index, act in enumerate(acts):
        for target in act.targets:
            ref = by_id.get(target)
            verdict: TargetVerdict
            if ref is None:
                verdict = "unknown_reference"
            elif ref.reason == "denotes_property":
                verdict = "invalid_reference_target"
            elif act.kind in MUTATING_ACTS and ref.outcome != "exact":
                verdict = "not_exact"
            else:
                verdict = "ok"
            out.append(TargetCheck(index, target, verdict))
    return out


__all__ = [
    "MUTATING_ACTS",
    "REASONS",
    "ActionRef",
    "CatalogLookup",
    "ProductFacts",
    "ReferenceFacts",
    "ReferenceSources",
    "ShownItem",
    "TargetCheck",
    "gate_act_targets",
    "match_name_in_set",
    "name_key",
    "plan_lookup",
    "resolve_references",
    "sources_from_state",
]
