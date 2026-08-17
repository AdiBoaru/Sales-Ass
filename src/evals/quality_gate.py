"""NX-246 (felia 3) — poarta de calitate: determinist ÎNAINTE de stil, fail-closed peste tot.

Ordinea e întregul design. Mai întâi se verifică DETERMINIST fiecare tur (schema ViewModel,
terminal non-empty, grounding, hard constraints, safety, referințe, scope de acțiune, PII). Abia
dacă totul trece, are sens să întrebi oameni dacă răspunsul sună bine.

Motivul e că altfel un judge poate compensa o halucinație cu un scor bun de ton — și exact asta
s-ar întâmpla, fiindcă un text fluent care inventează un preț citește mai bine decât unul onest
care spune „nu știu". Cardul o cere explicit: „un judge nu poate compensa o halucinație cu un scor
bun de ton". Aici e impus de cod: `deterministic.passed=False` ⇒ verdict `FAIL`, indiferent de
rubrici, indiferent de pairwise, fără drept de apel.

**Toate pragurile sunt PREÎNREGISTRATE** în `GatePolicy`, cu amprentă în raport. Se schimbă doar
printr-un PR de policy separat, ÎNAINTE de rularea candidateului. Un prag mutat după ce s-au văzut
cifrele nu e un prag, e o justificare.

**Datele lipsă produc FAIL, nu skip.** Un gate care trece pentru că n-a găsit holdoutul e un gate
fail-open — adică opusul unui gate.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from src.evals.pairwise import RUBRIC_DIMENSIONS, PairwiseResult
from src.evals.web_journeys import Coverage, SealCheck

VERDICT_PASS = "PASS"
VERDICT_FAIL = "FAIL"
VERDICT_INSUFFICIENT = "INSUFFICIENT"
#: Distinct de `FAIL`: infrastructura e gata, dar corpusul/holdoutul nu există încă. Un `FAIL` ar
#: sugera că am măsurat și candidateul a pierdut; `NOT-READY` spune adevărul — n-am măsurat.
#: (Același vocabular ca verdictul NX-238 pentru portul de retrieval.)
VERDICT_NOT_READY = "NOT-READY"


class GatePolicy(BaseModel):
    """Pragurile, înghețate. Amprenta lor intră în fiecare raport."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str = Field(default="nx246-gate-v1")
    #: Rubrica: media per dimensiune pe holdout.
    min_rubric_mean: float = Field(default=4.0, ge=1, le=5)
    #: Niciun cohort (familie) obligatoriu sub asta. Mai jos decât media globală, deliberat: o
    #: familie grea are voie să fie mai slabă, dar nu prăbușită.
    min_cohort_mean: float = Field(default=3.8, ge=1, le=5)
    #: `win + 0,5×tie`.
    min_pairwise_score: float = Field(default=0.55, gt=0.5, le=1)
    #: Limita inferioară bootstrap 95% nu are voie să coboare sub „la fel de bun ca champion".
    min_pairwise_ci_low: float = Field(default=0.50, ge=0, le=1)
    #: Overtalk sever (rubrica `no_overtalk` ≤ 2) — proporție maximă.
    max_severe_overtalk_ratio: float = Field(default=0.05, ge=0, le=1)
    #: Regresie maximă per cohort față de champion, în puncte procentuale de pairwise.
    max_cohort_regression_pp: float = Field(default=5.0, ge=0)
    #: Sub atâtea perechi evaluate, verdictul e `INSUFFICIENT` (nu `FAIL`, nu `PASS`).
    min_pairs: int = Field(default=40, ge=10)
    #: Order bias tolerat: `|p(A câștigă) − 0,5|`.
    max_order_bias: float = Field(default=0.10, ge=0, le=0.5)
    #: Dezacord tolerat între evaluatori înainte ca raportul să fie considerat necalibrat.
    max_disagreement_rate: float = Field(default=0.30, ge=0, le=1)

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()


