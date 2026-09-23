"""NX-235 — când merită întrebat, și când e mai onest să răspunzi.

O clarificare are un cost pe care nu-l plătește asistentul: încă un tur, încă o așteptare, și
senzația că vorbești cu un formular. Azi decizia de a întreba e implicită — nano spune
`missing_field`, iar codul întreabă. Nimic nu verifică dacă RĂSPUNSUL ar schimba ceva: dacă toate
cele patru produse care se potrivesc sunt oricum sub buget, întrebarea despre buget nu adaugă
informație, doar întârziere.

Modulul face din „merită?" un calcul, nu o intuiție. `information gain` se estimează din
PARTIȚIA candidaților: cât rămâne, în medie, după fiecare răspuns posibil. O întrebare care taie
mulțimea în două părți egale e valoroasă; una la care 9 din 10 candidați cad în același coș nu e.

Deasupra calculului stau porțile care nu se negociază: siguranța întreabă întotdeauna, aceeași
cheie nu se re-întreabă la nesfârșit, iar când nu există rezultate NU se pune o întrebare — se
spune onest ce nu s-a găsit și ce constrângere ar putea fi relaxată.

PUR și determinist: fără DB, fără LLM, fără ceas.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

from src.conversation.needs import NeedVocabulary, norm_key
from src.conversation.state_v2 import ConversationStateV2

ClarificationReason = Literal["safety", "hard_conflict", "missing_required", "disambiguation"]

# Prioritatea motivelor. Siguranța prima — o întrebare de siguranță nu concurează pe information
# gain cu una de rafinare comercială.
_REASON_PRIORITY: dict[str, int] = {
    "safety": 0,
    "hard_conflict": 1,
    "missing_required": 2,
    "disambiguation": 3,
}
# Motivele care întreabă indiferent de câștigul informațional (cerute de corectitudine, nu de UX).
MANDATORY_REASONS: frozenset[str] = frozenset({"safety", "hard_conflict"})


@dataclass(frozen=True)
class ClarificationCandidate:
    """O întrebare POSIBILĂ, propusă de un stagiu. `partition` = câți candidați ar rămâne pentru
    fiecare răspuns posibil — sursa estimării de information gain. Fără ea, întrebarea e evaluată
    conservator (nu presupunem că ar ajuta)."""

    key: str
    reason: ClarificationReason = "missing_required"
    options_refs: tuple[str, ...] = ()
    partition: tuple[int, ...] = ()
    resume_route: str | None = None


@dataclass(frozen=True)
class ClarificationDecision:
    """Decizia + DE CE. `reason` e din vocabular închis (intră în
    `clarification_decision{ask|answer,reason,information_gain_bucket}`)."""

    ask: bool
    candidate: ClarificationCandidate | None = None
    reason: str = "no_candidate"
    information_gain: float = 0.0
    gain_bucket: str = "none"


@dataclass(frozen=True)
class ClarificationPolicy:
    """Pragurile deciziei — în cod, nu în prompt (P4)."""

    vocabulary: NeedVocabulary = field(default_factory=NeedVocabulary)
    min_information_gain: float = 0.30
    max_attempts_per_key: int = 2


def estimate_information_gain(total_candidates: int, partition: Sequence[int]) -> float:
    """Cât din mulțimea de candidați dispare, în medie, după răspuns. `0.0` = întrebarea nu ajută.

    Modelul: dacă opțiunea *i* acoperă `n_i` candidați, ea e aleasă cu probabilitatea `n_i/acoperit`
    și lasă în urmă `n_i` (plus candidații pe care fațeta nu îi discriminează deloc). Câștigul e
    reducerea așteptată, normalizată la mulțimea inițială — deci comparabilă între întrebări.

    Cazurile limită sunt cele care contează în practică: zero candidați (n-ai ce rafina — răspunzi
    onest) și unul singur (deja decis) dau `0.0`."""
    total = max(0, int(total_candidates))
    buckets = [max(0, int(n)) for n in partition if int(n) > 0]
    covered = sum(buckets)
    if total <= 1 or covered <= 0:
        return 0.0
    expected_remaining = sum(n * n for n in buckets) / covered + max(0, total - covered)
    gain = 1.0 - (expected_remaining / total)
    return max(0.0, min(1.0, gain))


def gain_bucket(gain: float) -> str:
    """Bucket low-cardinality pentru telemetrie (P10/P12: fără valori continue în labels)."""
    if gain <= 0.0:
        return "none"
    if gain < 0.30:
        return "low"
    if gain < 0.60:
        return "medium"
    return "high"


def decide_clarification(
    state: ConversationStateV2,
    candidates: Sequence[ClarificationCandidate],
    *,
    total_candidates: int | None,
    policy: ClarificationPolicy,
) -> ClarificationDecision:
    """Cel mult O întrebare per tur, aleasă determinist.

    Ordinea de evaluare e importantă: porțile de buclă și de cunoaștere se aplică ÎNAINTE de
    câștigul informațional — o întrebare pe care am pus-o deja nu devine bună fiindcă ar tăia
    mult. Excepția e siguranța, care trece peste tot (inclusiv peste o întrebare în așteptare pe
    altă cheie: acolo cea comercială cedează locul).

    `total_candidates=None` înseamnă „încă n-am căutat" (triajul rulează înaintea retrievalului):
    acolo câștigul nu se poate estima, deci poarta de gain se SARE, nu se presupune zero — altfel
    am tăcea exact în momentul în care întrebarea e cea mai utilă. `0` e altceva: am căutat și n-am
    găsit nimic; acolo o întrebare nu ajută, un răspuns onest da (`UNKNOWN != MISMATCH`)."""
    if not candidates:
        return ClarificationDecision(False, None, "no_candidate")

    measurable = total_candidates is not None

    def _gain(candidate: ClarificationCandidate) -> float:
        if candidate.reason in MANDATORY_REASONS:
            return 1.0
        return estimate_information_gain(total_candidates or 0, candidate.partition)

    ordered = sorted(
        candidates, key=lambda c: (_REASON_PRIORITY.get(c.reason, 9), -_gain(c), c.key)
    )
    pending = state.pending_clarification
    pending_live = pending is not None and not pending.expired_at(state.revision)

    fallback = ClarificationDecision(False, None, "no_candidate")
    for candidate in ordered:
        key = norm_key(candidate.key)
        mandatory = candidate.reason in MANDATORY_REASONS
        gain = _gain(candidate)
        bucket = gain_bucket(gain) if (measurable or mandatory) else "unknown"
        decision_for = _gate(
            state, candidate, key, policy, pending_live=pending_live, mandatory=mandatory
        )
        if decision_for is not None:
            fallback = ClarificationDecision(False, candidate, decision_for, gain, bucket)
            continue
        if measurable and not mandatory and gain < policy.min_information_gain:
            fallback = ClarificationDecision(False, candidate, "low_gain", gain, bucket)
            continue
        return ClarificationDecision(True, candidate, candidate.reason, gain, bucket)
    return fallback


def _gate(
    state: ConversationStateV2,
    candidate: ClarificationCandidate,
    key: str,
    policy: ClarificationPolicy,
    *,
    pending_live: bool,
    mandatory: bool,
) -> str | None:
    """Porțile de buclă/cunoaștere. Întoarce motivul respingerii, sau `None` dacă se poate întreba.

    Siguranța sare peste tot, inclusiv peste o întrebare comercială în așteptare: dacă trebuie să
    știm dacă e însărcinată, întrebarea despre nuanță poate aștepta."""
    if candidate.reason == "safety":
        return None
    if pending_live:
        return "already_pending"
    asked = state.asked(key)
    if asked is not None and asked.attempts >= policy.max_attempts_per_key:
        return "already_asked"
    if not mandatory and state.need_for(key) is not None:
        # Deja știm răspunsul — a-l re-cere e exact bucla pe care clienții o resimt ca „nu ascultă".
        return "already_known"
    return None


# --- NX-315: întrebarea de îngustare, ALĂTURI de produse ------------------------------------------
#
# Diferența față de `decide_clarification`: acolo întrebarea ține locul răspunsului (n-am căutat
# încă, sau n-am găsit). Aici produsele sunt deja pe ecran, iar întrebarea e cea pe care un vânzător
# o pune după ce ți-a arătat raftul: „pentru ce tip de ten?". Deci câștigul se măsoară pe setul
# SERVIT, nu pe un catalog ipotetic, și asta face ca `total_candidates` să fie chiar cunoscut.

#: Peste atâtea valori nu mai e o întrebare, e un formular. Aceeași limită ca meniul NX-295
#: (`clarify_menu._MAX_PER_DIMENSION`), din același motiv.
NARROWING_MAX_VALUES = 4

#: Vocabular ÎNCHIS de motive (eticheta evenimentului `narrowing_offer`).
NARROWING_REASONS: tuple[str, ...] = (
    "offered",
    "no_partitioning_facet",
    "known",
    "asked",
    "single_value",
    "too_many_values",
    "low_gain",
)

# Ordinea în care se raportează un refuz când NICIO fațetă nu trece: cel mai apropiat de a trece
# câștigă. „low_gain" spune „am avut ce întreba, dar nu merita"; „no_partitioning_facet" spune „n-am
# avut nimic de întrebat" — pentru diagnoză sunt întrebări foarte diferite.
_NARROWING_REFUSAL_RANK = {
    "low_gain": 0,
    "too_many_values": 1,
    "single_value": 2,
    "asked": 3,
    "known": 4,
    "no_partitioning_facet": 5,
}

# Tipurile de fațetă ale căror valori se pot oferi ca opțiuni. Un număr (preț, SPF) se întreabă ca
# interval, nu ca listă, iar un boolean nu desparte un set, doar îl filtrează.
_NARROWABLE_TYPES = frozenset({"enum", "text", "list"})


@dataclass(frozen=True)
class NarrowingOffer:
    """Ce are voie modelul să întrebe pe turul ăsta. `values` = cheile canonice prezente în set,
    descrescător după câte produse le poartă; `partition` = aceleași numere, sursa câștigului."""

    facet: str
    values: tuple[str, ...]
    partition: tuple[int, ...]
    gain: float


@dataclass(frozen=True)
class NarrowingVerdict:
    offer: NarrowingOffer | None
    reason: str


def values_of(product: object, source_key: str) -> tuple[str, ...]:
    """Valorile unei fațete pe un produs, normalizate. Gol = fațetă NECUNOSCUTĂ pe produs (NX-316
    o citește ca „nu întrebăm", fiindcă răspunsul ar fi „nu știu")."""
    attrs = product.get("attributes") if isinstance(product, dict) else None
    raw = attrs.get(source_key) if isinstance(attrs, dict) else None
    items = raw if isinstance(raw, list) else [raw]
    out: list[str] = []
    for v in items:
        if isinstance(v, str) and (k := " ".join(v.split()).lower()) and k not in out:
            out.append(k)
    return tuple(out)


def narrowing_candidate(
    facets: Sequence[object],
    served: Sequence[object],
    *,
    known: frozenset[str] = frozenset(),
    asked: frozenset[str] = frozenset(),
    min_gain: float = 0.30,
    max_values: int = NARROWING_MAX_VALUES,
) -> NarrowingVerdict:
    """Fațeta care merită întrebată ALĂTURI de setul servit, sau niciuna. PURĂ.

    O fațetă e candidată dacă (toate):
      • e declarată `binding == "partitioning"` în pachet: cumpărătorul are exact UNA dintre valori,
        deci răspunsul chiar îngustează. La o fațetă `additive` („hidratare și luminozitate")
        răspunsul „amândouă" e valid, iar întrebarea n-ar tăia nimic;
      • clientul n-a spus-o deja (`known`, calculat de apelant din ce a SCRIS clientul și din
        constrângerile turului). A-l pune să repete e bucla pe care clienții o resimt ca „nu
        ascultă";
      • n-a fost întrebată în conversație (`asked`);
      • setul are ≥2 valori pe ea, fiecare pe ≥1 produs, și cel mult `max_values`;
      • câștigul informațional pe setul servit trece pragul NX-235.

    Dintre candidate câștigă cea cu câștigul cel mai mare; la egalitate, ordinea din pachet.
    Nu numește nicio categorie și nicio limbă: pe electrocasnice fațeta ar fi voltajul, pe
    anvelope dimensiunea, iar codul e același.
    """
    total = len(served)
    best: NarrowingOffer | None = None
    refusals: list[str] = []
    for facet in facets:
        if getattr(facet, "binding", "additive") != "partitioning":
            continue
        vtype = getattr(getattr(facet, "value_type", None), "value", None)
        source = getattr(getattr(facet, "source", None), "value", None)
        if vtype not in _NARROWABLE_TYPES or source != "attribute":
            continue
        key = str(getattr(facet, "key", "") or "")
        if not key:
            continue
        if key in known:
            refusals.append("known")
            continue
        if key in asked:
            refusals.append("asked")
            continue
        counts: dict[str, int] = {}
        for p in served:
            for v in values_of(p, str(getattr(facet, "source_key", key) or key)):
                counts[v] = counts.get(v, 0) + 1
        if len(counts) < 2:
            refusals.append("single_value")
            continue
        if len(counts) > max_values:
            refusals.append("too_many_values")
            continue
        ordered = sorted(counts, key=lambda v: (-counts[v], v))
        partition = tuple(counts[v] for v in ordered)
        gain = estimate_information_gain(total, partition)
        if gain < min_gain:
            refusals.append("low_gain")
            continue
        if best is None or gain > best.gain:
            best = NarrowingOffer(key, tuple(ordered), partition, gain)
    if best is not None:
        return NarrowingVerdict(best, "offered")
    if not refusals:
        return NarrowingVerdict(None, "no_partitioning_facet")
    return NarrowingVerdict(None, min(refusals, key=_NARROWING_REFUSAL_RANK.__getitem__))


def relaxation_candidates(state: ConversationStateV2) -> tuple[str, ...]:
    """Ce se poate RELAXA onest când nu există rezultate: doar nevoile `soft`, în ordinea inversă a
    declarării (cea mai recentă preferință cedează prima). `hard` nu apare niciodată aici — bugetul
    și excluderile nu se „relaxează" de la sine, se discută explicit cu clientul."""
    soft = [n for n in state.active_needs() if n.strength != "hard" and not n.sensitive_class]
    return tuple(n.key for n in sorted(soft, key=lambda n: -n.updated_revision))


__all__ = [
    "MANDATORY_REASONS",
    "NARROWING_MAX_VALUES",
    "NARROWING_REASONS",
    "ClarificationCandidate",
    "ClarificationDecision",
    "ClarificationPolicy",
    "ClarificationReason",
    "NarrowingOffer",
    "NarrowingVerdict",
    "decide_clarification",
    "estimate_information_gain",
    "gain_bucket",
    "narrowing_candidate",
    "relaxation_candidates",
    "values_of",
]
