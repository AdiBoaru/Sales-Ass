"""NX-330 (kernel v1.0, pasul 3a) — proveniența unei schimbări de stare, calculată de COD.

Contractul (`docs/KERNEL-CONTRACT-v1.md`, „Provenance and strength"): modelul dă doar CITATUL, nu
poate declara un nivel. Codul îl calculează în patru pași, în ordine:

1. **Localizarea citatului** în mesajele CLIENTULUI (turul curent + turele lui din fereastră), pe
   cuvinte întregi, pliate. Negăsit ⇒ `inferred`. Mesajele botului nu ajung niciodată aici
   (`UserWords` le exclude prin construcție), iar potrivirea NU e pe prefix: «mat» nu e în
   «matreata».
2. **Rezolvarea citatului** prin vocabularul turului: se rezolvă pe valoarea propusă ⇒ candidat
   `explicit`; pe ALTĂ valoare ⇒ `semantic_mismatch`; pe nimic ⇒ `implicit`.
3. **Polaritatea**, pe markerii per locale din `query_terms`: `avoid` cere negație, `lte`/`gte` cer
   comparator; un marker lipsă retrogradează la `implicit`; negația lângă o valoare `eq` e
   `polarity_conflict`.
4. **Tăria**: `explicit` ⇒ `hard` doar pe o dimensiune hard-capable (I8), altfel `soft`; `implicit`
   ⇒ `soft`; `inferred` ⇒ `ranking`, doar turul curent (I23).

Plus validările care resping (vocabularul închis `ChangeReject`): dimensiune necunoscută, handle
necunoscut, referință nedeclarată (I22), unitatea altei dimensiuni (I9), conflict de polaritate sau
de sens, limite dure încrucișate în același tur, plafoanele de runtime.

Modul PUR (rolul `pure` din `tests/kernel_modules.json`): niciun client de model, niciun I/O, niciun
ceas. Nu ramifică pe textul brut decât prin tabelele per locale din `query_terms` (singura excepție
permisă de poarta de text brut) și prin vocabular. Nu conține literali de vertical (I14)."""

from __future__ import annotations

from collections.abc import Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from src.catalog.query_terms import (
    comparators,
    fold,
    inflection_suffixes,
    negation_markers,
    relative_comparators,
    stopwords,
    tokens,
)
from src.catalog.vocabulary import (
    CATEGORY_DIMENSION,
    CatalogVocabulary,
    Resolution,
    ResolutionStatus,
    facet_overlays,
    resolve,
    resolve_any,
)
from src.conversation.interpretation import (
    UNIVERSAL_DIMENSIONS,
    ChangeReject,
    CheckedChange,
    Provenance,
    StateChange,
    TurnInterpretation,
)
from src.conversation.needs import (
    HARD,
    PRICE_BOUNDS,
    PRICE_DIMENSION,
    UNIVERSAL_SPECS,
    NeedVocabulary,
)
from src.conversation.state_v2 import MAX_TYPE_UMBRELLA, Need
from src.domain.constraints import EMPTY_UNITS, UnitRegistry
from src.domain.facets import FacetType

#: Plafoanele de runtime ale contractului (decizia 3 din „Review decisions"). Peste ele, restul se
#: taie de la coadă, în ordine, și se numără (`interpretation_truncated`).
MAX_CHANGES = 10
MAX_REFERENCES = 6
MAX_ACTS = 3

PRICE = PRICE_DIMENSION
UNMAPPED = "unmapped"
#: `kernel.v6.0` (NX-364): o limită de preț FĂRĂ număr și fără țintă («să nu fie foarte scump»,
#: «ceva mai de calitate, nu contează prețul»). Valoarea e o BANDĂ față de prețurile a ce se
#: potrivește cererii, nu o sumă: `lte` ⇒ jumătatea ieftină, `gte` ⇒ cea scumpă.
PRICE_BAND_LOW = "band:low"
PRICE_BAND_HIGH = "band:high"
#: Cheile de nevoie care stau pe dimensiunea prețului.
PRICE_KEYS: frozenset[str] = frozenset(PRICE_BOUNDS)

#: Potrivirile de vocabular care pot face un citat `explicit` sau îl pot contrazice. `tokens` (o
#: submulțime de cuvinte) nu intră: „dus" ar fi atunci o valoare doar fiindcă apare în „gel de dus".
_STRONG_MATCH: frozenset[str] = frozenset({"exact", "overlay"})
#: Cea mai lungă secvență de cuvinte din citat încercată pe vocabular (valorile au 1-3 cuvinte).
_MAX_NGRAM = 4
#: Câte cuvinte înaintea valorii poate sta o negație ca să o contrazică („nu gras").
_NEGATION_WINDOW = 2
#: NX-349: valorile unei fațete da/nu, cum le scrie MODELUL (limbajul schemei, nu al clientului).
#: Vocabularul nu ține booleeni (`catalog.vocabulary`), deci fără ramura asta orice `set
#: fragrance_free true` ajungea `unmapped` cu valoarea „true", adică termen de ordonare.
_FLAGS: Mapping[str, bool] = {"true": True, "false": False}
#: Relațiile cu care o fațetă da/nu are sens: valoarea poartă deja polaritatea («fără parfum» =
#: `true`), deci `avoid`/`lte`/`gte` pe ea sunt un conflict de polaritate.
_FLAG_RELATIONS = frozenset({"eq", "contains"})


