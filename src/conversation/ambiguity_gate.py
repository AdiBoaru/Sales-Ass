"""Kernel `kernel.v1.0`, pasul 4a (NX-332) — poarta de ambiguitate. PURĂ.

Contractul (§„Ownership", rândul „Ambiguity candidates": modelul *proposes*, codul *owns final
verdict*): între reducer și planner există UN loc care decide dacă turul poate acționa sau trebuie
să întrebe. Aici se decide. Patru verdicte (`AmbiguityDecision.verdict`):

- `act` — turul merge mai departe (eventual cu o confirmare ca ultimă frază);
- `resolve_from_context` — lecturile modelului duc la aceeași valoare, deci nu e nimic de întrebat;
- `act_both` — o citire ieftină pe două-trei produse: răspunsul despre toate bate o întrebare;
- `must_ask` — o mutație fără țintă `exact` (I10), limite care se contrazic, un `find` fără subiect,
  lecturi diferite cu câștig informațional mare.

Regulile sunt o TABELĂ ORDONATĂ, evaluată o singură dată (P1): prima care se aplică decide.
Anti-bucla (I11) stă peste toate și vine din `decide_clarification` (NX-235), refolosită, nu
rescrisă: aceeași cheie nu se întreabă de mai mult de `max_attempts_per_key` ori, iar o întrebare în
așteptare blochează alta nouă.

Întrebarea e scrisă DOAR din șabloanele pachetului (`DomainPack.clarify_templates[locale][kind]`),
cu `{options}` completat din etichete canonice (vocabular, pachet) sau din numele de afișare ale
produselor RECITITE din catalog. Textul modelului (`Ambiguity.readings`) și citatele clientului
(`StateChange.quote`) nu ajung niciodată într-o întrebare (contractul, runda 3, punctul 20).

Poarta nu scrie stare (I3): cheia întrebării pleacă în `GateOutcome.asked_key`, iar pasul 6 o
persistă printr-o a doua trecere a reducerului (`set_pending_question` / `note_asked`).

Ce NU face: nu cheamă modelul (I13), nu alege executorul (plannerul, NX-333), nu ramifică pe textul
brut (lecturile se consumă o dată, în `_read_readings`, prin vocabular), nu conține literali de
vertical (I14)."""

from __future__ import annotations

import hashlib
import string
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from dataclasses import replace as _replace
from typing import Literal, NamedTuple

from src.catalog.render_text import display_name
from src.catalog.vocabulary import (
    CATEGORY_DIMENSION,
    CatalogVocabulary,
    ResolutionStatus,
    facet_overlays,
    resolve_any,
)
from src.conversation.answer_policy import read_query
from src.conversation.clarification_policy import (
    NARROWING_MAX_VALUES,
    ClarificationCandidate,
    ClarificationPolicy,
    ClarificationReason,
    decide_clarification,
    estimate_information_gain,
)
from src.conversation.interpretation import (
    Act,
    AmbiguityDecision,
    CheckedChange,
    ResolvedRef,
    TurnInterpretation,
)
from src.conversation.needs import NeedVocabulary
from src.conversation.references import (
    BRAND_DIMENSION,
    MUTATING_ACTS,
    ProductFacts,
    ReferenceFacts,
    find_name_reference,
    gate_act_targets,
)
from src.conversation.routine_family import (
    RESUME_BUNDLE,
    WILDCARD,
    family_of_shelf,
    routine_family,
    routine_family_options,
)
from src.conversation.state_reducer import StateUpdateProposal
from src.conversation.state_v2 import MAX_DISPLAYED, ConversationStateV2
from src.domain.pack import DEFAULT_REFERENCE_DIMENSIONS
from src.web.localization import currency_word, format_amount

Verdict = Literal["act", "resolve_from_context", "act_both", "must_ask"]

#: Vocabularul ÎNCHIS al lui `AmbiguityDecision.reason` (și al evenimentului
#: `ambiguity_decision{verdict, reason}` de la pasul 6). Un test cere ca orice motiv emis să fie
#: aici.
GATE_REASONS: tuple[str, ...] = (
    # regula 9 și notele care o înlocuiesc
    "clear",
    "invalid_target",
    "vocabulary_unavailable",
    "readings_unresolved",
    # mutația (I10)
    "mutation_not_exact",
    "mutation_unavailable",
    # limitele încrucișate
    "hard_conflict",
    # subiectul
    "no_subject",
    "no_subject_low_gain",
    # NX-389 (`kernel.v9.0`): familia unei rutini fără familie
    "no_family",
    "family_readings",
    "family_single",
    "family_unservable",
    # lecturile
    "same_reading",
    "readings_differ",
    "readings_low_gain",
    # țintele ambigue la citire
    "ambiguous_read",
    "ambiguous_too_many",
    # confirmarea unei nevoi implicite
    "confirm_implicit",
    # anti-bucla și degradarea (P6)
    "already_asked",
    "already_pending",
    "already_known",
    "no_template",
    "no_options",
)

#: Cheile șabloanelor de clarificare. `reference` / `scope` / `value` sunt valorile lui
#: `Ambiguity.about`; restul sunt chei de DATE ale pachetului, nu valori în schema modelului.
TEMPLATE_KINDS: tuple[str, ...] = (
    "reference",
    "scope",
    "value",
    "subject",
    "family",
    "confirm",
    "conflict",
    "generic",
)
#: Etichetele de relație ale limitelor din întrebarea `conflict` („sub {value}", „minim {value}").
BOUND_KINDS: tuple[str, ...] = ("bound_lte", "bound_gte")
GENERIC = "generic"
OPTIONS_MARKER = "options"
VALUE_MARKER = "value"

#: Actele de CITIRE pe care poarta le lasă să răspundă despre mai mulți candidați.
READ_ACTS: frozenset[str] = frozenset({"detail", "link", "compare"})
#: Peste atâția candidați o citire „despre toate" nu mai e un răspuns, e un catalog. Pragul
#: plannerului pentru un `find` care numește un produs (NX-375); poarta folosește `MAX_READ_ALL`.
MAX_ACT_BOTH = 3
#: Câte opțiuni încap într-o întrebare: aceeași limită ca întrebarea de îngustare (NX-315).
MAX_OPTIONS = NARROWING_MAX_VALUES
#: NX-385: pe câți candidați răspunde o citire „despre toți" (`act_both`). Cât arată executorul:
#: comparația ține patru coloane (`compose.build_comparison`), iar peste ele un răspuns „despre
#: toți" ar tăia tăcut restul. Un test leagă cele două cifre.
MAX_READ_ALL = 4
#: NX-385: câte produse oferă o întrebare „la care te referi". Tot ce poate fi pe ecran
#: (`MAX_DISPLAYED`): o listă tăiată la primele patru lăsa pe dinafară exact produsul la care se
#: gândea clientul (`w1_masca_noapte_utilizare#2`, cinci produse, întrebarea numea patru). Peste
#: plafon întrebarea se pune pe o dimensiune care îi desparte pe toți, nu pe o parte din nume.
MAX_REFERENCE_OPTIONS = MAX_DISPLAYED
#: Prefixul cheii unei întrebări despre o ȚINTĂ (`target_question_key`).
REFERENCE_KEY_PREFIX = "ref:"
#: NX-389: cheia întrebării de familie a unei rutini (memoria ei, anti-bucla I11).
ROUTINE_FAMILY_KEY = "routine_family"

PRICE = "price"
BRAND = BRAND_DIMENSION
#: NX-385: câte cuvinte din coada numelui întreg intră în eticheta care deosebește doi candidați cu
#: același nume de afișare (nuanța, gramajul: „17N Vanilla", „150 ml").
_TAIL_WORDS = 3
#: Sursele unei nevoi care NU sunt cunoaștere: o ipoteză (`implicit`) sau o inferență a modelului.
#: Pentru poarta `already_known` a lui `decide_clarification` nu contează ca „știm deja răspunsul":
#: tocmai pe ele le confirmăm.
_UNCONFIRMED_SOURCES: frozenset[str] = frozenset({"user_implicit", "model_inferred"})