#: Verificările deterministe, ca vocabular ÎNCHIS. Fiecare e o poartă cu toleranță ZERO — nu o
#: notă. Numele intră în raport, deci trebuie să fie stabile.
DETERMINISTIC_CHECKS: tuple[str, ...] = (
    "viewmodel_schema",  # payload-ul respectă `web-view.v2`
    "non_empty_terminal",  # P6: cel puțin un bloc randabil
    "grounding",  # fiecare preț/stoc/link vine din fapte (NX-240)
    "hard_constraints",  # buget/categorie/variantă respectate; UNKNOWN ≠ MISMATCH
    "safety",  # contraindicații NX-173
    "state_delta",  # nevoile revocate nu reînvie
    "reference_resolution",  # „primul"/„acesta" se leagă de ce vede clientul
    "action_scope",  # nicio acțiune în afara celor permise de journey
    "receipt",  # mutațiile au receipt idempotent
    "no_pii",  # nimic din NX-230 nu apare în răspuns
)


@dataclass
class DeterministicResult:
    """Rezultatul porții deterministe pe toată suita. `by_check` numără VIOLĂRILE, nu trecerile:
    un raport în care scrie `grounding: 0` se citește corect chiar și de cine se grăbește."""

    turns_checked: int = 0
    by_check: dict[str, int] = field(default_factory=dict)
    failing_journeys: tuple[str, ...] = ()

    @property
    def violations(self) -> int:
        return sum(self.by_check.values())

    @property
    def passed(self) -> bool:
        return self.turns_checked > 0 and self.violations == 0

    def record(self, check: str, journey_id: str) -> None:
        if check not in DETERMINISTIC_CHECKS:
            raise ValueError(f"verificare deterministă necunoscută: {check!r}")
        self.by_check[check] = self.by_check.get(check, 0) + 1
        if journey_id not in self.failing_journeys:
            self.failing_journeys = (*self.failing_journeys, journey_id)

    def as_dict(self) -> dict[str, Any]:
        return {
            "turns_checked": self.turns_checked,
            "violations": self.violations,
            "by_check": dict(sorted(self.by_check.items())),
            "failing_journeys": list(self.failing_journeys[:20]),
            "passed": self.passed,
        }


@dataclass
class GateReport:
    """Verdictul + tot ce trebuie ca să poată fi contestat."""

    verdict: str
    policy_fingerprint: str
    reasons: tuple[str, ...] = ()
    deterministic: DeterministicResult | None = None
    pairwise: PairwiseResult | None = None
    dev_coverage: Coverage | None = None
    holdout_coverage: Coverage | None = None
    seal: SealCheck | None = None
    champion_release: str = ""
    candidate_release: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "policy_fingerprint": self.policy_fingerprint,
            "reasons": list(self.reasons),
            "champion_release": self.champion_release,
            "candidate_release": self.candidate_release,
            "deterministic": None if self.deterministic is None else self.deterministic.as_dict(),
            "pairwise": None if self.pairwise is None else self.pairwise.as_dict(),
            "dev_coverage": None if self.dev_coverage is None else self.dev_coverage.as_dict(),
            "holdout_coverage": (
                None if self.holdout_coverage is None else self.holdout_coverage.as_dict()
            ),
            "seal": (
                None if self.seal is None else {"ok": self.seal.ok, "reason": self.seal.reason}
            ),
        }


