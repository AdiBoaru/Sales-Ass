"""NX-246 (felia 3) — pairwise ORB: randomizare, adjudecare, acord, order bias, bootstrap.

NX-210 (`nx210_blind`) a rezolvat deja pairwise-ul pe RĂSPUNSURI, cu o statistică de tip
„delta medie pe rubrică". Aici e nevoie de altceva, din două motive:

1. unitatea e un JOURNEY, nu un răspuns — evaluatorul vede o conversație și alege un câștigător;
2. pragul cerut e o PROPORȚIE (`win + 0,5×tie ≥ 55%`, cu limita inferioară bootstrap ≥ 50%), nu o
   diferență de medii. Cele două nu sunt interschimbabile: se poate câștiga la medii pierzând
   majoritatea journey-urilor, dacă victoriile sunt mari și înfrângerile multe.

Ce se refolosește neatins din NX-210: doctrina (randomizare deterministă din seed, pachet orb
separat de cheia de dezvăluire, redactare PII înainte de a scrie orice artefact) și, unde se
potrivește, chiar funcțiile.

**Trei capcane pe care modulul le tratează explicit:**

  • **order bias** — dacă „A" câștigă sistematic indiferent ce e în A, rezultatul măsoară poziția,
    nu calitatea. Se raportează întotdeauna, chiar când e mic;
  • **acordul dintre evaluatori** — o medie din doi evaluatori care nu sunt de acord niciodată e
    un număr fabricat. Dezacordul declanșează adjudecare, iar rata lui intră în raport;
  • **scurgerea de etichetă** — dacă un evaluator poate deduce care variantă e candidate (din
    ordine, din metadate, din artefact), tot exercițiul e teatru. Pachetul orb nu conține
    versiuni, iar `assert_blind` refuză să-l emită dacă ar conține.
"""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass, field
from statistics import mean
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from src.safety.external_data import contains_pii

Side = Literal["A", "B"]
Winner = Literal["A", "B", "tie"]

#: Rubrica NX-246. Alta decât cea NX-210 (task_success/factual_grounding/…), deliberat: acolo se
#: măsura dacă răspunsul e CORECT, aici dacă e BUN de citit. Ambele sunt necesare, în ordinea
#: asta — corectitudinea e o poartă deterministă, nu o notă (vezi `quality_gate`).
RUBRIC_DIMENSIONS: tuple[str, ...] = (
    "naturalness",  # română firească, continuitate, fără ton robotic/meta
    "helpfulness",  # răspunde direct și avansează cumpărarea fără pași inutili
    "trust",  # onest despre necunoscut, nu exagerează, justifică prin fapte
    "no_overtalk",  # lungimea/numărul de opțiuni sunt proporționale cu întrebarea
    "context_handling",  # ține corecțiile, referințele și pagina/coșul fără contradicții
)

#: Motivele permise pentru o alegere. ÎNCHISE: „mi-a plăcut mai mult" nu e un motiv pe care îl poți
#: agrega, iar text liber de la evaluatori ar deveni date nestructurate în artefact.
CHOICE_REASONS: frozenset[str] = frozenset(
    {
        "more_natural",
        "more_helpful",
        "more_honest",
        "shorter",
        "better_context",
        "fewer_errors",
        "no_difference",
    }
)

#: Diferență peste care doi evaluatori se consideră în dezacord pe o dimensiune (cardul: „>1").
MAX_DIMENSION_SPREAD = 1.0