#: Clasa unei reguli decide cum coboară verdictul când întrebarea nu se poate pune (anti-buclă,
#: șablon lipsă): o CITIRE răspunde despre toate (`act_both`), o MUTAȚIE sau o regulă care blochează
#: acțiunea rămâne `must_ask` fără întrebare, iar plannerul servește răspunsul determinist. O
#: CONFIRMARE (regula 8) acționează oricum: întrebarea era doar ultima frază.
_Class = Literal["read", "blocking", "confirm"]
_FALLBACK_VERDICT: Mapping[str, Verdict] = {
    "read": "act_both",
    "blocking": "must_ask",
    "confirm": "act",
}


class GateOutcome(NamedTuple):
    """Ieșirea porții. `decision` e forma contractului, neschimbată; restul fixează cine persistă
    memoria întrebării, ca pasul 6 să nu inventeze altceva:

    - `asked_kind="pending"`: întrebarea ține locul răspunsului ⇒ `set_pending_question(asked_key)`;
    - `asked_kind="noted"`: confirmarea e ultima frază a răspunsului ⇒ `note_asked(asked_key)`;
    - `skipped_acts`: indexurile actelor cu țintă invalidă (regula 0), pe care plannerul nu le
      planifică."""

    decision: AmbiguityDecision
    asked_key: str | None = None
    asked_kind: Literal["pending", "noted"] | None = None
    skipped_acts: tuple[int, ...] = ()
    #: NX-389: familia unei rutini fără familie, decisă de poartă fără întrebare: singura familie
    #: servibilă, sau cea majoritară după ce întrebarea a fost deja pusă (`family_defaulted`).
    routine_family: str | None = None
    #: NX-389: etichetele familiilor servibile, cea aleasă întâi (pentru dezvăluire și întrebare).
    family_labels: tuple[str, ...] = ()
    #: NX-389b: cheile rafturilor opțiunilor întrebării de familie, în aceeași ordine ca etichetele;
    #: memoria întrebării le ține (`options_refs`), iar răspunsul se recunoaște pe ele.
    family_options: tuple[str, ...] = ()


def target_question_key(product_ids: Sequence[str]) -> str:
    """Cheia unei întrebări despre o țintă: `ref:` + primele 8 caractere din sha1 peste id-urile
    candidaților, SORTATE. Nu `ref:<ref_id>`: `r1` e local turului, deci aceeași întrebare ar primi
    altă cheie la turul următor, iar anti-bucla n-ar vedea-o."""
    digest = hashlib.sha1(",".join(sorted(set(product_ids))).encode("utf-8")).hexdigest()
    return f"{REFERENCE_KEY_PREFIX}{digest[:8]}"


# --- șabloanele ----------------------------------------------------------------------------------


def template_markers(phrase: str) -> tuple[str, ...] | None:
    """Marcatorii unui șablon (`{options}` ⇒ `("options",)`), sau None dacă nu se poate formata.
    Loaderul aplică aceeași regulă la load (fără să importe kernelul), iar
    `tests/test_clarify_templates.py` cere ca cele două să dea același verdict."""
    try:
        return tuple(
            field for _, field, _, _ in string.Formatter().parse(phrase) if field is not None
        )
    except ValueError:
        return None


def valid_template(kind: str, phrase: str) -> bool:
    """Exact UN marcator, cel cerut de fel: `{value}` pe etichetele de limită, `{options}` în
    rest."""
    expected = VALUE_MARKER if kind in BOUND_KINDS else OPTIONS_MARKER
    return template_markers(phrase) == (expected,)


def _templates(pack: object | None, locale: str) -> Mapping[str, str]:
    table = getattr(pack, "clarify_templates", None)
    if not isinstance(table, Mapping):
        return {}
    lang = (locale or "").strip().lower()
    per_locale = table.get(lang) or table.get(lang.split("-")[0])
    return per_locale if isinstance(per_locale, Mapping) else {}


def _glue(pack: object | None, locale: str) -> str:
    """Legătura dinaintea ultimei opțiuni, din `answer_shape_templates["list_glue"]` (NX-299).
    Lipsă ⇒ virgulă: o enumerare fără „și" rămâne corectă, doar mai seacă (fail-open)."""
    table = getattr(pack, "answer_shape_templates", None)
    per_key = table.get("list_glue") if isinstance(table, Mapping) else None
    if not isinstance(per_key, Mapping):
        return ", "
    lang = (locale or "").strip().lower()
    value = per_key.get(lang) or per_key.get(lang.split("-")[0])
    return value if isinstance(value, str) and value else ", "


def _enumerate(labels: Sequence[str], glue: str) -> str:
    """«a, b și c»: aceeași regulă ca încadrarea serverului (`answer_shape._enumerate`), cu legătura
    locale-i. Copiată în trei rânduri ca modulul pur să nu depindă de calea v1."""
    if len(labels) <= 1:
        return labels[0] if labels else ""
    return f"{', '.join(labels[:-1])}{glue}{labels[-1]}"


# --- lecturile modelului -------------------------------------------------------------------------


@dataclass(frozen=True)
class _Readings:
    """Ce spun lecturile unei ambiguități, după vocabular: valorile (dimensiune, cheie) în ordinea
    lecturilor, fără duplicate. Nicio lectură nu supraviețuiește ca text."""

    values: tuple[tuple[str, str], ...]


def _read_readings(
    readings: Sequence[str],
    vocab: CatalogVocabulary,
    overlays: Mapping[str, Mapping[str, str]] | None,
) -> _Readings:
    """SINGURUL loc care citește `Ambiguity.readings` (text de model). Fiecare lectură →
    `resolve_any` → (dimensiune, valoare) sau nimic; de aici încolo deciziile ramifică pe valori
    canonice, niciodată pe text (tiparul lui `provenance._read_quote`). O lectură care nu se rezolvă
    nu contează ca valoare diferită."""
    values: list[tuple[str, str]] = []
    for reading in readings:
        res = resolve_any(vocab, reading, overlays=overlays)
        if res.status is ResolutionStatus.KNOWN and res.key:
            pair = (res.dimension, res.key)
            if pair not in values:
                values.append(pair)
    return _Readings(tuple(values))


# --- poarta --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Ask:
    """O întrebare POSIBILĂ, înainte de anti-buclă și de șablon."""

    kind: str
    key: str
    labels: tuple[str, ...]
    reason: str  # motivul verdictului când întrebarea se pune
    candidate_reason: ClarificationReason
    partition: tuple[int, ...]
    total: int | None
    klass: _Class
    min_options: int = 2
    max_options: int = MAX_OPTIONS

    @property
    def asks_verdict(self) -> Verdict:
        """Verdictul când întrebarea SE pune: o confirmare acționează și întreabă la final, restul
        țin locul răspunsului."""
        return "act" if self.klass == "confirm" else "must_ask"

    @property
    def asked_kind(self) -> Literal["pending", "noted"]:
        return "noted" if self.klass == "confirm" else "pending"


