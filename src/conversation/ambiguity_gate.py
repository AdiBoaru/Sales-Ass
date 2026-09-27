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
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
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
    MUTATING_ACTS,
    ProductFacts,
    ReferenceFacts,
    gate_act_targets,
)
from src.conversation.state_v2 import ConversationStateV2
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
#: Peste atâția candidați o citire „despre toate" nu mai e un răspuns, e un catalog.
MAX_ACT_BOTH = 3
#: Câte opțiuni încap într-o întrebare: aceeași limită ca întrebarea de îngustare (NX-315).
MAX_OPTIONS = NARROWING_MAX_VALUES

PRICE = "price"
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


def target_question_key(product_ids: Sequence[str]) -> str:
    """Cheia unei întrebări despre o țintă: `ref:` + primele 8 caractere din sha1 peste id-urile
    candidaților, SORTATE. Nu `ref:<ref_id>`: `r1` e local turului, deci aceeași întrebare ar primi
    altă cheie la turul următor, iar anti-bucla n-ar vedea-o."""
    digest = hashlib.sha1(",".join(sorted(set(product_ids))).encode("utf-8")).hexdigest()
    return f"ref:{digest[:8]}"


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
        """Numele de afișare, fără repetiții: pe SOLE există produse cu nume identic (familii de
        nuanțe), iar „La care te referi: GESKE SonicLift și GESKE SonicLift?" nu e o întrebare. Sub
        două nume distincte, `_ask` coboară pe `no_options`."""
        names: list[str] = []
        for pid in product_ids:
            if pid not in self.facts.products:
                continue
            name = display_name(self.facts.products[pid].name) or pid
            if name not in names:
                names.append(name)
        return tuple(names)

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
        labels = ask.labels[:MAX_OPTIONS]
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
            self.state, [candidate], total_candidates=ask.total, policy=self.policy
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
        skipped = tuple(
            sorted(
                {
                    c.act_index
                    for c in checks
                    if c.verdict in ("unknown_reference", "invalid_reference_target")
                }
            )
        )
        acts = [(i, a) for i, a in enumerate(self.interp.acts) if i not in skipped]
        if skipped:
            self._note("invalid_target")
        outcome = self._rules(acts, [c for c in checks if c.act_index not in skipped])
        return outcome._replace(skipped_acts=skipped)

    def _rules(self, acts: list[tuple[int, Act]], checks) -> GateOutcome:
        # Regula 2 nu depinde de acte: limitele care se contrazic sunt ale stării, deci se întreabă
        # și când nu rămâne niciun act (toate sărite de regula 0, sau un tur doar cu schimbări).
        rules: list[Callable[[], GateOutcome | None]] = [self._conflict]
        if acts:
            rules = [
                lambda: self._mutation(acts, checks),
                self._conflict,
                lambda: self._subject(acts[-1][1]),
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
            return self._ask(
                _Ask(
                    kind="reference",
                    key=target_question_key(ids),
                    labels=self._names(ids[:MAX_OPTIONS]),
                    reason="mutation_not_exact",
                    candidate_reason="disambiguation",
                    partition=(1,) * min(len(ids), MAX_OPTIONS),
                    total=len(ids),
                    klass="blocking",
                ),
                on_decline=self._declined("blocking", "mutation_not_exact"),
            )
        for check in checks:
            if check.act_index not in mutating:
                continue
            ref = self.resolved.get(check.target)
            if check.verdict == "not_exact":
                ids = [
                    pid for pid in (ref.product_ids if ref else []) if pid in self.facts.products
                ]
                if len(ids) < 2:
                    # Nicio pereche de candidați pe țintă (un nume negăsit, un produs dispărut din
                    # catalog): întrebarea „la care te referi" se pune pe ecran, dacă are ce oferi.
                    ids = [f.product_id for f in self._screen()]
                shown = ids[:MAX_OPTIONS]
                return self._ask(
                    _Ask(
                        kind="reference",
                        key=target_question_key(ids),
                        labels=self._names(shown),
                        reason="mutation_not_exact",
                        candidate_reason="disambiguation",
                        partition=(1,) * len(shown),
                        total=len(ids),
                        klass="blocking",
                    ),
                    on_decline=self._declined("blocking", "mutation_not_exact"),
                )
            if ref is not None and ref.outcome == "exact" and ref.reason == "unavailable":
                # Un produs epuizat iese `exact` cu motiv, nu `stale` (NX-329). N-ai ce întreba
                # între UN produs; coșul nu se execută, iar răspunsul spune că e epuizat.
                return self._decide("must_ask", "mutation_unavailable")
        return None

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
        if topic.category_key or topic.product_type:
            return None
        if read_query(primary, pack=self.pack, locale=self.locale).has_words:
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
        """Regulile 6 și 7: o țintă `ambiguous` la o citire. Până la trei candidați se răspunde
        despre toți; peste, se întreabă pe primii patru. Condițiile sunt exclusive (toate ≤ 3 /
        vreuna > 3), deci ordinea nu poate alege între două reguli aplicabile."""
        ambiguous = [
            ref
            for _, act in acts
            if act.kind in READ_ACTS
            for target in act.targets
            if (ref := self.resolved.get(target)) is not None and ref.outcome == "ambiguous"
        ]
        if not ambiguous:
            return None
        too_many = next((r for r in ambiguous if len(r.product_ids) > MAX_ACT_BOTH), None)
        if too_many is None:
            return self._decide("act_both", "ambiguous_read")
        ids = [pid for pid in too_many.product_ids if pid in self.facts.products]
        shown = ids[:MAX_OPTIONS]
        return self._ask(
            _Ask(
                kind="reference",
                key=target_question_key(ids),
                labels=self._names(shown),
                reason="ambiguous_too_many",
                candidate_reason="disambiguation",
                partition=(1,) * len(shown),
                total=len(ids),
                klass="read",
            ),
            on_decline=self._declined("read", "ambiguous_too_many"),
        )

    def _confirm(self) -> GateOutcome | None:
        """Regula 8: o nevoie `user_implicit` scrisă în ACEST tur, pe o cheie neîntrebată, cu câștig
        pe setul curent ⇒ `act` + o întrebare de confirmare ca ultimă frază. Fără set (primul tur)
        nu se confirmă nimic: o confirmare fără dovadă că ar tăia ceva e o întrebare în plus.
        „Scrisă în acest tur" = `updated_revision` e revizia turului (regula reducerului,
        `_written_by_previous_turn`)."""
        if self.state.revision <= 0:
            return None
        for need in self.state.active_needs():
            if need.source != "user_implicit" or need.updated_revision != self.state.revision:
                continue
            if not isinstance(need.normalized_value, str):
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


__all__ = [
    "BOUND_KINDS",
    "GATE_REASONS",
    "MAX_ACT_BOTH",
    "MAX_OPTIONS",
    "READ_ACTS",
    "TEMPLATE_KINDS",
    "GateOutcome",
    "decide_ambiguity",
    "lookup_attributes",
    "target_question_key",
    "template_markers",
    "valid_template",
]