@dataclass(frozen=True)
class UserWords:
    """Cuvintele CLIENTULUI. Construite de apelant din mesajul curent și turele clientului din
    fereastra de istoric; mesajele botului nu au loc aici, deci un citat găsit doar în replica
    botului iese `inferred` prin construcție."""

    current: str
    earlier: tuple[str, ...] = ()  # cel mai recent primul


@dataclass(frozen=True)
class Handle:
    """Un handle „cN" pentru o nevoie activă: ce vede modelul în prompt (pasul 5) și ce validează
    codul aici. `revision` = `updated_revision` al nevoii, pentru regula de corecție (NX-331)."""

    handle: str
    key: str
    value: str | float | bool | None
    source: str
    written_turn: str | None = None
    revision: int = 0
    #: NX-334: dimensiunea citită din vocabular la construcție (`storage_min` → `storage`). Fără
    #: vocabular rămâne regula universală (bugetul stă pe `price`).
    of_dimension: str | None = None

    @property
    def dimension(self) -> str:
        return self.of_dimension or dimension_of_key(self.key)


def dimension_of_key(key: str, vocabulary: NeedVocabulary | None = None) -> str:
    """Cheia unei nevoi → dimensiunea contractului. Bugetul stă pe `price`, iar limitele unei
    fațete numerice (`storage_min` / `storage_max`, NX-334) pe fațeta lor, citite din tabelul
    vocabularului, nu din sufixul cheii."""
    if key in PRICE_KEYS:
        return PRICE
    return vocabulary.dimension_of(key) if vocabulary is not None else key


def need_handles(needs: Sequence[Need], vocabulary: NeedVocabulary) -> tuple[Handle, ...]:
    """Handle-uri STABILE: nevoile ACTIVE, ordonate după (cheie, valoare), numerotate c1..cN.

    Aceeași funcție scrie promptul (pasul 5) și validează aici, deci un handle nu poate însemna
    două lucruri. Ordinea nu depinde de ordinea de inserție: o nevoie reafirmată nu renumerotează
    restul."""
    active = sorted(
        (n for n in needs if n.is_active), key=lambda n: (n.key, str(n.normalized_value))
    )
    return tuple(
        Handle(
            handle=f"c{i}",
            key=n.key,
            value=n.normalized_value,
            source=n.source,
            written_turn=n.source_turn_id,
            revision=n.updated_revision,
            of_dimension=dimension_of_key(n.key, vocabulary),
        )
        for i, n in enumerate(active, 1)
    )


def tenant_dimensions(pack: object | None) -> frozenset[str]:
    """Enumul de dimensiuni al tenantului: cheile de fațetă ale pachetului + cele universale.
    Același enum pe care îl primește modelul (`build_interpretation_schema`)."""
    facets = getattr(pack, "facets", ()) or ()
    return frozenset({f.key for f in facets if getattr(f, "key", None)} | set(UNIVERSAL_DIMENSIONS))


def hard_capable(dimension: str, pack: object | None) -> bool:
    """I8: o fațetă `enforce_ready`, sau o nevoie universală dură (buget, mărime, restricție).

    Acoperirea unei fațete dă dreptul de a FILTRA; dreptul de a EXCLUDE cere auditul de precizie
    (NX-268/271), adică exact `enforce_ready`. Pe datele de azi nicio fațetă nu-l are, deci dur e
    doar prețul și cheile universale dure."""
    if dimension == PRICE:
        return True
    for facet in getattr(pack, "facets", ()) or ():
        if getattr(facet, "key", None) == dimension and getattr(facet, "enforce_ready", False):
            return True
    return any(s.key == dimension and s.default_strength == HARD for s in UNIVERSAL_SPECS)


def check_targets(interp: TurnInterpretation) -> list[str]:
    """I22: fiecare `Act.targets` și fiecare `StateChange.relative_to` numește un `Reference.id`
    declarat în aceeași interpretare (după plafonul de referințe). Întoarce id-urile necunoscute, în
    ordinea apariției; apelantul respinge turul cu `unknown_reference`."""
    declared = {r.id for r in interp.references[:MAX_REFERENCES]}
    unknown: list[str] = []
    for act in interp.acts[:MAX_ACTS]:
        unknown += [t for t in act.targets if t not in declared and t not in unknown]
    for change in interp.changes[:MAX_CHANGES]:
        rel = change.relative_to
        if rel is not None and rel not in declared and rel not in unknown:
            unknown.append(rel)
    return unknown