class _Gate:
    def __init__(
        self,
        interp: TurnInterpretation,
        checked: Sequence[CheckedChange],
        resolved: Sequence[ResolvedRef],
        state: ConversationStateV2,
        facts: ReferenceFacts,
        *,
        vocab: CatalogVocabulary | None,
        pack: object | None,
        locale: str,
        policy: ClarificationPolicy,
    ) -> None:
        self.interp = interp
        self.checked = checked
        self.resolved = {r.ref_id: r for r in resolved}
        self.state = state
        self.facts = facts
        self.vocab = vocab if vocab is not None and not vocab.is_empty() else None
        self.pack = pack
        self.locale = locale
        self.policy = policy
        self.templates = _templates(pack, locale)
        self.glue = _glue(pack, locale)
        self.overlays = facet_overlays(pack, self.vocab.facet_names) if self.vocab else None
        self.notes: list[str] = []
        #: NX-389: familia aleasă fără întrebare (`GateOutcome.routine_family`) și etichetele ei
        self.family_choice: str | None = None
        self.family_labels: tuple[str, ...] = ()
        self.family_options: tuple[str, ...] = ()

    # --- utilitare -------------------------------------------------------------------------------

    def _note(self, reason: str) -> None:
        if reason not in self.notes:
            self.notes.append(reason)

    def _screen(self) -> tuple[ProductFacts, ...]:
        """Setul de pe ecran ÎNAINTEA turului, cu faptele recitite în tur. Un produs pe care
        catalogul nu l-a mai întors nu există pentru poartă (I1)."""
        return tuple(
            self.facts.products[d.product_id]
            for d in self.state.references.displayed_products
            if d.product_id in self.facts.products
        )

    def _names(self, product_ids: Sequence[str]) -> tuple[str, ...]:
        """Eticheta fiecărui candidat, fără repetiții. Numele de afișare întâi; pe SOLE există
        produse cu același nume de afișare (familii de nuanțe și gramaje: „TIRTIR Mask Fit Red
        Cushion" de două ori), iar „La care te referi: X și X?" nu e o întrebare. NX-385
        (`w4_mixt_pasta_original#3`, `w5_english_sunscreen#4`): candidații cu același nume de
        afișare primesc în paranteză faptul RECITIT care îi deosebește (`_distinguishers`, în
        ordine, până devin distincte). Doi candidați pe care nimic citit nu-i deosebește au o
        singură etichetă; sub două etichete, `_ask` coboară pe `no_options` (o mutație rămâne
        oprită: faptele citite în tur nu sunt toate faptele produsului, deci nu se alege unul)."""
        return tuple(dict.fromkeys(self._label_map(product_ids).values()))

    def _label_map(self, product_ids: Sequence[str]) -> dict[str, str]:
        """Eticheta fiecărui candidat RECITIT (`_names`), pe id, în ordinea candidaților."""
        known = [
            self.facts.products[pid]
            for pid in dict.fromkeys(product_ids)
            if pid in self.facts.products
        ]
        head = {f.product_id: display_name(f.name) or f.product_id for f in known}
        labels = dict(head)
        groups: dict[str, list[ProductFacts]] = {}
        for f in known:
            groups.setdefault(head[f.product_id], []).append(f)
        for name, group in groups.items():
            if len(group) < 2:
                continue
            parts: dict[str, list[str]] = {f.product_id: [] for f in group}
            for describe in self._distinguishers(group):
                values = {f.product_id: describe(f) for f in group}
                if len(set(values.values())) < 2:
                    continue  # la fel pe toți: nu deosebește nimic
                for pid, value in values.items():
                    if value:
                        parts[pid].append(value)
                if len({tuple(p) for p in parts.values()}) == len(group):
                    break
            for f in group:
                if parts[f.product_id]:
                    labels[f.product_id] = f"{name} ({', '.join(parts[f.product_id])})"
        return labels

    def _distinguishers(
        self, group: Sequence[ProductFacts]
    ) -> list[Callable[[ProductFacts], str | None]]:
        """Faptele care pot deosebi produse cu același nume de afișare, în ordinea în care le-ar
        citi clientul pe card: varianta, valorile dimensiunilor de referință ale pachetului
        (tipul), prețul, apoi cuvintele din numele ÎNTREG pe care nu le au ceilalți (nuanța,
        gramajul, pe care numele de afișare le taie). Toate vin din catalogul recitit în tur."""

        def variant(f: ProductFacts) -> str | None:
            return " / ".join(v for v in f.variant_labels if v) or None

        def dimension(dim: str) -> Callable[[ProductFacts], str | None]:
            source = self._source_key(dim)

            def read(f: ProductFacts) -> str | None:
                raw = f.brand if dim == BRAND else f.attributes.get(source)
                items = raw if isinstance(raw, (list, tuple)) else [raw]
                values = [self._value_label(dim, v) for v in items if isinstance(v, str) and v]
                return " / ".join(values) or None

            return read

        def price(f: ProductFacts) -> str | None:
            return self._quantity(PRICE, float(f.price)) if f.price is not None else None

        def rating(f: ProductFacts) -> str | None:
            # Ca pe card: steaua și nota cu o zecimală, în scrierea locale-i.
            # Rotunjit întâi la o zecimală, ca pe card (4,96 ⇒ 5,0, nu 4,9).
            value = round(float(f.rating), 1) if f.rating is not None else None
            amount = format_amount(value, self.locale) if value is not None else None
            return f"⭐{amount[:-1]}" if amount else None

        words = {
            f.product_id: [w for w in f.name.split() if any(c.isalnum() for c in w)] for f in group
        }
        common = set.intersection(*({w.casefold() for w in ws} for ws in words.values()))

        def tail(f: ProductFacts) -> str | None:
            own = [w for w in words[f.product_id] if w.casefold() not in common]
            return " ".join(own[:_TAIL_WORDS]) or None

        return [variant, *(dimension(d) for d in self._reference_dims()), price, rating, tail]

    def _reference_dims(self) -> tuple[str, ...]:
        dims = getattr(self.pack, "reference_dimensions", None)
        return tuple(dims if dims is not None else DEFAULT_REFERENCE_DIMENSIONS)

    def _dimension_options(
        self, product_ids: Sequence[str]
    ) -> tuple[tuple[str, ...], tuple[int, ...]]:
        """NX-385: peste `MAX_REFERENCE_OPTIONS` candidați întrebarea nu-i mai poate numi pe toți,
        iar o listă tăiată poate lăsa pe dinafară produsul la care se gândea clientul. Întrebarea se
        pune atunci pe o dimensiune de referință a pachetului (marca, tipul) cunoscută pe TOȚI
        candidații, cu 2..`MAX_OPTIONS` valori, cea cu câștigul cel mai mare: fiecare candidat e
        acoperit de o opțiune. Fără una ⇒ `((), ())`, iar `_ask` coboară pe `no_options`."""
        known = [self.facts.products[p] for p in product_ids if p in self.facts.products]
        best: tuple[float, str, list[str], tuple[int, ...]] | None = None
        for dim in self._reference_dims():
            source = self._source_key(dim)
            values = [f.brand if dim == BRAND else f.attributes.get(source) for f in known]
            if not known or not all(isinstance(v, str) and v for v in values):
                continue
            counts = Counter(str(v) for v in values)
            if not 2 <= len(counts) <= MAX_OPTIONS:
                continue
            ordered = sorted(counts, key=lambda v: (-counts[v], v))
            partition = tuple(counts[v] for v in ordered)
            gain = estimate_information_gain(len(known), partition)
            if best is None or gain > best[0]:
                best = (gain, dim, ordered, partition)
        if best is None:
            return (), ()
        _, dim, ordered, partition = best
        labels = tuple(v if dim == BRAND else self._value_label(dim, v) for v in ordered)
        return labels, partition

    def _value_label(self, dimension: str, key: str) -> str:
        """Eticheta canonică a unei valori: a pachetului (localizată), apoi a vocabularului, apoi
        cheia. Niciodată textul modelului."""
        getter = getattr(self.pack, "value_label", None)
        label = getter(dimension, key, self.locale) if callable(getter) else None
        if label:
            return label
        if self.vocab is not None:
            for entry in self.vocab.entries(dimension):
                if entry.key == key:
                    return entry.label
        return key

    def _source_key(self, dimension: str) -> str:
        for facet in getattr(self.pack, "facets", ()) or ():
            if getattr(facet, "key", None) == dimension:
                return str(getattr(facet, "source_key", dimension) or dimension)
        return dimension

    def _partition(
        self, dimension: str, keys: Sequence[str] | None
    ) -> tuple[tuple[int, ...], int | None]:
        """Câte produse de pe ecran poartă fiecare valoare (`keys`, sau toate valorile găsite).
        Fără ecran ⇒ `total=None` („încă n-am căutat", semantica lui `decide_clarification`).

        La fel când dimensiunea nu se poate MĂSURA pe ecran: raftul (`ProductFacts` nu-l poartă) sau
        un atribut pe care citirea nu l-a cerut (`ReferenceFacts.attributes_read`). Un atribut
        necitit lipsește din fapte fără să fie absent de pe produs, iar numărat ar fi dat zero pe
        toate valorile, adică „câștig mic" exact acolo unde răspunsul ar tăia setul în două."""
        screen = self._screen()
        if not screen or dimension == CATEGORY_DIMENSION:
            return (), None
        source = self._source_key(dimension)
        read = self.facts.attributes_read
        if read is not None and source not in read:
            return (), None
        counts: dict[str, int] = {}
        for facts in screen:
            raw = facts.attributes.get(source)
            items = raw if isinstance(raw, (list, tuple)) else [raw]
            for value in items:
                if isinstance(value, str) and value:
                    counts[value] = counts.get(value, 0) + 1
        if keys is None:
            ordered = sorted(counts, key=lambda k: (-counts[k], k))
        else:
            ordered = list(keys)
        return tuple(counts.get(k, 0) for k in ordered), len(screen)

    # --- întrebarea ------------------------------------------------------------------------------

    def _ask(
        self, ask: _Ask, *, on_decline: Callable[[str], GateOutcome | None]
    ) -> GateOutcome | None:
        """Opțiunile, anti-bucla, apoi șablonul. `on_decline(motiv)` decide verdictul când
        `decide_clarification` nu lasă întrebarea (câștig mic, cheie deja întrebată, întrebare în
        așteptare, răspuns deja știut); None = regula nu se aplică, poarta trece mai departe."""
        labels = ask.labels[: ask.max_options]
        if len(labels) < ask.min_options:
            # Fără opțiuni nu există întrebare cu opțiuni: niciun `{options}` gol în text.
            return self._fallback(ask, "no_options")
        candidate = ClarificationCandidate(
            key=ask.key,
            reason=ask.candidate_reason,
            options_refs=labels,
            partition=ask.partition,
        )
        decision = decide_clarification(
            self.state, [candidate], total_candidates=ask.total, policy=self._policy_for(ask.key)
        )
        if not decision.ask and not self._hypothesis_only(ask, decision.reason):
            return on_decline(decision.reason)
        template = self.templates.get(ask.kind) or self.templates.get(GENERIC)
        if template is None or not valid_template(ask.kind, template):
            return self._fallback(ask, "no_template")
        question = template.format(**{OPTIONS_MARKER: _enumerate(labels, self.glue)})
        return GateOutcome(
            AmbiguityDecision(verdict=ask.asks_verdict, reason=ask.reason, question=question),
            asked_key=ask.key,
            asked_kind=ask.asked_kind,
        )

    def _policy_for(self, key: str) -> ClarificationPolicy:
        """NX-385 (I11, `w1_masca_noapte_utilizare#3`, `w4_mixt_cushion_livrare#3`): o întrebare
        despre o ȚINTĂ („la care te referi") se pune o singură dată pe aceeași mulțime de candidați
        (`ClarificationPolicy.max_attempts_per_target`). Un tur care nu răspunde la ea închide
        întrebarea (`question_answered`), iar a doua întrebare identică e exact bucla pe care
        clientul o resimte ca „nu ascultă". A doua oară o citire răspunde despre toți candidații,
        iar o mutație rămâne oprită fără o nouă întrebare (`already_asked`)."""
        if key == ROUTINE_FAMILY_KEY:
            # NX-389 (D2, Adi 2026-10-09): familia se întreabă O dată; a doua oară rutina se face pe
            # familia cu cele mai multe produse și i se spune clientului (`family_defaulted`)
            return _replace(self.policy, max_attempts_per_key=1)
        if not key.startswith(REFERENCE_KEY_PREFIX):
            return self.policy
        cap = min(self.policy.max_attempts_per_key, self.policy.max_attempts_per_target)
        return _replace(self.policy, max_attempts_per_key=cap)

    def _target_ask(
        self,
        product_ids: Sequence[str],
        *,
        reason: str,
        klass: _Class,
        on_decline: Callable[[str], GateOutcome | None],
    ) -> GateOutcome | None:
        """Întrebarea „la care te referi" pe TOȚI candidații (NX-385), cu etichete care îi deosebesc
        (`_names`). Peste `MAX_REFERENCE_OPTIONS` etichete, întrebarea se pune pe o dimensiune care
        îi acoperă pe toți (`_dimension_options`), niciodată pe primii N. Cheia rămâne a mulțimii de
        candidați, ca plannerul să pună exact candidații întrebării pe carduri."""
        ids = list(dict.fromkeys(product_ids))
        per_id = self._label_map(ids)
        counts = Counter(per_id.values())
        labels = tuple(dict.fromkeys(per_id.values()))
        partition = tuple(counts[label] for label in labels)
        if len(labels) > MAX_REFERENCE_OPTIONS:
            labels, partition = self._dimension_options(ids)
        return self._ask(
            _Ask(
                kind="reference",
                key=target_question_key(ids),
                labels=labels,
                reason=reason,
                candidate_reason="disambiguation",
                partition=partition,
                total=len(ids),
                klass=klass,
                max_options=MAX_REFERENCE_OPTIONS,
            ),
            on_decline=on_decline,
        )

    def _hypothesis_only(self, ask: _Ask, refused: str) -> bool:
        """O confirmare refuzată DOAR pentru că „știm deja" răspunsul, când ce știm e chiar ipoteza
        de confirmat (o nevoie `implicit`), trece. Porțile lui `decide_clarification` rulează în
        ordine (în așteptare → deja întrebat → deja știut), deci la `already_known` primele două au
        trecut; rămâne doar pragul de câștig, verificat aici cu aceeași funcție. Poarta nu
        construiește o stare modificată (I3): reinterpretează doar ultimul refuz."""
        if ask.klass != "confirm" or refused != "already_known":
            return False
        need = self.state.need_for(ask.key)
        if need is None or need.source not in _UNCONFIRMED_SOURCES:
            return False
        if ask.total is None:
            return False
        gain = estimate_information_gain(ask.total, ask.partition)
        return gain >= self.policy.min_information_gain

    def _fallback(self, ask: _Ask, reason: str) -> GateOutcome:
        """P6: o întrebare care nu se poate pune nu înseamnă tăcere. O citire răspunde despre toți
        candidații, o confirmare acționează fără ea, iar restul rămâne `must_ask` fără întrebare:
        plannerul servește răspunsul determinist de azi."""
        return self._decide(_FALLBACK_VERDICT[ask.klass], reason)

    def _decide(self, verdict: Verdict, reason: str) -> GateOutcome:
        return GateOutcome(AmbiguityDecision(verdict=verdict, reason=reason, question=None))

    def _declined(self, klass: _Class, low_gain: str) -> Callable[[str], GateOutcome]:
        """Verdictul când `decide_clarification` refuză întrebarea: câștig mic ⇒ `act` (motivul
        regulii); răspuns deja știut ⇒ `act`; cheie deja întrebată sau întrebare în așteptare ⇒ o
        citire răspunde despre toate, o mutație rămâne oprită (I10, I11)."""

        def decline(reason: str) -> GateOutcome:
            if reason == "low_gain":
                # O mutație nu coboară niciodată la `act` (I10), oricât de mic ar fi câștigul.
                verdict: Verdict = "must_ask" if klass == "blocking" else "act"
                return self._decide(verdict, low_gain)
            if reason == "already_known":
                return self._decide("act", reason)
            return self._decide(_FALLBACK_VERDICT[klass], reason)

        return decline

    # --- regulile, în ordine ---------------------------------------------------------------------

    def run(self) -> GateOutcome:
        checks = gate_act_targets(self.interp.acts, list(self.resolved.values()))
        # Regula 0: o țintă nedeclarată sau care numește o proprietate (I24) scoate ACTUL din plan,
        # nu turul. Restul actelor merg mai departe.
        invalid = {
            c.act_index
            for c in checks
            if c.verdict in ("unknown_reference", "invalid_reference_target")
        }
        skipped = tuple(sorted(invalid | self._out_of_range_reads()))
        acts = [(i, a) for i, a in enumerate(self.interp.acts) if i not in skipped]
        if skipped:
            self._note("invalid_target")
        outcome = self._rules(acts, [c for c in checks if c.act_index not in skipped])
        if outcome.decision.verdict != "must_ask" and self.family_choice is not None:
            outcome = outcome._replace(
                routine_family=self.family_choice, family_labels=self.family_labels
            )
        elif outcome.asked_key == ROUTINE_FAMILY_KEY:
            outcome = outcome._replace(
                family_labels=self.family_labels, family_options=self.family_options
            )
        return outcome._replace(skipped_acts=skipped)

    def _out_of_range_reads(self) -> set[int]:
        """NX-385 (`w2_tint_rosu_ordinal_paginare#2`): «al treilea» pe un ecran cu UN produs.
        Resolverul spune `ambiguous` cu motivul `ordinal_out_of_range` (contractul: un ordinal
        dincolo de set e ambiguu), cu tot setul drept candidați. Dar clientul n-a numit niciunul
        dintre ei: a numit o poziție care nu există. O citire „despre toți" ar fi răspunsul despre
        ALT produs (pe turul real, singurul de pe ecran). Actul de citire iese din plan ca o țintă
        invalidă (regula 0, dezvăluirea `invalid_target`); o mutație pe aceeași țintă rămâne a
        regulii 1 (nu e `exact`, deci întreabă sau se oprește, I10)."""
        out: set[int] = set()
        for index, act in enumerate(self.interp.acts):
            if act.kind not in READ_ACTS:
                continue
            for target in act.targets:
                ref = self.resolved.get(target)
                if ref is not None and ref.reason == "ordinal_out_of_range":
                    out.add(index)
        return out

    def _rules(self, acts: list[tuple[int, Act]], checks) -> GateOutcome:
        # Regula 2 nu depinde de acte: limitele care se contrazic sunt ale stării, deci se întreabă
        # și când nu rămâne niciun act (toate sărite de regula 0, sau un tur doar cu schimbări).
        rules: list[Callable[[], GateOutcome | None]] = [self._conflict]
        if acts:
            rules = [
                lambda: self._mutation(acts, checks),
                self._conflict,
                lambda: self._subject(acts[-1][1]),
                lambda: self._family(acts[-1][1]),
                self._readings,
                lambda: self._ambiguous_reads(acts),
                self._confirm,
            ]
        # Un verdict al lecturilor care NU întreabă (`same_reading`, câștig mic, deja întrebat)
        # spune doar că lecturile n-au nevoie de o întrebare; nu spune nimic despre ținte. Nu
        # închide deci evaluarea: regulile de după (ținte ambigue, confirmarea) decid primele, iar
        # el rămâne verdictul de rezervă. Altfel un `detail` pe două produse, lângă o lectură
        # rezolvată, ieșea `resolve_from_context`, iar plannerul n-avea țintă (`reply_only`), în
        # loc de `act_both`.
        fallback: GateOutcome | None = None
        for rule in rules:
            outcome = rule()
            if outcome is None:
                continue
            passive = outcome.asked_key is None and outcome.decision.verdict != "must_ask"
            if rule == self._readings and passive:
                fallback = outcome
                continue
            return outcome
        if fallback is not None:
            return fallback
        return self._decide("act", self.notes[0] if self.notes else "clear")

    def _mutation(self, acts: list[tuple[int, Act]], checks) -> GateOutcome | None:
        """Regula 1 (I10): pe ORICE act care mută (nu doar pe cel principal: în «adaugă-l în coș și
        arată-mi o husă» coșul e primul act, iar principalul e `find`), o țintă care nu e `exact`
        sau e `exact` dar epuizată oprește turul."""
        mutating = {i for i, a in acts if a.kind in MUTATING_ACTS}
        if any(not a.targets for i, a in acts if i in mutating):
            # Un coș fără țintă («pune-l în coș» fără o referință) nu produce nicio verificare în
            # `gate_act_targets`, deci ar fi trecut drept `clear` (I10 încălcat). Nu există o țintă
            # `exact`: întrebarea se pune pe ecran, sau coșul se oprește fără ea.
            ids = [f.product_id for f in self._screen()]
            return self._target_ask(
                ids,
                reason="mutation_not_exact",
                klass="blocking",
                on_decline=self._declined("blocking", "mutation_not_exact"),
            )
        for check in checks:
            if check.act_index not in mutating:
                continue
            ref = self.resolved.get(check.target)
            zoomed = self._zoomed_other(ref)
            if zoomed:
                # kernel.v6.0 (recenzia): un ordinal numărat pe lista „în care s-a intrat” e `exact`
                # pentru o citire, dar ecranul arată ALT produs. «Adaugă-l pe primul» pe un ecran de
                # detaliu poate numi cardul de pe ecran sau primul din listă: pe coș nu ghicim.
                return self._target_ask(
                    zoomed,
                    reason="mutation_not_exact",
                    klass="blocking",
                    on_decline=self._declined("blocking", "mutation_not_exact"),
                )
            if check.verdict == "not_exact":
                ids = [
                    pid for pid in (ref.product_ids if ref else []) if pid in self.facts.products
                ]
                if len(ids) < 2:
                    # Nicio pereche de candidați pe țintă (un nume negăsit, un produs dispărut din
                    # catalog): întrebarea „la care te referi" se pune pe ecran, dacă are ce oferi.
                    ids = [f.product_id for f in self._screen()]
                return self._target_ask(
                    ids,
                    reason="mutation_not_exact",
                    klass="blocking",
                    on_decline=self._declined("blocking", "mutation_not_exact"),
                )
            if ref is not None and ref.outcome == "exact" and ref.reason == "unavailable":
                # Un produs epuizat iese `exact` cu motiv, nu `stale` (NX-329). N-ai ce întreba
                # între UN produs; coșul nu se execută, iar răspunsul spune că e epuizat.
                return self._decide("must_ask", "mutation_unavailable")
        return None

    def _zoomed_other(self, ref: ResolvedRef | None) -> list[str]:
        """Candidații unei mutații pe un ordinal numărat pe listă (`ordinal_in_zoomed_list`):
        ținta din listă și produsul de pe ecran, când diferă. Gol = nimic de întrebat."""
        if ref is None or ref.reason != "ordinal_in_zoomed_list":
            return []
        screen = [f.product_id for f in self._screen()]
        ids = [*ref.product_ids, *(pid for pid in screen if pid not in ref.product_ids)]
        return ids[:MAX_OPTIONS] if len(ids) > 1 else []

    def _conflict(self) -> GateOutcome | None:
        """Regula 2: două limite `hard_conflict` în ACELAȘI tur („sub 100, minim 150"). Opțiunile
        sunt limitele, randate din valoarea CANONICĂ, niciodată din citat."""
        by_dim: dict[str, list[CheckedChange]] = {}
        for c in self.checked:
            if c.rejected == "hard_conflict":
                by_dim.setdefault(c.dimension, []).append(c)
        for dimension, changes in by_dim.items():
            if len(changes) < 2:
                continue
            ordered = sorted(changes, key=lambda c: (c.change.relation != "lte", c.canonical_value))
            labels = tuple(
                label for label in (self._bound(dimension, c) for c in ordered) if label is not None
            )
            if len(labels) < len(ordered):
                # Fără eticheta de relație întrebarea n-ar spune care limită e care.
                return self._decide("must_ask", "no_template")
            return self._ask(
                _Ask(
                    kind="conflict",
                    key=f"conflict:{dimension}",
                    labels=labels,
                    reason="hard_conflict",
                    candidate_reason="hard_conflict",
                    partition=(),
                    total=None,
                    klass="blocking",
                ),
                on_decline=self._declined("blocking", "hard_conflict"),
            )
        return None

    def _bound(self, dimension: str, change: CheckedChange) -> str | None:
        relation = change.change.relation
        template = self.templates.get(f"bound_{relation}")
        value = change.canonical_value
        if template is None or not valid_template(f"bound_{relation}", template):
            return None
        if not isinstance(value, (int, float)):
            return None
        text = self._quantity(dimension, float(value))
        if text is None:
            return None
        return template.format(**{VALUE_MARKER: text})

    def _quantity(self, dimension: str, value: float) -> str | None:
        """Valoarea unei limite, ca text al locale-ului (recenzia NX-332: `:g` dădea
        „1.23457e+06" și punct zecimal pe `ro`). Numărul trece prin localizarea canonică
        (`format_amount`); prețul primește cuvântul monedei tenantului, restul unitatea canonică a
        dimensiunii din `UnitRegistry`; fără zecimale când e întreg („sub 200 lei", „6,5 inch").
        Niciodată unitatea scrisă de model."""
        amount = format_amount(value, self.locale)
        if amount is None:
            return None
        # `format_amount` scrie mereu două zecimale; o limită întreagă se spune fără ele.
        number = amount[:-3] if float(value).is_integer() else amount.rstrip("0")
        if dimension == PRICE:
            word = currency_word(getattr(self.pack, "currency", None), self.locale)
            return f"{number} {word}" if word else None
        unit = self._unit(dimension)
        return f"{number} {unit}" if unit else number

    def _unit(self, dimension: str) -> str | None:
        """Unitatea canonică a dimensiunii, din `UnitRegistry`. Niciodată unitatea scrisă de
        model."""
        units = getattr(self.pack, "units", None)
        spec = getattr(units, "specs", {}).get(dimension) if units is not None else None
        if spec is not None and getattr(spec, "canonical", None):
            return str(spec.canonical)
        return None

    def _subject(self, primary: Act) -> GateOutcome | None:
        """Regula 3: `find` fără subiect, fără cuvinte în cerere, fără nevoi de fațetă ⇒ o
        întrebare de subiect pe rafturile de top, doar cu câștig pe numărătorile lor."""
        if primary.kind != "find":
            return None
        topic = self.state.topic
        if topic.has_subject:  # NX-350: o umbrelă e subiect
            return None
        if read_query(primary, pack=self.pack, locale=self.locale).has_words:
            return None
        # NX-375 (recenzia B6): un produs NUMIT e subiectul cererii; plannerul îl caută sau îl
        # arată (același proprietar al alegerii, `find_name_reference`), deci nu se întreabă raftul.
        accepted = [c.change for c in self.checked if c.rejected is None]
        if find_name_reference(self.interp, primary, self.resolved, accepted) is not None:
            return None
        facet_keys = {
            getattr(f, "key", None)
            for f in getattr(self.pack, "facets", ()) or ()
            if getattr(getattr(f, "source", None), "value", None) == "attribute"
        }
        # Cheia unei nevoi nu e mereu cheia fațetei: o limită numerică (NX-334) e `storage_min` /
        # `storage_max`. Dimensiunea o dă vocabularul de nevoi, nu sufixul, altfel «minim 256 GB»
        # fără subiect ar fi primit întrebarea de subiect deși clientul a spus ce vrea.
        needs = NeedVocabulary.from_pack(self.pack)  # type: ignore[arg-type]
        if any(needs.dimension_of(n.key) in facet_keys for n in self.state.active_needs()):
            return None
        if self.vocab is None:
            self._note("vocabulary_unavailable")
            return None
        shelves = self.vocab.categories
        if not shelves:
            return None
        top_depth = min(e.depth for e in shelves)
        top = sorted((e for e in shelves if e.depth == top_depth), key=lambda e: (-e.count, e.key))
        if len(top) < 2:
            # Un singur raft acoperă tot catalogul: răspunsul n-ar tăia nimic (câștig zero), deci nu
            # e un caz de „fără opțiuni", ci unul în care întrebarea nu merită pusă.
            return self._decide("act", "no_subject_low_gain")
        shown = top[:MAX_OPTIONS]
        return self._ask(
            _Ask(
                kind="subject",
                key="subject",
                labels=tuple(e.label for e in shown),
                reason="no_subject",
                candidate_reason="missing_required",
                partition=tuple(e.count for e in shown),
                total=sum(e.count for e in top),
                klass="blocking",
            ),
            on_decline=self._subject_declined,
        )

    def _family(self, primary: Act) -> GateOutcome | None:
        """NX-389 (`kernel.v9.0`): o rutină (`bundle`) fără familie întreabă pentru ce e, între
        familiile care o pot servi. Se aplică doar fără raft în subiect (un raft numit fără familie
        nu e o rutină, NX-386), doar cu executorul general de rutină al pachetului și doar cu
        flagul (`ClarificationPolicy.routine_family_question`).

        Criteriul NU e câștigul de informație: pe o rutină răspunsul nu îngustează un set, alege
        altul (față și corp sunt rutini disjuncte). Se întreabă când cel puțin două familii au
        destule produse pentru nevoile turului (`routine_family_options`); una singură ⇒ rutina pe
        ea, fără întrebare (`family_single`); niciuna ⇒ calea de azi (`family_unservable`).

        Lecturile modelului despre raft (`about=scope`, două rafturi cu familii diferite) bat
        statistica pe catalog: ele vorbesc despre ACEST client («pielea uscată după duș»: față sau
        corp), deci se întreabă între ele chiar când nevoile ar da o familie."""
        if not self.policy.routine_family_question or primary.kind != "bundle":
            return None
        table = getattr(self.pack, "bundle_executors", None)
        if not isinstance(table, Mapping) or WILDCARD not in table:
            return None
        state = self.state
        if state.topic.category_key:
            # Un raft fără familie de rutină rămâne pe calea de azi chiar ghicit: pe trafic, așa
            # arată cererile de UN produs etichetate `bundle` («si o periuta pt el?»), care n-au
            # voie să primească întrebarea de familie (regula GO NX-389).
            shelf_family = family_of_shelf(
                state.topic.category_key, pack=self.pack, vocab=self.vocab
            )
            if shelf_family is None or not self._guessed_shelf():
                return None
            # NX-390: raftul e doar ghicit de model în acest tur, deci familia nu e spusă
            state = _replace(state, topic=_replace(state.topic, category_key=None))
        read = self._scope_families()
        if read is None and routine_family(state, pack=self.pack, vocab=self.vocab):
            return None
        if self.vocab is None:
            self._note("vocabulary_unavailable")
            return None
        options = routine_family_options(self.state, pack=self.pack, families=read)
        if not options:
            self._note("family_unservable")
            return None
        self.family_labels = tuple(self._shelf_label(o.shelf) for o in options)
        self.family_options = tuple(o.shelf for o in options)
        if len(options) == 1:
            self.family_choice = options[0].family
            self._note("family_single")
            return None
        majority = options[0].family

        def declined(reason: str) -> GateOutcome:
            # Deja întrebat (sau altă întrebare vie): rutina pe familia majoritară, spusă
            # clientului de plan (`family_defaulted`), niciodată a doua întrebare (I11, D2).
            self.family_choice = majority
            return self._decide("act", reason)

        return self._ask(
            _Ask(
                kind="family",
                key=ROUTINE_FAMILY_KEY,
                labels=self.family_labels,
                reason="family_readings" if read is not None else "no_family",
                candidate_reason="missing_required",
                partition=tuple(o.count for o in options),
                total=None,
                klass="blocking",
            ),
            on_decline=declined,
        )

    def _guessed_shelf(self) -> bool:
        """NX-390 (`kernel.v10.0`): raftul subiectului e doar o ghicitură a modelului. Fie a intrat
        în subiect în turul în care s-a întrebat familia, fie e pus CHIAR în acest tur (reducerul
        scrie `changed_at_revision` = revizia turului) doar de schimbări de raft ne-`explicit`.
        Turul real `75137b28` («fa mi o rutina pt piele deshidratata»): modelul a scris
        `ten-ingrijirea-tenului` cu citatul „piele”, `implicit`, iar poarta l-a luat drept familie
        spusă, deși clientul n-a spus ten sau corp. Un raft din turele anterioare, unul spus
        explicit, unul numit în fraza unei nevoi (`CheckedChange.shelf`) sau unul ales la o
        întrebare (`answer_topic`, fără schimbare acceptată) rămâne familia."""
        topic = self.state.topic
        # Raftul a intrat în subiect ÎNAINTE ca întrebarea de familie să se închidă: prin
        # construcție n-a fost familia (altfel întrebarea nu se punea). La «nu știu» decide tot
        # poarta, pe familia majoritară spusă clientului, nu ghicitura rămasă în stare. Întrebarea
        # vie are revizia turului în care s-a pus; închisă, `AskedQuestion` are revizia
        # închiderii, iar raftul ALES atunci (`answer_topic`) intră exact la ea, deci nu e prins.
        pending = self.state.pending_clarification
        if (
            pending is not None
            and pending.target_key == ROUTINE_FAMILY_KEY
            and pending.asked_at_revision == topic.changed_at_revision
        ):
            return True
        asked = self.state.asked(ROUTINE_FAMILY_KEY)
        if asked is not None and topic.changed_at_revision < asked.revision:
            return True
        if asked is not None and topic.changed_at_revision == asked.revision:
            return False  # raftul ales chiar la închiderea întrebării: răspunsul clientului
        if topic.changed_at_revision != self.state.revision:
            return False
        if any(c.rejected is None and c.shelf == topic.category_key for c in self.checked):
            return False  # clientul l-a numit în fraza unei nevoi («ten gras»): spus, nu ghicit
        shelf_changes = [
            c
            for c in self.checked
            if c.rejected is None
            and c.dimension == CATEGORY_DIMENSION
            and c.change.op in ("set", "add")
            and c.canonical_value == topic.category_key
        ]
        return bool(shelf_changes) and all(c.provenance != "explicit" for c in shelf_changes)

    def _scope_families(self) -> tuple[str, ...] | None:
        """Familiile rafturilor din lecturile `scope` ale modelului, când sunt cel puțin două
        diferite; altfel None (lecturile nu privesc familia, regula lor rămâne a `_readings`)."""
        if self.vocab is None:
            return None
        found: list[str] = []
        for ambiguity in self.interp.ambiguities:
            if ambiguity.about != "scope":
                continue
            for dimension, key in _read_readings(
                ambiguity.readings, self.vocab, self.overlays
            ).values:
                if dimension != CATEGORY_DIMENSION:
                    continue
                family = family_of_shelf(key, pack=self.pack, vocab=self.vocab)
                if family is not None and family not in found:
                    found.append(family)
        return tuple(found) if len(found) >= 2 else None

    def _shelf_label(self, key: str) -> str:
        for entry in self.vocab.categories if self.vocab is not None else ():
            if entry.key == key:
                return entry.label
        return key

    def _subject_declined(self, reason: str) -> GateOutcome:
        # Sub prag sau deja întrebat: `act`. O a treia întrebare de subiect e bucla (I11), iar un
        # `must_ask` fără întrebare ar lăsa turul fără nicio cale.
        return self._decide("act", "no_subject_low_gain" if reason == "low_gain" else reason)

    def _readings(self) -> GateOutcome | None:
        """Regulile 4 și 5: lecturile modelului despre subiect (`scope`) sau despre o valoare
        (`value`). Aceeași valoare ⇒ nimic de întrebat; valori diferite ⇒ întrebare doar cu câștig
        pe setul curent. `about=reference` nu decide nimic: ținta e a resolverului."""
        relevant = [a for a in self.interp.ambiguities if a.about in ("scope", "value")]
        if not relevant:
            return None
        if self.vocab is None:
            self._note("vocabulary_unavailable")
            return None
        read = [(a, _read_readings(a.readings, self.vocab, self.overlays)) for a in relevant]
        resolved = [(a, r) for a, r in read if r.values]
        if not resolved:
            self._note("readings_unresolved")
            return None
        differing = [(a, r) for a, r in resolved if len(r.values) > 1]
        if not differing:
            return self._decide("resolve_from_context", "same_reading")
        ambiguity, readings = differing[0]
        values = readings.values[:MAX_OPTIONS]
        dimensions = sorted({d for d, _ in values})
        # Partiția cere o singură dimensiune; lecturi pe dimensiuni diferite nu se pot număra pe
        # același atribut, deci câștigul rămâne nemăsurabil pe ecran (`None` ⇒ întrebare).
        if len(dimensions) == 1:
            partition, total = self._partition(dimensions[0], [k for _, k in values])
        else:
            partition, total = (), None
        return self._ask(
            _Ask(
                kind=ambiguity.about,
                key="+".join(dimensions),
                labels=tuple(self._value_label(d, k) for d, k in values),
                reason="readings_differ",
                candidate_reason="disambiguation",
                partition=partition,
                total=total,
                klass="read",
            ),
            on_decline=self._declined("read", "readings_low_gain"),
        )

    def _ambiguous_reads(self, acts: list[tuple[int, Act]]) -> GateOutcome | None:
        """Regulile 6 și 7: o țintă `ambiguous` la o citire. Până la `MAX_READ_ALL` candidați se
        răspunde despre toți (NX-385: cât arată comparația, nu trei); peste, se întreabă pe TOȚI
        (`_target_ask`), niciodată pe primii patru. Condițiile sunt exclusive (toate ≤ plafon /
        vreuna peste), deci ordinea nu poate alege între două reguli aplicabile."""
        ambiguous = [
            ref
            for _, act in acts
            if act.kind in READ_ACTS
            for target in act.targets
            if (ref := self.resolved.get(target)) is not None and ref.outcome == "ambiguous"
        ]
        if not ambiguous:
            return None
        too_many = next((r for r in ambiguous if len(r.product_ids) > MAX_READ_ALL), None)
        if too_many is None:
            # Recenzia NX-385: plafonul e al RĂSPUNSULUI, nu al unei referințe. Plannerul unește
            # candidații tuturor țintelor actului (două referințe ambigue, 3 + 3), deci peste
            # plafon se întreabă pe referința cu cei mai mulți candidați.
            for _, act in acts:
                if act.kind not in READ_ACTS:
                    continue
                refs = [r for t in act.targets if (r := self.resolved.get(t)) is not None]
                union = {
                    pid
                    for r in refs
                    if r.outcome in ("exact", "ambiguous")
                    for pid in r.product_ids
                }
                if len(union) > MAX_READ_ALL:
                    too_many = max(
                        (r for r in refs if r.outcome == "ambiguous"),
                        key=lambda r: len(r.product_ids),
                        default=None,
                    )
                    if too_many is not None:
                        break
        if too_many is None:
            return self._decide("act_both", "ambiguous_read")
        ids = [pid for pid in too_many.product_ids if pid in self.facts.products]
        return self._target_ask(
            ids,
            reason="ambiguous_too_many",
            klass="read",
            on_decline=self._declined("read", "ambiguous_too_many"),
        )

    def _confirm(self) -> GateOutcome | None:
        """Regula 8: o nevoie `user_implicit` scrisă în ACEST tur, pe o cheie neîntrebată, cu câștig
        pe setul curent ⇒ `act` + o întrebare de confirmare ca ultimă frază. Fără set (primul tur)
        nu se confirmă nimic: o confirmare fără dovadă că ar tăia ceva e o întrebare în plus.
        „Scrisă în acest tur" = `updated_revision` e revizia turului (regula reducerului,
        `_written_by_previous_turn`).

        NX-385 (`w3_barbati_ce_aveti#3`, `w5_un_cuvant_ser#2`): o nevoie `implicit` e moale
        (I7), iar plannerul o duce în `prefer`, deci ORDONEAZĂ rezultatele, nu exclude nimic
        (`kernel.v5.0`). O întrebare „Să înțeleg că e vorba de X?" sub produsele deja ordonate după
        X confirmă o ipoteză care n-ar fi costat nimic dacă era greșită. Confirmarea rămâne doar
        unde ipoteza ar decide ceva: o nevoie care filtrează (`hard`), sau o dimensiune pe care
        lecturile modelului se bat (`_competing_dimensions`). Restul se aplică în tăcere."""
        if self.state.revision <= 0:
            return None
        competing = self._competing_dimensions()
        for need in self.state.active_needs():
            if need.source != "user_implicit" or need.updated_revision != self.state.revision:
                continue
            if not isinstance(need.normalized_value, str):
                continue
            if need.strength != "hard" and need.key not in competing:
                continue
            partition, total = self._partition(need.key, None)
            if total is None:
                continue
            outcome = self._ask(
                _Ask(
                    kind="confirm",
                    key=need.key,
                    labels=(self._value_label(need.key, need.normalized_value),),
                    reason="confirm_implicit",
                    candidate_reason="disambiguation",
                    partition=partition,
                    total=total,
                    klass="confirm",
                    min_options=1,
                ),
                # Câștig mic, cheie deja întrebată sau întrebare în așteptare: regula nu se aplică
                # (condiția ei e „neîntrebată, cu câștig"), iar turul ajunge la `clear`.
                on_decline=lambda _reason: None,
            )
            if outcome is not None:
                return outcome
        return None

    def _competing_dimensions(self) -> frozenset[str]:
        """Dimensiunile pe care lecturile modelului (`scope`/`value`) duc la cel puțin două valori
        diferite, citite O DATĂ prin vocabular (`_read_readings`). Fără vocabular, niciuna."""
        if self.vocab is None:
            return frozenset()
        dims: set[str] = set()
        for ambiguity in self.interp.ambiguities:
            if ambiguity.about not in ("scope", "value"):
                continue
            values = _read_readings(ambiguity.readings, self.vocab, self.overlays).values
            if len(values) > 1:
                dims |= {d for d, _ in values}
        return frozenset(dims)


