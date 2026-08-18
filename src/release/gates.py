"""NX-249 — porțile de promovare: ce trebuie dovedit ca o etapă să poată avansa.

Trei familii de porți, evaluate în ordinea în care contează:

  1. **Hard stops** — toleranță ZERO. Un singur leak cross-tenant, un preț inventat, un receipt
     fals sau un terminal gol oprește progresia indiferent de orice scor pozitiv. Nu există
     medie care să compenseze un invariant încălcat.
  2. **Etapă** — timp ȘI eșantion. Amândouă, nu oricare. La trafic mic verdictul e `INSUFFICIENT`
     și etapa se prelungește; nu se promovează pe timp scurs cu 12 conversații.
  3. **Statistice** — non-inferioritate cu interval, pe cohorturi comparabile. Cu marjă
     predeclarată, cu `n` publicat, și cu regula că un eșantion mic nu decide.

## De ce patru verdicte

`PASS` / `FAIL` / `INSUFFICIENT` / `UNKNOWN`. Ultimele două nu sunt sinonime pentru „nu":
`INSUFFICIENT` = am măsurat, dar prea puțin; `UNKNOWN` = n-am putut măsura (artefact lipsă,
fereastră care nu se potrivește, denominator zero). Prima se rezolvă așteptând, a doua reparând
instrumentul. Confundate, ambele devin „mai încercăm o dată" — patologia pe care NX-238 și NX-246
au numit-o deja.

## Ce NU face modulul

Nu promovează. `evaluate` întoarce un raport; `PASS` e o constatare, nu o comandă. Mutarea
traficului cere `DecisionRecord` + `scripts/release_control.py apply` + aprobare umană. Cardul e
explicit: **nu există auto-promotion**, iar aici asta e o proprietate structurală — modulul nu are
acces la storeul de policy.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from src.release.models import (
    STAGES_BY_INDEX,
    ReleasePolicy,
    RolloutStage,
    stage_for,
)

VERDICT_PASS = "PASS"
VERDICT_FAIL = "FAIL"
VERDICT_INSUFFICIENT = "INSUFFICIENT"
VERDICT_UNKNOWN = "UNKNOWN"

#: Ordinea de PESIMISM: verdictul agregat e cel mai rău dintre porți. Un `FAIL` nu se mediază cu
#: trei `PASS`-uri, iar un `UNKNOWN` nu devine verde fiindcă restul sunt verzi.
_SEVERITY = (VERDICT_FAIL, VERDICT_UNKNOWN, VERDICT_INSUFFICIENT, VERDICT_PASS)


def worst(verdicts: list[str]) -> str:
    for v in _SEVERITY:
        if v in verdicts:
            return v
    return VERDICT_UNKNOWN


# ── Hard stops ──────────────────────────────────────────────────────────────────────────────
#: Vocabular ÎNCHIS. Un hard stop care nu e aici nu se poate raporta — și asta e intenția: lista
#: se extinde printr-un PR, nu printr-un string liber scris în grabă într-un incident.
HARD_STOPS: tuple[str, ...] = (
    "cross_tenant_leak",
    "authorization_bypass",
    "secret_or_pii_leak",
    "invented_fact",  # preț/stoc/link/variantă inventate sau hard constraint încălcat
    "empty_terminal",  # terminal gol / tăcere
    "result_misattribution",  # rezultat atașat altui turn
    "duplicate_execution",  # dublu LLM / dublu side effect la replay
    "false_receipt",
    "state_corruption",
    "artifact_mismatch",  # digest/semnătură/schemă/evidence tampering
    "slo_fast_burn",
    "rollback_impossible",
)
HARD_STOP_SET = frozenset(HARD_STOPS)


@dataclass
class GateResult:
    """O poartă: ce a verificat, pe ce numere, cu ce verdict și de ce.

    `note` nu e decor: un `FAIL` fără motiv reproductibil produce o ședință, nu o reparație.
    """

    name: str
    verdict: str
    note: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "gate": self.name,
            "verdict": self.verdict,
            "note": self.note,
            **({"detail": self.detail} if self.detail else {}),
        }


@dataclass(frozen=True, slots=True)
class CohortStats:
    """Agregatele UNUI cohort, calculate din faptele de ledger. Fără conținut, fără ID-uri."""

    track: str
    turns: int = 0
    terminal: int = 0
    completed: int = 0
    renderable_terminal: int = 0
    failed: int = 0
    first_attempt_ok: int = 0
    durations_ms: tuple[float, ...] = ()
    action_turns: int = 0
    action_failed: int = 0

    @property
    def terminal_rate(self) -> float | None:
        return None if self.turns == 0 else self.terminal / self.turns

    @property
    def failure_rate(self) -> float | None:
        return None if self.terminal == 0 else self.failed / self.terminal

    def as_dict(self) -> dict[str, Any]:
        return {
            "track": self.track,
            "turns": self.turns,
            "terminal": self.terminal,
            "completed": self.completed,
            "failed": self.failed,
            "renderable_terminal": self.renderable_terminal,
            "first_attempt_ok": self.first_attempt_ok,
            "action_turns": self.action_turns,
            "action_failed": self.action_failed,
            "latency_ms": percentiles(list(self.durations_ms)),
        }


def percentiles(values: list[float], points: tuple[int, ...] = (50, 90, 95)) -> dict[str, float]:
    """Percentile prin interpolare liniară — aceeași metodă ca `observability/slo.py`.

    Reimplementate acolo dintr-un motiv de reproductibilitate (independență de versiunea de
    Postgres); reluate aici cu ACELEAȘI reguli, ca două rapoarte pe aceleași date să nu difere
    în p90 din cauza unei convenții de rotunjire.
    """
    if not values:
        return {}
    ordered = sorted(values)
    out: dict[str, float] = {}
    for p in points:
        if len(ordered) == 1:
            out[f"p{p}"] = round(ordered[0], 1)
            continue
        rank = (p / 100.0) * (len(ordered) - 1)
        low, high = math.floor(rank), math.ceil(rank)
        value = (
            ordered[low]
            if low == high
            else ordered[low] + (ordered[high] - ordered[low]) * (rank - low)
        )
        out[f"p{p}"] = round(value, 1)
    return out


def wilson_interval(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Interval Wilson 95%. NU Wald.

    Motivul e același ca la NX-246 felia 2: cu 10 din 10, Wald raportează „între 100% și 100%",
    ceea ce e o afirmație despre aritmetică, nu despre lume. Wilson rămâne onest la eșantioane
    mici — exact regimul în care se ia decizia de promovare la 5%.
    """
    if n <= 0:
        return (0.0, 1.0)
    p = successes / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n) / denom
    return (max(0.0, center - margin), min(1.0, center + margin))