# --- implementare --------------------------------------------------------------------------------


def _find(hay: Sequence[str], needle: Sequence[str]) -> int:
    """Poziția primei apariții CONTIGUE a lui `needle` în `hay`, sau -1."""
    n = len(needle)
    if not n:
        return -1
    for i in range(len(hay) - n + 1):
        if tuple(hay[i : i + n]) == tuple(needle):
            return i
    return -1


def _number_tokens(number: float) -> tuple[str, ...]:
    """Tokenii unui număr, cum apar într-un mesaj pliat („99.5" și „99,5" dau amândouă 99 5)."""
    text = str(int(number)) if float(number).is_integer() else repr(float(number))
    return tuple(tokens(text))


@dataclass(frozen=True)
class _Evidence:
    words: tuple[str, ...]
    located: bool
    comparator_ops: frozenset[str]  # op-urile comparatorilor găsiți în citat
    negations: tuple[int, ...]  # pozițiile negațiilor, după scăderea comparatorilor


#: NX-350: dimensiunea tipului de produs, a doua jumătate a subiectului (`Topic.product_type`).
SUBJECT_TYPE = "product_type"


def _same_stem(a: str, b: str, suffixes: Collection[str]) -> bool:
    """Același cuvânt, eventual flexionat pe AMBELE părți («creme» = „crema": tulpina „crem" +
    „e" / „a"), cu tulpina de cel puțin 3 litere. Sufixele sunt ale locale-i (P11)."""
    if a == b:
        return True
    for sa in ("", *suffixes):
        if sa and not a.endswith(sa):
            continue
        stem = a[: len(a) - len(sa)] if sa else a
        if len(stem) < 3:
            continue
        for sb in ("", *suffixes):
            if b == stem + sb:
                return True
    return False


def _spells(words: Sequence[str], code: Sequence[str], suffixes: Collection[str]) -> bool:
    """NX-350: citatul conține codul ÎNTREG, cuvânt cu cuvânt și în ordine (cu flexiune)."""
    size = len(code)
    return size > 0 and any(
        all(_same_stem(words[i + j], code[j], suffixes) for j in range(size))
        for i in range(len(words) - size + 1)
    )


def _read_quote(quote: str, user: UserWords, locale: str | None) -> _Evidence:
    words = tuple(tokens(quote))
    located = bool(words) and any(
        _find(tokens(message), words) >= 0 for message in (user.current, *user.earlier)
    )
    masked = [False] * len(words)
    ops: set[str] = set()
    for phrase, op in (*comparators(locale), *relative_comparators(locale)):
        needle = tokens(phrase)
        start = _find(words, needle)
        while start >= 0:
            if not any(masked[start : start + len(needle)]):
                ops.add(op)
                for i in range(start, start + len(needle)):
                    masked[i] = True
            nxt = _find(words[start + 1 :], needle)
            start = start + 1 + nxt if nxt >= 0 else -1
    neg = negation_markers(locale)
    negations = tuple(i for i, w in enumerate(words) if w in neg and not masked[i])
    return _Evidence(words, located, frozenset(ops), negations)