def lookup_attributes(
    interp: TurnInterpretation, *, vocab: CatalogVocabulary | None, pack: object | None
) -> tuple[str, ...]:
    """Atributele pe care poarta le NUMĂRĂ pe ecran (regulile 5 și 8), ca pasul 6 să le ceară în
    ACEEAȘI citire (`plan_lookup(extra_attributes=…)`). Fără ele, `reference_facts` aduce doar
    dimensiunile de referință ale pachetului, iar partiția ar fi rămas nemăsurabilă.

    PUR: dimensiunile lecturilor (rezolvate o dată, prin `_read_readings`) și ale schimbărilor
    turului (o nevoie `implicit` confirmabilă e o schimbare a acestui tur), traduse în cheia din
    `attributes` (`source_key`). Raftul nu e un atribut."""
    dims: set[str] = set()
    usable = vocab if vocab is not None and not vocab.is_empty() else None
    if usable is not None:
        overlays = facet_overlays(pack, usable.facet_names)
        for ambiguity in interp.ambiguities:
            if ambiguity.about in ("scope", "value"):
                read = _read_readings(ambiguity.readings, usable, overlays)
                dims |= {d for d, _ in read.values}
    facets = {getattr(f, "key", None): f for f in getattr(pack, "facets", ()) or ()}
    dims |= {c.dimension for c in interp.changes if c.dimension in facets}
    dims -= {CATEGORY_DIMENSION, PRICE}
    return tuple(sorted(str(getattr(facets.get(d), "source_key", d) or d) for d in dims if d))