class WebRubricScores(BaseModel):
    """Rubrica umană, ancore publicate 1-5. `extra="forbid"`: o dimensiune inventată de un
    evaluator n-ar avea ancoră, deci n-ar fi comparabilă între oameni."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    naturalness: float = Field(ge=1, le=5)
    helpfulness: float = Field(ge=1, le=5)
    trust: float = Field(ge=1, le=5)
    no_overtalk: float = Field(ge=1, le=5)
    context_handling: float = Field(ge=1, le=5)

    @property
    def overall(self) -> float:
        return mean(self.model_dump().values())

    def spread(self, other: WebRubricScores) -> dict[str, float]:
        mine, theirs = self.model_dump(), other.model_dump()
        return {k: abs(mine[k] - theirs[k]) for k in mine}


class JourneyPacket(BaseModel):
    """Ce vede evaluatorul: starea vizibilă unui shopper + două variante, fără nicio etichetă.

    Nu conține: `release_sha`, `model_id`, `pipeline_version`, care variantă e candidate, latență,
    cost. Toate acestea trăiesc în cheia de dezvăluire, care se deschide DUPĂ ratings.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    pair_id: str
    journey_id: str
    family: str
    transcript_a: tuple[str, ...]
    transcript_b: tuple[str, ...]


class RevealKey(BaseModel):
    """Cheia: ce era A și ce era B. Sigilată până la finalul ratings."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    pair_id: str
    journey_id: str
    candidate_side: Side
    champion_release: str = ""
    candidate_release: str = ""


class Rating(BaseModel):
    """Verdictul UNUI evaluator pe UN pachet."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    pair_id: str
    evaluator_id: str = Field(min_length=1, max_length=32)
    winner: Winner
    reason: str
    scores_a: WebRubricScores
    scores_b: WebRubricScores

    @property
    def reason_ok(self) -> bool:
        return self.reason in CHOICE_REASONS


class BlindLeak(ValueError):
    """Pachetul ar dezvălui care variantă e candidate. Artefact invalid, rerun orb."""


_LABEL_TOKENS = ("candidate", "champion", "release", "model_id", "gpt-", "claude-", "canary")


def assert_blind(packet: JourneyPacket) -> None:
    """Refuză un pachet care poartă etichete. Failure matrix: „evaluator vede labelul candidate
    ⇒ artifact invalid; rerun blind" — mai bine refuzat la emitere decât descoperit după rating."""
    blob = json.dumps(packet.model_dump(mode="json"), ensure_ascii=False).lower()
    hit = [token for token in _LABEL_TOKENS if token in blob]
    if hit:
        raise BlindLeak(f"pachetul conține etichete de release: {hit}")


def _safe(text: str) -> str:
    """Fail-closed pe PII, ca `nx210_blind._safe_report_text`: artefactele ajung în review."""
    return "[REDACTED]" if contains_pii(text) else text


def build_packets(
    cases: list[tuple[str, str, tuple[str, ...], tuple[str, ...]]],
    *,
    seed: str,
    champion_release: str = "",
    candidate_release: str = "",
) -> tuple[tuple[JourneyPacket, ...], tuple[RevealKey, ...]]:
    """`(journey_id, family, transcript_champion, transcript_candidate)` → pachete + chei.

    Randomizarea laturii e DETERMINISTĂ din `seed` (ca la NX-210): aceeași suită dă aceeași
    repartiție, deci rularea se poate reproduce și contesta. Un `random()` real ar face imposibil
    de verificat că ordinea n-a fost aleasă după ce s-au văzut rezultatele.
    """
    packets: list[JourneyPacket] = []
    keys: list[RevealKey] = []
    for journey_id, family, champion, candidate in cases:
        pair_id = hashlib.sha256(f"{seed}:pair:{journey_id}".encode()).hexdigest()[:16]
        side: Side = (
            "A"
            if hashlib.sha256(f"{seed}:side:{journey_id}".encode()).digest()[0] % 2 == 0
            else "B"
        )
        safe_champion = tuple(_safe(t) for t in champion)
        safe_candidate = tuple(_safe(t) for t in candidate)
        a, b = (safe_candidate, safe_champion) if side == "A" else (safe_champion, safe_candidate)
        packet = JourneyPacket(
            pair_id=pair_id,
            journey_id=journey_id,
            family=family,
            transcript_a=a,
            transcript_b=b,
        )
        assert_blind(packet)
        packets.append(packet)
        keys.append(
            RevealKey(
                pair_id=pair_id,
                journey_id=journey_id,
                candidate_side=side,
                champion_release=champion_release,
                candidate_release=candidate_release,
            )
        )
    return tuple(packets), tuple(keys)