def evaluate_gate(
    *,
    policy: GatePolicy,
    seal: SealCheck | None,
    dev_coverage: Coverage | None,
    holdout_coverage: Coverage | None,
    deterministic: DeterministicResult | None,
    pairwise: PairwiseResult | None,
    severe_overtalk_ratio: float | None = None,
    champion_by_family: dict[str, float] | None = None,
    champion_release: str = "",
    candidate_release: str = "",
) -> GateReport:
    """Toate condițiile din card, în ordinea în care contează. Pur și determinist.

    Ordinea de evaluare nu e cosmetică. Se verifică întâi dacă avem DREPTUL să emitem un verdict
    (sigiliu, acoperire), apoi FAPTELE (determinist), abia apoi STILUL (rubrici, pairwise). Un gate
    care ar începe cu rubricile ar putea raporta „scoruri excelente" despre o suită care nu există.
    """
    report = GateReport(
        verdict=VERDICT_NOT_READY,
        policy_fingerprint=policy.fingerprint,
        deterministic=deterministic,
        pairwise=pairwise,
        dev_coverage=dev_coverage,
        holdout_coverage=holdout_coverage,
        seal=seal,
        champion_release=champion_release,
        candidate_release=candidate_release,
    )

    # ── 1. Avem dreptul să emitem un verdict? ───────────────────────────────────────────────
    not_ready: list[str] = []
    if seal is None or not seal.ok:
        not_ready.append(f"holdout nesigilat: {seal.reason if seal else 'manifest absent'}")
    if dev_coverage is None:
        not_ready.append("corpus de development absent")
    elif not dev_coverage.complete:
        not_ready.extend(f"dev: {g}" for g in dev_coverage.gaps)
    if holdout_coverage is None:
        not_ready.append("acoperirea holdoutului necunoscută")
    elif not holdout_coverage.complete:
        not_ready.extend(f"holdout: {g}" for g in holdout_coverage.gaps)
    if not_ready:
        # NOT-READY, nu FAIL: n-am măsurat, deci nu putem spune că a picat cineva.
        report.verdict = VERDICT_NOT_READY
        report.reasons = tuple(not_ready)
        return report

    # ── 2. Faptele. Toleranță ZERO, înaintea oricărei note de stil. ─────────────────────────
    if deterministic is None or deterministic.turns_checked == 0:
        report.verdict = VERDICT_NOT_READY
        report.reasons = ("nicio verificare deterministă rulată",)
        return report
    if not deterministic.passed:
        report.verdict = VERDICT_FAIL
        report.reasons = tuple(
            f"determinist: {check}×{n}" for check, n in sorted(deterministic.by_check.items())
        )
        return report

    # ── 3. Eșantionul susține o decizie? ────────────────────────────────────────────────────
    if pairwise is None or pairwise.n < policy.min_pairs:
        report.verdict = VERDICT_INSUFFICIENT
        report.reasons = (f"perechi {0 if pairwise is None else pairwise.n}/{policy.min_pairs}",)
        return report

    # ── 4. Stilul + validitatea măsurătorii. ────────────────────────────────────────────────
    fails: list[str] = []
    for dim in RUBRIC_DIMENSIONS:
        value = pairwise.rubric_means.get(dim)
        if value is None:
            fails.append(f"rubrica {dim} lipsește")
        elif value < policy.min_rubric_mean:
            fails.append(f"{dim} {value:.2f} < {policy.min_rubric_mean}")
    for family, dims in sorted(pairwise.rubric_by_family.items()):
        for dim, value in sorted(dims.items()):
            if value < policy.min_cohort_mean:
                fails.append(f"cohort {family}.{dim} {value:.2f} < {policy.min_cohort_mean}")
    if (
        severe_overtalk_ratio is not None
        and severe_overtalk_ratio > policy.max_severe_overtalk_ratio
    ):
        fails.append(
            f"overtalk sever {severe_overtalk_ratio:.1%} > {policy.max_severe_overtalk_ratio:.0%}"
        )
    if pairwise.score is None or pairwise.score < policy.min_pairwise_score:
        fails.append(f"pairwise {pairwise.score} < {policy.min_pairwise_score}")
    if pairwise.ci_low is None or pairwise.ci_low < policy.min_pairwise_ci_low:
        fails.append(f"limita inferioară {pairwise.ci_low} < {policy.min_pairwise_ci_low}")
    if champion_by_family:
        for family, entry in sorted(pairwise.by_family.items()):
            before = champion_by_family.get(family)
            if before is None:
                continue
            drop = (before - entry["pairwise_score"]) * 100
            if drop > policy.max_cohort_regression_pp:
                fails.append(
                    f"regresie {family} {drop:.1f}pp > {policy.max_cohort_regression_pp}pp"
                )
    # Validitatea MĂSURĂTORII, nu a candidateului — dar tot blocantă: un rezultat obținut cu bias
    # de ordine sau cu evaluatori necalibrați nu susține o promovare.
    if pairwise.order_bias is not None and abs(pairwise.order_bias - 0.5) > policy.max_order_bias:
        fails.append(f"order bias {pairwise.order_bias:.2f} (tolerat ±{policy.max_order_bias})")
    if (
        pairwise.disagreement_rate is not None
        and pairwise.disagreement_rate > policy.max_disagreement_rate
    ):
        fails.append(
            f"dezacord {pairwise.disagreement_rate:.0%} > {policy.max_disagreement_rate:.0%}"
        )

    report.verdict = VERDICT_FAIL if fails else VERDICT_PASS
    report.reasons = tuple(fails)
    return report


__all__ = [
    "DETERMINISTIC_CHECKS",
    "VERDICT_FAIL",
    "VERDICT_INSUFFICIENT",
    "VERDICT_NOT_READY",
    "VERDICT_PASS",
    "DeterministicResult",
    "GatePolicy",
    "GateReport",
    "evaluate_gate",
]