class _Checker:
    """Validarea unei interpretări pe instantaneul turului. Fără stare între ture."""

    def __init__(
        self,
        *,
        user: UserWords,
        handles: Sequence[Handle],
        vocab: CatalogVocabulary | None,
        units: UnitRegistry,
        pack: object | None,
        locale: str | None,
        declared_refs: Collection[str],
    ) -> None:
        self.user = user
        self.handles = {h.handle: h for h in handles}
        self.vocab = vocab if vocab is not None and not vocab.is_empty() else None
        self.units = units
        self.pack = pack
        self.locale = locale
        self.stop = stopwords(locale)
        self.dimensions = tenant_dimensions(pack)
        self.declared_refs = frozenset(declared_refs)
        self.overlays: Mapping[str, Mapping[str, str]] = (
            facet_overlays(pack, self.vocab.facet_names) or {} if self.vocab else {}
        )

    # --- vocabular -------------------------------------------------------------------------------

    def _resolve_on(self, phrase: str, dimension: str) -> Resolution | None:
        """Valoarea pe care `phrase` o numește PE `dimension`, doar potriviri tari."""
        if self.vocab is None or not phrase:
            return None
        vocab_dim = CATEGORY_DIMENSION if dimension == CATEGORY_DIMENSION else dimension
        res = resolve(self.vocab, phrase, vocab_dim, overlay=self.overlays.get(vocab_dim))
        if res.status is ResolutionStatus.KNOWN and res.matched_by in _STRONG_MATCH:
            return res
        return None

    def _resolve_anywhere(self, phrase: str) -> Resolution | None:
        """Valoarea pe care `phrase` o numește pe ORICE dimensiune a tenantului, doar tare."""
        if self.vocab is None or not phrase:
            return None
        res = resolve_any(self.vocab, phrase, overlays=self.overlays or None)
        if (
            res.status is ResolutionStatus.KNOWN
            and res.matched_by in _STRONG_MATCH
            and res.dimension in self.dimensions
        ):
            return res
        return None

    def _ngrams(self, words: Sequence[str]) -> Iterator[tuple[int, int, str]]:
        """(start, lungime, frază) pentru secvențele de 1..4 cuvinte care nu încep și nu se termină
        într-un cuvânt gol."""
        for start in range(len(words)):
            for size in range(1, _MAX_NGRAM + 1):
                chunk = words[start : start + size]
                if len(chunk) < size:
                    break
                if chunk[0] in self.stop or chunk[-1] in self.stop:
                    continue
                yield start, size, " ".join(chunk)

    # --- canonic ---------------------------------------------------------------------------------

    def _canonical(self, dimension: str, value: str) -> tuple[str, str | None]:
        """(dimensiunea finală, valoarea canonică). O valoare pe care vocabularul n-o are pe
        dimensiunea propusă devine `unmapped` și se re-rezolvă O DATĂ: cunoscută altundeva ⇒
        promovată pe acea dimensiune; necunoscută ⇒ rămâne `unmapped`, un semnal soft."""
        if dimension != UNMAPPED:
            on = self._resolve_on(value, dimension)
            if on is not None:
                return dimension, on.key
        promoted = self._resolve_anywhere(value)
        if promoted is not None:
            return promoted.dimension, promoted.key
        if dimension != UNMAPPED and self.vocab is None:
            # Fără vocabular nu se poate judeca nimic: valoarea rămâne pe dimensiunea propusă, iar
            # proveniența nu poate trece de `implicit` (citatul nu se poate rezolva).
            return dimension, fold(value).strip() or None
        return UNMAPPED, fold(value).strip() or None

    # --- o schimbare -----------------------------------------------------------------------------

    def check(self, index: int, change: StateChange) -> CheckedChange:
        # Textul brut al citatului se consumă AICI și numai aici, prin tabelele per locale: de
        # aici încolo deciziile se iau pe DOVADA structurată (găsit, poziții, comparatori,
        # negații), nu pe cuvintele clientului (poarta de text brut a contractului).
        return self._decide(index, change, _read_quote(change.quote, self.user, self.locale))

    def _decide(self, index: int, change: StateChange, evidence: _Evidence) -> CheckedChange:
        located_level: Provenance = "explicit" if evidence.located else "inferred"

        def reject(reason: ChangeReject, dimension: str = "", value=None) -> CheckedChange:
            return CheckedChange(
                change=change,
                dimension=dimension or (change.dimension or ""),
                canonical_value=value,
                provenance=located_level,
                strength="ranking",
                rejected=reason,
            )

        if index >= MAX_CHANGES:
            return reject("truncated")

        # Operațiile structurale: nu au valoare de rezolvat, doar un citat care le susține.
        if change.op == "clear":
            if change.target not in ("topic", "all"):
                return reject("unknown_handle")
            return self._structural(change, change.target, evidence)
        if change.op in ("remove", "replace"):
            handle = self.handles.get(change.target or "")
            if handle is None:
                return reject("unknown_handle")
            proposed = change.dimension
            if change.op == "replace" and proposed and proposed != handle.dimension:
                return reject("unknown_handle", handle.dimension)
            if change.op == "remove":
                return self._structural(change, handle.dimension, evidence)
            return self._valued(change, handle.dimension, evidence)

        dimension = change.dimension or ""
        if dimension not in self.dimensions:
            return reject("unknown_dimension")
        return self._valued(change, dimension, evidence)

    def _structural(
        self, change: StateChange, dimension: str, evidence: _Evidence
    ) -> CheckedChange:
        provenance: Provenance = "explicit" if evidence.located else "inferred"
        return CheckedChange(
            change=change,
            dimension=dimension,
            canonical_value=None,
            provenance=provenance,
            strength="soft" if provenance != "inferred" else "ranking",
            rejected=None,
        )

    def _valued(self, change: StateChange, dimension: str, evidence: _Evidence) -> CheckedChange:
        located: Provenance = "explicit" if evidence.located else "inferred"

        def reject(reason: ChangeReject, value=None) -> CheckedChange:
            return CheckedChange(
                change=change,
                dimension=dimension,
                canonical_value=value,
                provenance=located,
                strength="ranking",
                rejected=reason,
            )

        relation = change.relation or "eq"
        if change.relative_to is not None:
            if change.relative_to not in self.declared_refs:
                return reject("unknown_reference")
            if relation not in ("lte", "gte"):
                return reject("polarity_conflict")
            if dimension != PRICE:
                # Contractul („Ownership"): valoarea unei schimbări relative o calculează codul din
                # PREȚUL recitit al țintei. Pe altă dimensiune numărul ar fi prețul pus drept
                # lățime sau capacitate (NX-334: «mai îngustă decât asta» scria `width_max = 1999`),
                # adică exact clasa I9: o valoare a altei dimensiuni.
                return reject("unit_mismatch")
            # Valoarea o calculează `delta.py` din prețul RECITIT al țintei; aici doar proveniența.
            level = self._polarity_level(
                located, relation, evidence, dimension, number_in_quote=False
            )
            return self._finish(change, dimension, None, level)

        hit_at, hit_size = -1, 0
        if change.number is not None:
            canonical_number = self._number(change, dimension)
            if canonical_number is None:
                return reject("unit_mismatch")
            canonical: str | float | None = canonical_number
            number_at = _find(evidence.words, _number_tokens(change.number))
            level: Provenance = located
            if located == "explicit" and number_at < 0:
                level = "implicit"  # citatul e al clientului, numărul nu
            hit_at, hit_size = number_at, len(_number_tokens(change.number))
            number_in_quote = number_at >= 0
        else:
            if not change.value:
                return reject("unknown_dimension")
            facet = self._bool_facet(dimension)
            if facet is not None:
                # Recenzia NX-349: pe o fațetă da/nu valoarea e o STARE. Altceva nu coboară pe
                # `unmapped` (acolo «fara parfum» ar deveni termen de ordonare și text de căutare).
                flag = self._flag_value(facet, change.value)
                if flag is None:
                    return reject("semantic_mismatch")
                return self._flag(change, dimension, facet, flag, evidence)
            if (
                dimension == PRICE
                and relation in ("lte", "gte")
                and not any(ch.isdigit() for ch in change.value)
            ):
                # kernel.v6.0 (NX-364): pe v5.1 valoarea («ft scump») nu se rezolva pe preț, cobora
                # pe `unmapped` și devenea termen de ordonare, adică ordona după cuvântul „scump”.
                # Sensul e în RELAȚIA scrisă de model (`lte`), nu într-o listă de cuvinte: devine
                # o bandă, semnal al turului (`ranking`, nepersistat, ca un `inferred`, I23),
                # fiindcă o dorință vagă nu e un buget. O valoare cu cifre («100») e o sumă
                # scrisă fără `number` (recenzia): rămâne pe drumul de dinainte.
                band = PRICE_BAND_LOW if relation == "lte" else PRICE_BAND_HIGH
                return CheckedChange(
                    change=change,
                    dimension=PRICE,
                    canonical_value=band,
                    provenance=located,
                    strength="ranking",
                    rejected=None,
                )
            dimension, canonical = self._canonical(dimension, change.value)
            level = located
            number_in_quote = False
            if located == "explicit":
                level, hit_at, mismatch, hit_size = self._resolve_quote(
                    evidence, dimension, canonical
                )
                if mismatch:
                    return reject("semantic_mismatch", canonical)

        if relation in ("eq", "contains") and hit_at >= 0:
            window = range(max(0, hit_at - _NEGATION_WINDOW), hit_at)
            if any(p in window for p in evidence.negations):
                return reject("polarity_conflict", canonical)
        level = self._polarity_level(level, relation, evidence, dimension, number_in_quote)
        checked = self._finish(change, dimension, canonical, level)
        if level == "explicit" and hit_at >= 0 and hit_size:
            # NX-352: cuvintele citatului care au NUMIT valoarea (nu tot citatul): doar ele pot
            # pleca din textul căutării, fiindcă doar pe ele le poartă filtrul.
            matched = tuple(evidence.words[hit_at : hit_at + hit_size])
            checked = checked.model_copy(update={"matched": matched})
        if dimension == SUBJECT_TYPE and level == "implicit" and evidence.located:
            umbrella = self._umbrella(evidence, canonical)
            if umbrella:
                checked = checked.model_copy(update={"umbrella": umbrella})
        return checked

    def _bool_facet(self, dimension: str) -> object | None:
        """Fațeta da/nu a pachetului pe `dimension`, sau `None`."""
        for facet in getattr(self.pack, "facets", ()) or ():
            if getattr(facet, "key", None) != dimension:
                continue
            return facet if getattr(facet, "value_type", None) is FacetType.BOOL else None
        return None

    def _flag_phrases(self, facet: object) -> list[tuple[tuple[str, ...], bool]]:
        """Frazele care NUMESC o fațetă da/nu, cu starea pe care o spun: eticheta locale-i numește
        starea ADEVĂRATĂ („Fără parfum"), un alias spune starea din valoarea lui. Date de pachet
        per locale (P11)."""
        base = (self.locale or "").split("-")[0].lower()
        labels = getattr(facet, "labels", None) or {}
        out: list[tuple[tuple[str, ...], bool]] = []
        label = labels.get(base) if base else None
        if label and tokens(label):
            out.append((tuple(tokens(label)), True))
        for alias, value in (getattr(facet, "aliases", None) or {}).items():
            state = _FLAGS.get(fold(str(value)).strip())
            if state is not None and tokens(alias):
                out.append((tuple(tokens(alias)), state))
        # cele mai lungi întâi: «fara parfum adaugat» înaintea lui «fara parfum»
        out.sort(key=lambda pv: -len(pv[0]))
        return out

    def _flag_value(self, facet: object, value: str) -> bool | None:
        """Starea pe care o scrie modelul: `true`/`false`, sau chiar o frază a pachetului care
        numește fațeta (starea frazei). Altceva ⇒ `None` (respins de apelant)."""
        folded = fold(value).strip()
        if folded in _FLAGS:
            return _FLAGS[folded]
        words = tuple(tokens(value))
        return next((state for phrase, state in self._flag_phrases(facet) if phrase == words), None)

    def _flag(
        self,
        change: StateChange,
        dimension: str,
        facet: object,
        flag: bool,
        evidence: _Evidence,
    ) -> CheckedChange:
        """NX-349: o fațetă da/nu nu are intrare de vocabular (catalogul nu indexează booleeni),
        deci se judecă pe frazele PACHETULUI care o numesc. Starea SPUSĂ = starea frazei, întoarsă
        de o negație chiar înaintea ei («nu fara parfum» = `false`); aceeași cu valoarea ⇒
        `explicit`, alta ⇒ `semantic_mismatch`; fără frază ⇒ `implicit` («să nu conțină parfum» e
        descrierea, nu numele fațetei). Valoarea canonică e `true`/`false` (ca `format_value`).

        O negație care e chiar primul cuvânt al frazei nu o întoarce pe următoarea («fara alcool
        fara parfum» e o enumerare). `matched` = fraza (cu negația care a întors-o): plannerul o
        scoate din textul căutării, fiindcă «parfum» ar urca exact produsele parfumate (NX-352 duce
        altfel cuvintele unei valori spuse, dar nefiltrate, în text)."""
        located: Provenance = "explicit" if evidence.located else "inferred"
        canonical = format_flag(flag)

        def reject(reason: ChangeReject) -> CheckedChange:
            return CheckedChange(
                change=change,
                dimension=dimension,
                canonical_value=canonical,
                provenance=located,
                strength="ranking",
                rejected=reason,
            )

        if (change.relation or "eq") not in _FLAG_RELATIONS:
            return reject("polarity_conflict")
        level: Provenance = located
        matched: tuple[str, ...] = ()
        if located == "explicit":
            level = "implicit"
            words = evidence.words
            for phrase, state in self._flag_phrases(facet):
                at = _find(words, phrase)
                if at < 0:
                    continue
                window = range(max(0, at - _NEGATION_WINDOW), at)
                flips = [p for p in evidence.negations if p in window and words[p] != phrase[0]]
                if (state != bool(flips)) != flag:
                    return reject("semantic_mismatch")
                level = "explicit"
                matched = tuple(words[min([at, *flips]) : at + len(phrase)])
                break
        checked = self._finish(change, dimension, canonical, level)
        return checked.model_copy(update={"matched": matched}) if matched else checked

    def _resolve_quote(
        self, evidence: _Evidence, dimension: str, canonical: str | float | None
    ) -> tuple[Provenance, int, bool, int]:
        """Pasul 2. (nivel, poziția valorii în citat, e contrazisă?, câte cuvinte o numesc)."""
        if dimension == UNMAPPED or canonical is None or self.vocab is None:
            return "implicit", -1, False, 0
        other = False
        for start, size, phrase in self._ngrams(evidence.words):
            on = self._resolve_on(phrase, dimension)
            if on is not None and on.key == canonical:
                return "explicit", start, False, size
        spelled = self._spelled_name(evidence.words, dimension, canonical)
        if spelled is not None:
            return "explicit", spelled[0], False, spelled[1]
        for _start, _size, phrase in self._ngrams(evidence.words):
            anywhere = self._resolve_anywhere(phrase)
            if anywhere is None:
                continue
            # Un RAFT nu concurează cu o valoare de fațetă: e subiectul, nu o proprietate. Altfel
            # «se usucă după duș» ar fi „contrazis" de un raft „Duș".
            if anywhere.dimension == CATEGORY_DIMENSION and dimension != CATEGORY_DIMENSION:
                continue
            if (anywhere.dimension, anywhere.key) != (dimension, canonical):
                other = True
        return "implicit", -1, other, 0

    def _spelled_name(
        self, words: Sequence[str], dimension: str, canonical: str | float | None
    ) -> tuple[int, int] | None:
        """`kernel.v6.0` (NX-364): citatul numește valoarea PROPUSĂ printr-unul din numele ei
        cunoscute (cheia și frazele overlay-ului care duc la ea), flexionat. (poziție, lungime).

        Potrivirea tare a vocabularului e o căutare exactă de dicționar, deci «am tenul uscat» nu
        confirma `skin_type=dry` („ten uscat” e fraza pachetului): pe setul real A, toate cele trei
        `skin_type` coborâte la `implicit` aveau valoarea corectă, iar planul le trata ca simple
        preferințe. Aici numele valorii se caută cuvânt cu cuvânt, în ordine, cu tulpina și sufixele
        locale-i (`_spells`, aceeași regulă ca umbrela NX-350; tabelul e al locale-i, P11).

        Confirmă DOAR valoarea propusă, cu un nume pe care pachetul i-l dă deja: nu adaugă nicio
        frază și nu schimbă ce e contrazis (a doua trecere din `_resolve_quote` rămâne exactă).
        Recenzia v6.0: o contradicție FLEXIONATĂ ar putea șterge o nevoie spusă («pielea mi se pare
        uscată» ar fi numit „par uscat”), deci flexiunea doar confirmă, niciodată nu contrazice.
        O descriere («mi se usucă pielea») nu conține niciun nume al valorii, deci rămâne
        `implicit`: pe același set, `implicit` are precizie 46%, iar verificarea pe vocabular e
        exact ce le separă."""
        if not isinstance(canonical, str) or self.vocab is None or not words:
            return None
        vocab_dim = CATEGORY_DIMENSION if dimension == CATEGORY_DIMENSION else dimension
        overlay = self.overlays.get(vocab_dim) or {}
        names = {canonical, *(phrase for phrase, key in overlay.items() if key == canonical)}
        suffixes = inflection_suffixes(self.locale)
        best: tuple[int, int] | None = None
        for name in names:
            code = tuple(tokens(name))
            size = len(code)
            if not size or size > len(words):
                continue
            for i in range(len(words) - size + 1):
                window = words[i : i + size]
                if not all(_same_stem(window[j], code[j], suffixes) for j in range(size)):
                    continue
                # Recenzia v6.0: o tulpină scurtă se potrivește și cu verbe («pare» = „par” + „e”),
                # deci «mi se pare uscată» ar fi „numit” `par uscat`. Flexiunea e acceptată doar
                # lângă un cuvânt IDENTIC al numelui («tenul USCAT»); un nume de un cuvânt rămâne
                # pe potrivirea exactă, ca pe v5.1.
                if not any(window[j] == code[j] for j in range(size)):
                    continue
                if best is None or size > best[1]:
                    best = (i, size)
                break
        return best

    def _umbrella(self, evidence: _Evidence, canonical: str | float | None) -> tuple[str, ...]:
        """NX-350: UMBRELA unui tip spus vag: codurile de tip care poartă cuvântul-tip spus de
        client («cremă» → toate cremele; «creme de față» → doar „crema de fata").

        Recenzia NX-350 (constatarea 1): un cuvânt-tip e doar unul care numește CE ESTE un cod,
        adică CAPUL lui (primul cuvânt plin: „crema" din „crema de fata"), cu flexiune (aceeași
        tulpină + un sufix al locale-i). Un cuvânt din coada codului descrie PENTRU CE e («ten»
        din „fond de ten", «par» din „crema de par", «fata» din „fata de" = „față de"), deci nu
        deschide o umbrelă.

        Umbrela conține MEREU codul presupus de model (primul), altfel ar putea spune altceva
        decât interpretarea validată: capul e al codului presupus. Fără el în citat, umbrela e doar
        codul presupus. Plafon `MAX_TYPE_UMBRELLA`, cele mai mari coduri întâi, determinist."""
        own = (str(canonical),) if isinstance(canonical, str) else ()
        entries = self.vocab.entries(SUBJECT_TYPE) if self.vocab is not None else ()
        suffixes = inflection_suffixes(self.locale)
        codes = [
            (e.key, [w for w in tokens(e.key) if w not in self.stop], e.count) for e in entries
        ]
        mine = next((words for key, words, _n in codes if key == canonical and words), None)
        if mine is None:
            return own
        head = [w for w in evidence.words if _same_stem(w, mine[0], suffixes)]
        if not head:
            return own
        # A doua recenzie (constatarea 4): umbrela se îngustează la codul presupus DOAR când
        # citatul îl spune ÎNTREG, în șir, de la cap («creme de fata»); un cuvânt din coada codului
        # aflat oriunde altundeva («o cremă mai ieftină față de cealaltă», «mi se pare») nu spune
        # ce fel de cremă.
        if _spells(evidence.words, tuple(tokens(str(canonical))), suffixes):
            return own
        hits = [
            (key, count)
            for key, words, count in codes
            if key != canonical and words and _same_stem(head[0], words[0], suffixes)
        ]
        hits.sort(key=lambda kc: (-kc[1], kc[0]))
        return (*own, *(k for k, _n in hits[: MAX_TYPE_UMBRELLA - len(own)]))

    def _polarity_level(
        self,
        level: Provenance,
        relation: str,
        evidence: _Evidence,
        dimension: str,
        number_in_quote: bool,
    ) -> Provenance:
        """Pasul 3: un marker lipsă retrogradează `explicit` la `implicit`."""
        if level != "explicit":
            return level
        if relation == "avoid" and not evidence.negations:
            return "implicit"
        if relation in ("lte", "gte") and relation not in evidence.comparator_ops:
            bare_price = (
                relation == "lte"
                and dimension == PRICE
                and number_in_quote
                and "gte" not in evidence.comparator_ops
            )
            if not bare_price:
                return "implicit"
        return level

    def _number(self, change: StateChange, dimension: str) -> float | None:
        """I9: un număr devine valoare a dimensiunii D doar dacă unitatea lui aparține lui D, sau
        n-are unitate și D e prețul. Întoarce valoarea în unitatea canonică, sau None (respins)."""
        unit = fold(change.unit or "").strip() or None
        try:
            value = Decimal(str(change.number))
        except (InvalidOperation, ValueError):
            return None
        if unit is None:
            return float(value) if dimension == PRICE else None
        if self.units.facet_for_unit(unit) != dimension:
            return None
        spec = self.units.specs.get(dimension)
        converted = spec.to_canonical(value, unit) if spec is not None else None
        return float(converted) if converted is not None else None

    def _finish(
        self,
        change: StateChange,
        dimension: str,
        canonical: str | float | None,
        level: Provenance,
    ) -> CheckedChange:
        if level == "inferred":
            strength = "ranking"
        elif level == "explicit" and dimension != UNMAPPED and hard_capable(dimension, self.pack):
            strength = "hard"
        else:
            strength = "soft"
        return CheckedChange(
            change=change,
            dimension=dimension,
            canonical_value=canonical,
            provenance=level,
            strength=strength,
            rejected=None,
        )