def decide_ambiguity(
    interp: TurnInterpretation,
    checked: Sequence[CheckedChange],
    resolved: Sequence[ResolvedRef],
    state: ConversationStateV2,
    facts: ReferenceFacts,
    *,
    vocab: CatalogVocabulary | None,
    pack: object | None,
    locale: str,
    policy: ClarificationPolicy,
) -> GateOutcome:
    """PUR. Un singur verdict pe tur (I11), cu motiv din `GATE_REASONS`.

    `checked` = ieșirea validatorului de proveniență (limitele `hard_conflict` stau aici);
    `resolved` = verdictele resolverului (singura sursă a verdictului pe o țintă; un
    `Ambiguity{about: reference}` scris de model nu decide nimic); `state` = starea DUPĂ reducer,
    cu ecranul de DINAINTEA turului; `facts` = faptele recitite în tur (ecran + ținte).

    Vocabular absent ⇒ regulile care au nevoie de el trec mai departe, iar verdictul final o spune
    (`vocabulary_unavailable`). Nu ridică niciodată."""
    return _Gate(
        interp,
        checked,
        resolved,
        state,
        facts,
        vocab=vocab,
        pack=pack,
        locale=locale,
        policy=policy,
    ).run()


def memory_proposal(outcome: GateOutcome, turn_id: str) -> StateUpdateProposal | None:
    """Memoria întrebării scrisă din `GateOutcome` (NX-336: mutată din `fixture_catalog`, un
    singur proprietar pentru replay și pentru commit-ul pasului 6): `set_pending_question` pentru o
    întrebare care ține locul răspunsului, `note_asked` pentru confirmarea pusă la final. PUR."""
    if outcome.asked_key is None:
        return None
    if outcome.asked_kind == "pending":
        routine = outcome.asked_key == ROUTINE_FAMILY_KEY and bool(outcome.family_options)
        # NX-389b: întrebarea de familie ține minte ACTUL (rutina) și opțiunile (cheile rafturilor),
        # ca răspunsul să reia rutina (`routine_family.resume_routine`)
        return StateUpdateProposal(
            "set_pending_question",
            key=outcome.asked_key,
            reason=outcome.decision.reason,
            source="policy",
            turn_id=turn_id,
            options_refs=outcome.family_options if routine else (),
            resume_route=RESUME_BUNDLE if routine else None,
        )
    return StateUpdateProposal(
        "note_asked", key=outcome.asked_key, source="policy", turn_id=turn_id
    )