# ── Porțile ─────────────────────────────────────────────────────────────────────────────────
def gate_hard_stops(observed: list[str]) -> GateResult:
    """Toleranță zero. Un cod necunoscut e tot `FAIL`: nu ignorăm ce nu înțelegem."""
    if not observed:
        return GateResult("hard_stops", VERDICT_PASS, "niciun hard stop raportat")
    unknown = [c for c in observed if c not in HARD_STOP_SET]
    return GateResult(
        "hard_stops",
        VERDICT_FAIL,
        f"{len(observed)} hard stop(uri): {', '.join(sorted(set(observed)))}",
        {"codes": sorted(set(observed)), "unknown_codes": sorted(set(unknown))},
    )


def gate_stage_window(
    stage: RolloutStage, *, elapsed_hours: float, candidate_turns: int
) -> GateResult:
    """Timp ȘI eșantion. Lipsa oricăruia dă `INSUFFICIENT`, nu `FAIL`.

    Distincția contează operațional: `INSUFFICIENT` înseamnă „mai lasă-l", `FAIL` înseamnă
    „oprește-l". Un gate care le confundă va fi ignorat după a treia alarmă falsă.
    """
    missing = []
    if elapsed_hours < stage.min_hours:
        missing.append(f"timp {elapsed_hours:.1f}h < {stage.min_hours:.0f}h")
    if candidate_turns < stage.min_candidate_turns:
        missing.append(f"eșantion {candidate_turns} < {stage.min_candidate_turns}")
    detail = {
        "stage": stage.label,
        "elapsed_hours": round(elapsed_hours, 2),
        "candidate_turns": candidate_turns,
        "min_hours": stage.min_hours,
        "min_candidate_turns": stage.min_candidate_turns,
    }
    if missing:
        return GateResult("stage_window", VERDICT_INSUFFICIENT, "; ".join(missing), detail)
    return GateResult("stage_window", VERDICT_PASS, "timp și eșantion îndeplinite", detail)