def format_flag(flag: bool) -> str:
    """Valoarea canonică a unei fațete da/nu. Aceeași formă ca `interpretation_check.format_value`
    (vederea modelului), fără import invers (modulul ăsta e sub validare)."""
    return next(text for text, state in _FLAGS.items() if state is flag)


def _bounds_of(change: CheckedChange) -> tuple[float | None, float | None]:
    """(limita de jos, limita de sus) pe care o pune o schimbare numerică. NX-334: `eq` e ambele
    („256 GB" = exact 256), iar pe preț e plafon (regula contractului: un număr de preț fără
    comparator e `lte`), același tabel ca `delta._BOUND_KEYS`."""
    value = change.canonical_value
    if not isinstance(value, float) or change.change.relative_to:
        return None, None
    relation = change.change.relation
    if relation == "lte" or (relation == "eq" and change.dimension == PRICE):
        return None, value
    if relation == "gte":
        return value, None
    if relation == "eq":
        return value, value
    return None, None


def _cross_hard_conflicts(checked: list[CheckedChange]) -> list[CheckedChange]:
    """Limite care se încrucișează în ACELAȘI tur („sub 100, minim 150"; „256 GB, dar maxim 128")
    ⇒ ambele `hard_conflict`, iar poarta de ambiguitate (pasul 4) întreabă. O limită dintr-un tur
    ANTERIOR nu e treaba asta: acolo cea nouă câștigă (reducerul, `bound_crossed`)."""
    out = list(checked)
    bounds = [_bounds_of(c) for c in out]
    for i, first in enumerate(out):
        for j, second in enumerate(out):
            if i == j or first.rejected or second.rejected:
                continue
            if first.dimension != second.dimension:
                continue
            low, high = bounds[i][0], bounds[j][1]
            if low is not None and high is not None and low > high:
                out[i] = first.model_copy(
                    update={"rejected": "hard_conflict", "strength": "ranking"}
                )
                out[j] = second.model_copy(
                    update={"rejected": "hard_conflict", "strength": "ranking"}
                )
    return out