# ── Adjudecare + agregare ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Adjudication:
    """De ce o pereche are nevoie de al treilea evaluator."""

    pair_id: str
    reason: str


def needs_adjudication(ratings: list[Rating]) -> Adjudication | None:
    """Doi evaluatori, un verdict? `None` = de acord. Regula e cea din card, literal.

    Două declanșatoare, ambele necesare: câștigători diferiți (dezacord de fond) SAU o dimensiune
    cu diferență >1 punct (dezacord de calibrare — chiar dacă au ales același câștigător, nu văd
    același lucru, iar mediile lor n-ar trebui combinate fără arbitraj).
    """
    if len(ratings) < 2:
        return Adjudication(ratings[0].pair_id if ratings else "?", "sub doi evaluatori")
    first, second = ratings[0], ratings[1]
    if first.winner != second.winner:
        return Adjudication(first.pair_id, "winner_disagreement")
    for dim, delta in first.scores_a.spread(second.scores_a).items():
        if delta > MAX_DIMENSION_SPREAD:
            return Adjudication(first.pair_id, f"spread:{dim}")
    for dim, delta in first.scores_b.spread(second.scores_b).items():
        if delta > MAX_DIMENSION_SPREAD:
            return Adjudication(first.pair_id, f"spread:{dim}")
    return None


def win_score(winner: Winner, candidate_side: Side) -> float:
    """`1` victorie, `0,5` egal, `0` înfrângere — pentru candidate. Formula din card."""
    if winner == "tie":
        return 0.5
    return 1.0 if winner == candidate_side else 0.0


def bootstrap_ci(
    values: list[float], *, confidence: float = 0.95, samples: int = 5_000, seed: int = 246
) -> tuple[float, float]:
    """Interval bootstrap pe media scorurilor pairwise (aceeași metodă ca `nx210_blind`).

    Determinist prin `seed`: raportul trebuie să poată fi recalculat identic de altcineva.
    """
    if not values:
        raise ValueError("bootstrap fără observații")
    rng = random.Random(seed)
    estimates = sorted(mean(rng.choice(values) for _ in values) for _ in range(samples))
    alpha = (1 - confidence) / 2
    low = estimates[max(0, int(alpha * samples))]
    high = estimates[min(samples - 1, int((1 - alpha) * samples))]
    return low, high


@dataclass
class PairwiseResult:
    """Agregatul. Poartă și lucrurile care INVALIDEAZĂ un rezultat, nu doar scorul."""

    n: int = 0
    wins: int = 0
    ties: int = 0
    losses: int = 0
    score: float | None = None
    ci_low: float | None = None
    ci_high: float | None = None
    order_bias: float | None = None
    disagreement_rate: float | None = None
    adjudicated: tuple[str, ...] = ()
    by_family: dict[str, dict[str, Any]] = field(default_factory=dict)
    rubric_means: dict[str, float] = field(default_factory=dict)
    rubric_by_family: dict[str, dict[str, float]] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "wins": self.wins,
            "ties": self.ties,
            "losses": self.losses,
            "pairwise_score": None if self.score is None else round(self.score, 4),
            "confidence_interval_95": (
                None
                if self.ci_low is None
                else [round(self.ci_low, 4), round(self.ci_high or 0.0, 4)]
            ),
            "order_bias": None if self.order_bias is None else round(self.order_bias, 4),
            "disagreement_rate": (
                None if self.disagreement_rate is None else round(self.disagreement_rate, 4)
            ),
            "adjudicated": list(self.adjudicated),
            "by_family": {k: self.by_family[k] for k in sorted(self.by_family)},
            "rubric_means": {k: round(v, 3) for k, v in sorted(self.rubric_means.items())},
            "rubric_by_family": {
                fam: {k: round(v, 3) for k, v in sorted(dims.items())}
                for fam, dims in sorted(self.rubric_by_family.items())
            },
        }