def gate_non_inferiority(
    *,
    name: str,
    candidate_bad: int,
    candidate_n: int,
    control_bad: int,
    control_n: int,
    margin_pp: float,
    min_n: int = 30,
) -> GateResult:
    """Candidate nu e mai rău decât control cu mai mult de `margin_pp` puncte procentuale.

    Se compară RATE, nu numere absolute, iar limita se ia din intervalul Wilson al candidateului,
    nu din estimarea punctuală: la 200 de ture, o diferență de 3pp poate fi zgomot, iar a o trata
    ca semnal ar bloca releaseuri bune la fel de des cum ar lăsa să treacă unele proaste.

    Eșantion insuficient în ORICARE cohort ⇒ `INSUFFICIENT`. Cardul e explicit: „sample insuficient
    nu decide".
    """
    detail = {
        "candidate": {"bad": candidate_bad, "n": candidate_n},
        "control": {"bad": control_bad, "n": control_n},
        "margin_pp": margin_pp,
    }
    if candidate_n < min_n or control_n < min_n:
        return GateResult(
            name,
            VERDICT_INSUFFICIENT,
            f"eșantion sub {min_n} (candidate={candidate_n}, control={control_n})",
            detail,
        )
    c_rate = candidate_bad / candidate_n
    k_rate = control_bad / control_n
    c_low, c_high = wilson_interval(candidate_bad, candidate_n)
    detail["candidate"]["rate"] = round(c_rate, 4)
    detail["control"]["rate"] = round(k_rate, 4)
    detail["candidate"]["ci95"] = [round(c_low, 4), round(c_high, 4)]
    limit = k_rate + margin_pp / 100.0
    detail["limit"] = round(limit, 4)
    if c_high <= limit:
        return GateResult(name, VERDICT_PASS, "non-inferior cu marja predeclarată", detail)
    if c_rate > limit:
        return GateResult(
            name,
            VERDICT_FAIL,
            f"rata candidate {c_rate:.3f} depășește limita {limit:.3f}",
            detail,
        )
    # Estimarea punctuală trece, dar intervalul nu exclude o regresie peste marjă.
    return GateResult(
        name,
        VERDICT_INSUFFICIENT,
        f"intervalul candidate ({c_high:.3f}) nu exclude o regresie peste {limit:.3f}",
        detail,
    )


def gate_artifact_match(
    policy: ReleasePolicy,
    *,
    quality_packet_hash: str,
    e2e_packet_hash: str,
    deploy_manifest_hash: str,
) -> GateResult:
    """Artefactele raportului sunt EXACT cele pe care policy-ul le-a autorizat.

    Fără poarta asta, un raport verde de acum trei zile ar putea promova digestul de azi. Cardul o
    numește „report combină alt digest/window → artifact mismatch → FAIL/UNKNOWN"; aici lipsa e
    `UNKNOWN` (n-am putut compara) și nepotrivirea e `FAIL` (am comparat și diferă).
    """
    actual = {
        "quality_packet_hash": quality_packet_hash,
        "e2e_packet_hash": e2e_packet_hash,
        "deploy_manifest_hash": deploy_manifest_hash,
    }
    expected = {
        "quality_packet_hash": policy.quality_packet_hash,
        "e2e_packet_hash": policy.e2e_packet_hash,
        "deploy_manifest_hash": policy.deploy_manifest_hash,
    }
    missing = [k for k, v in actual.items() if not str(v).strip()]
    if missing:
        return GateResult(
            "artifact_match",
            VERDICT_UNKNOWN,
            f"artefacte absente: {', '.join(sorted(missing))}",
            {"expected": expected},
        )
    mismatched = [k for k in actual if actual[k] != expected[k]]
    if mismatched:
        return GateResult(
            "artifact_match",
            VERDICT_FAIL,
            f"artefacte de la alt release: {', '.join(sorted(mismatched))}",
            {"mismatched": sorted(mismatched)},
        )
    return GateResult("artifact_match", VERDICT_PASS, "artefactele corespund policy-ului")