def check_changes(
    interp: TurnInterpretation,
    *,
    words: UserWords,
    handles: Sequence[Handle] = (),
    vocab: CatalogVocabulary | None = None,
    units: UnitRegistry | None = None,
    pack: object | None = None,
    locale: str | None = None,
) -> list[CheckedChange]:
    """Un `CheckedChange` per `StateChange`, în ordinea modelului. PUR și determinist.

    Nimic nu se aruncă: o schimbare invalidă iese cu `rejected` din vocabularul închis, ca să
    apară în trace și în contoare. Tăria `hard` apare doar pe `explicit` + dimensiune hard-capable
    (I7)."""
    checker = _Checker(
        user=words,
        handles=handles,
        vocab=vocab,
        units=units if units is not None else getattr(pack, "units", None) or EMPTY_UNITS,
        pack=pack,
        locale=locale,
        declared_refs=[r.id for r in interp.references[:MAX_REFERENCES]],
    )
    checked = [checker.check(i, c) for i, c in enumerate(interp.changes)]
    return _cross_hard_conflicts(checked)


__all__ = [
    "MAX_ACTS",
    "MAX_CHANGES",
    "MAX_REFERENCES",
    "PRICE",
    "PRICE_KEYS",
    "UNMAPPED",
    "Handle",
    "UserWords",
    "check_changes",
    "check_targets",
    "dimension_of_key",
    "hard_capable",
    "need_handles",
    "tenant_dimensions",
]