def question_answered(
    state: ConversationStateV2, thread: str, turn_id: str
) -> StateUpdateProposal | None:
    """NX-336 §1: pe turul interpretat, o întrebare VIE se închide când turul nu e o paranteză.

    Interpretarea a văzut întrebarea (blocul `PENDING`) și i-a citit răspunsul ca schimbări, deci
    nevoia vine din interpretare; aici se închide doar întrebarea, ca `clarify_resume` pe calea de
    azi. Fără asta întrebarea ar rămâne vie până expiră, iar poarta n-ar mai putea întreba nimic
    (`set_pending_question` respins cu `already_pending`). PUR."""
    pending = state.pending_clarification
    if thread == "aside" or pending is None or pending.expired_at(state.revision):
        return None
    return StateUpdateProposal(
        "resolve_question",
        key=pending.target_key,
        question_id=pending.question_id,
        source="user_explicit",
        turn_id=turn_id,
    )


__all__ = [
    "BOUND_KINDS",
    "GATE_REASONS",
    "MAX_ACT_BOTH",
    "MAX_OPTIONS",
    "READ_ACTS",
    "ROUTINE_FAMILY_KEY",
    "TEMPLATE_KINDS",
    "GateOutcome",
    "decide_ambiguity",
    "lookup_attributes",
    "memory_proposal",
    "question_answered",
    "target_question_key",
    "template_markers",
    "valid_template",
]