def gate_upstream(name: str, verdict: str | None, *, source: str) -> GateResult:
    """Un verdict venit din alt card (NX-246 SLO/quality, NX-247 E2E, NX-248 evidence).

    Traducerea e deliberat CONSERVATOARE: orice valoare pe care n-o recunoaștem devine `UNKNOWN`,
    nu `PASS`. Un artefact cu un vocabular schimbat trebuie să oprească promovarea, nu să treacă
    fiindcă string-ul nu se potrivea cu niciun eșec cunoscut.
    """
    if verdict is None:
        return GateResult(name, VERDICT_UNKNOWN, f"{source}: artefact absent")
    v = str(verdict).strip().upper().replace("-", "_")
    if v in ("PASS", "READY", "GO", "OK"):
        return GateResult(name, VERDICT_PASS, f"{source}: {verdict}")
    if v in ("FAIL", "BLOCKED", "NO_GO"):
        return GateResult(name, VERDICT_FAIL, f"{source}: {verdict}")
    if v in ("INSUFFICIENT", "INSUFFICIENT_SAMPLE"):
        return GateResult(name, VERDICT_INSUFFICIENT, f"{source}: {verdict}")
    return GateResult(name, VERDICT_UNKNOWN, f"{source}: {verdict}")


def gate_completeness(
    *, cohort_stats: dict[str, CohortStats], truncated: bool, unknown_turns: int
) -> GateResult:
    """Avem destule date ca verdictul să însemne ceva?

    Trei feluri de „nu": fereastră trunchiată (am văzut o parte), cohort lipsă (n-avem cu ce
    compara), ture fără captură (nu știm în ce cohort intră). Toate dau `UNKNOWN` — și toate se
    RAPORTEAZĂ, fiindcă un raport care tace despre lipsuri minte prin omisiune.
    """
    detail = {
        "truncated": truncated,
        "unknown_turns": unknown_turns,
        "tracks": sorted(cohort_stats),
    }
    if truncated:
        return GateResult(
            "completeness", VERDICT_UNKNOWN, "fereastră trunchiată — date parțiale", detail
        )
    if "candidate" not in cohort_stats or "champion" not in cohort_stats:
        return GateResult(
            "completeness",
            VERDICT_UNKNOWN,
            "lipsește un cohort — nu există comparație",
            detail,
        )
    total = sum(c.turns for c in cohort_stats.values())
    if total and unknown_turns / total > 0.05:
        return GateResult(
            "completeness",
            VERDICT_UNKNOWN,
            f"{unknown_turns}/{total} ture fără captură de release (>5%)",
            detail,
        )
    return GateResult("completeness", VERDICT_PASS, "cohorturi complete", detail)


# ── Agregarea ───────────────────────────────────────────────────────────────────────────────
@dataclass
class GateReport:
    """Verdictul de etapă + toate porțile, ca să poată fi contestat punct cu punct."""

    stage: str
    gates: list[GateResult] = field(default_factory=list)

    @property
    def verdict(self) -> str:
        return worst([g.verdict for g in self.gates])

    @property
    def blocking(self) -> list[str]:
        return [g.name for g in self.gates if g.verdict != VERDICT_PASS]

    def as_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "verdict": self.verdict,
            "blocking": self.blocking,
            "gates": [g.as_dict() for g in self.gates],
            "promotion": (
                "PASS nu promovează singur: cere DecisionRecord + apply cu expected revision"
            ),
        }


def next_stage(policy: ReleasePolicy) -> RolloutStage | None:
    """Etapa care urmează. `None` = policy-ul e deja la ultima (7 — close v1).

    Progresia e cu pas fix tocmai ca eșantionul minim să însemne ceva: de la 5% se merge la 20%,
    nu la 7% fiindcă „pare sigur" (un 7% nici nu poate exista — validarea policy-ului îl refuză).
    """
    return STAGES_BY_INDEX.get(stage_for(policy).index + 1)