def aggregate(
    ratings: list[Rating],
    keys: list[RevealKey],
    families: dict[str, str],
    *,
    seed: int = 246,
) -> PairwiseResult:
    """Ratings + cheia de dezvăluire → agregat, cu order bias și acord.

    Dezvăluirea se face AICI, adică după ce ratings există. Ordinea nu e o formalitate: dacă
    laturile s-ar cunoaște înainte, nimic din ce urmează n-ar mai însemna ceva.
    """
    by_pair: dict[str, list[Rating]] = {}
    for r in ratings:
        by_pair.setdefault(r.pair_id, []).append(r)
    key_by_pair = {k.pair_id: k for k in keys}

    result = PairwiseResult()
    scores: list[float] = []
    a_wins = 0
    decided = 0
    disagreements = 0
    adjudicated: list[str] = []
    rubric_sums: dict[str, list[float]] = {d: [] for d in RUBRIC_DIMENSIONS}
    fam_scores: dict[str, list[float]] = {}
    fam_rubric: dict[str, dict[str, list[float]]] = {}

    for pair_id, pair_ratings in sorted(by_pair.items()):
        key = key_by_pair.get(pair_id)
        if key is None:
            continue  # rating fără cheie: pachet străin, nu se numără (nici nu se ridică)
        adjudication = needs_adjudication(pair_ratings)
        if adjudication is not None:
            disagreements += 1
            adjudicated.append(pair_id)
            if len(pair_ratings) < 3:
                # Fără al treilea evaluator, perechea NU intră în scor. A o include cu media a doi
                # oameni care nu sunt de acord ar fabrica o observație.
                continue
        winner = _majority_winner(pair_ratings)
        scores.append(win_score(winner, key.candidate_side))
        if winner != "tie":
            decided += 1
            a_wins += 1 if winner == "A" else 0
        family = families.get(key.journey_id, "unknown")
        fam_scores.setdefault(family, []).append(win_score(winner, key.candidate_side))
        for r in pair_ratings:
            cand = r.scores_a if key.candidate_side == "A" else r.scores_b
            for dim, value in cand.model_dump().items():
                rubric_sums[dim].append(value)
                fam_rubric.setdefault(family, {}).setdefault(dim, []).append(value)

    result.n = len(scores)
    result.wins = sum(1 for s in scores if s == 1.0)
    result.ties = sum(1 for s in scores if s == 0.5)
    result.losses = sum(1 for s in scores if s == 0.0)
    result.adjudicated = tuple(adjudicated)
    result.disagreement_rate = (disagreements / len(by_pair)) if by_pair else None
    if scores:
        result.score = mean(scores)
        result.ci_low, result.ci_high = bootstrap_ci(scores, seed=seed)
    if decided:
        # 0,5 = fără bias. Abaterea de la 0,5 e cât de mult contează POZIȚIA, nu conținutul.
        result.order_bias = a_wins / decided
    result.rubric_means = {d: mean(v) for d, v in rubric_sums.items() if v}
    result.by_family = {
        fam: {"n": len(v), "pairwise_score": round(mean(v), 4)} for fam, v in fam_scores.items()
    }
    result.rubric_by_family = {
        fam: {d: mean(vals) for d, vals in dims.items() if vals} for fam, dims in fam_rubric.items()
    }
    return result


def _majority_winner(ratings: list[Rating]) -> Winner:
    """Câștigătorul, prin majoritate. Egalitate de voturi ⇒ `tie` — nu alegem noi în locul lor."""
    tally: dict[Winner, int] = {"A": 0, "B": 0, "tie": 0}
    for r in ratings:
        tally[r.winner] += 1
    best = max(tally.values())
    winners = [w for w, count in tally.items() if count == best]
    return winners[0] if len(winners) == 1 else "tie"


__all__ = [
    "CHOICE_REASONS",
    "RUBRIC_DIMENSIONS",
    "Adjudication",
    "BlindLeak",
    "JourneyPacket",
    "PairwiseResult",
    "Rating",
    "RevealKey",
    "WebRubricScores",
    "aggregate",
    "assert_blind",
    "bootstrap_ci",
    "build_packets",
    "needs_adjudication",
    "win_score",
]
