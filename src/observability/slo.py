"""NX-246 — `slo_policy.v1`: denominatorii sunt COD, nu o formulă scrisă de mână într-un panel.

Un dashboard în care fiecare panel își definește propriul „procent de succes" produce, inevitabil,
verdicte care nu se pot reproduce: cineva exclude timeouturile fiindcă „nu sunt vina noastră",
altcineva numără replay-urile ca requesturi noi, și amândoi au grafice verzi peste același
incident. Aici regula e inversă: **formula e una singură, versionată, testată**, iar raportul
spune din ce a fost calculată.

Trei decizii care fac diferența dintre un SLO util și unul decorativ:

1. **Lipsa datelor NU e verde.** Denominator zero, fereastră fără rânduri, set trunchiat, ceas
   sărit ⇒ `UNKNOWN` / `INSUFFICIENT`. Niciodată `PASS`. Cel mai periculos mod de a eșua nu e
   alarma falsă, ci tăcerea care seamănă cu sănătatea.

2. **Excluderile sunt EXPLICITE și raportate separat.** Input invalid, respingere de auth/origin,
   rate-limit și anulare de către utilizator nu se ascund în availability — dar nici nu dispar:
   apar cu numărul lor. Eșecul intern, timeoutul, bug-ul de exporter și eroarea de deploy rămân
   ÎN denominator, fiindcă exact ele sunt promisiunea pe care o facem.

3. **Latența se RAPORTEAZĂ până e ratificată.** NX-241 a propus pragurile (3s exact / 6s
   recomandare / 10s complex, global p90 < 12s); ratificarea lor e a acestui card și cere o
   fereastră reală de baseline, pe care încă nu o avem. Până atunci verdictul de latență e
   `UNKNOWN(unratified_threshold)`, nu un `PASS` obținut prin alegerea pragului după ce s-au
   văzut cifrele — exact ce interzice cardul.

Modulul e PUR: primește fapte, întoarce un raport. Fără DB, fără ceas, fără rețea — deci două
rulări pe aceleași date dau același verdict, iar testul de gate poate fi determinist.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

SLO_POLICY_VERSION = "slo_policy.v1"

#: Manifestul de bugete NX-241 din care provin pragurile de latență (trasabilitate: dacă manifestul
#: se schimbă, ratificarea trebuie refăcută — un prag moștenit tăcut e un prag inventat).
LATENCY_SOURCE_MANIFEST = "nx241.2026-08-16"

VERDICT_PASS = "PASS"
VERDICT_FAIL = "FAIL"
VERDICT_UNKNOWN = "UNKNOWN"
VERDICT_INSUFFICIENT = "INSUFFICIENT"

#: Sub atâtea eșantioane nu emitem verdict: un 100% din 3 ture nu spune nimic despre 99,5%.
MIN_SAMPLES = 30

#: Pragurile Stage 1. `ratified=False` = raportăm, nu judecăm (vezi docstring, decizia 3).
RATIFIED = False


@dataclass(frozen=True, slots=True)
class TurnFact:
    """Un rând de ledger REDUS la ce contează pentru SLI.

    Deliberat fără `response_json`, fără text, fără `conversation_id`: raportul de SLO nu are
    nevoie de conținut ca să numere, iar ce nu iese din DB nu poate ajunge într-un artefact de CI.
    `renderable` vine calculat în SQL exact din acest motiv (vezi `db/queries/web_turn_slo.py`).
    """

    status: str
    safe_error_code: str | None
    attempt: int
    accepted_at: datetime
    completed_at: datetime | None
    deadline_at: datetime | None
    renderable: bool

    @property
    def terminal(self) -> bool:
        return self.status in ("completed", "failed", "cancelled")

    @property
    def duration_ms(self) -> float | None:
        """Accept → terminal durabil. `None` dacă turul nu s-a terminat."""
        if self.completed_at is None:
            return None
        return (self.completed_at - self.accepted_at).total_seconds() * 1000.0


#: Coduri de eroare care înseamnă „clientul a renunțat / requestul n-a fost valid" — se raportează
#: SEPARAT. Nu sunt succese, dar nici promisiuni încălcate.
EXCLUDED_CODES: frozenset[str] = frozenset({"cancelled", "client_cancelled", "invalid_request"})


@dataclass
class SliResult:
    """Rezultatul UNUI indicator, cu tot ce trebuie ca să poată fi contestat: sursă, numărător,
    numitor, ce s-a exclus și de ce, pragul aplicat și verdictul."""

    name: str
    source: str  # "ledger" | "metrics"
    numerator: int = 0
    denominator: int = 0
    excluded: dict[str, int] = field(default_factory=dict)
    target: float | None = None
    verdict: str = VERDICT_UNKNOWN
    note: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def ratio(self) -> float | None:
        if self.denominator <= 0:
            return None
        return self.numerator / self.denominator

    def decide(self, *, min_samples: int = MIN_SAMPLES) -> None:
        """Verdictul, în ordinea în care contează: date lipsă → eșantion mic → prag."""
        if self.denominator <= 0:
            self.verdict = VERDICT_UNKNOWN
            self.note = self.note or "denominator zero (fără date în fereastră)"
            return
        if self.target is None:
            self.verdict = VERDICT_UNKNOWN
            self.note = self.note or "prag neratificat"
            return
        if self.denominator < min_samples:
            self.verdict = VERDICT_INSUFFICIENT
            self.note = self.note or f"eșantion {self.denominator} < {min_samples}"
            return
        ratio = self.ratio or 0.0
        self.verdict = VERDICT_PASS if ratio >= self.target else VERDICT_FAIL

    def as_dict(self) -> dict[str, Any]:
        ratio = self.ratio
        return {
            "name": self.name,
            "source": self.source,
            "numerator": self.numerator,
            "denominator": self.denominator,
            "ratio": None if ratio is None else round(ratio, 6),
            "target": self.target,
            "verdict": self.verdict,
            "excluded": dict(sorted(self.excluded.items())),
            "note": self.note,
            **({"extra": self.extra} if self.extra else {}),
        }


def percentiles(
    values: list[float], points: tuple[int, ...] = (50, 90, 95, 99)
) -> dict[str, float]:
    """Percentile prin interpolare liniară pe eșantionul sortat (metoda „inclusive").

    Implementate în cod, nu în SQL, dintr-un motiv de reproductibilitate: raportul trebuie să dea
    aceleași cifre pe aceleași date indiferent de versiunea de Postgres care le-a servit.
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
        low = math.floor(rank)
        high = math.ceil(rank)
        if low == high:
            value = ordered[low]
        else:
            value = ordered[low] + (ordered[high] - ordered[low]) * (rank - low)
        out[f"p{p}"] = round(value, 1)
    return out


@dataclass
class SloReport:
    """Raportul complet. Se serializează întreg în artefact: fereastră, versiuni, completitudine.

    `verdict` global e cel mai PESIMIST dintre indicatori — un `FAIL` nu se poate media cu trei
    `PASS`-uri, iar un `UNKNOWN` nu devine verde fiindcă restul sunt verzi.
    """

    policy_version: str
    window_from: datetime
    window_to: datetime
    business_id: str
    release_sha: str
    slis: list[SliResult] = field(default_factory=list)
    latency: dict[str, Any] = field(default_factory=dict)
    burn: dict[str, Any] = field(default_factory=dict)
    completeness: dict[str, Any] = field(default_factory=dict)
    invalid_samples: dict[str, int] = field(default_factory=dict)

    @property
    def verdict(self) -> str:
        order = [VERDICT_FAIL, VERDICT_UNKNOWN, VERDICT_INSUFFICIENT, VERDICT_PASS]
        verdicts = {s.verdict for s in self.slis}
        for v in order:
            if v in verdicts:
                return v
        return VERDICT_UNKNOWN

    def as_dict(self) -> dict[str, Any]:
        return {
            "policy_version": self.policy_version,
            "latency_manifest": LATENCY_SOURCE_MANIFEST,
            "window": {
                "from": self.window_from.isoformat(),
                "to": self.window_to.isoformat(),
                "utc": True,
            },
            "business_id": self.business_id,
            "release_sha": self.release_sha,
            "verdict": self.verdict,
            "slis": [s.as_dict() for s in self.slis],
            "latency_ms": self.latency,
            "burn_rate": self.burn,
            "completeness": self.completeness,
            "invalid_samples": dict(sorted(self.invalid_samples.items())),
        }


def evaluate(
    facts: list[TurnFact],
    *,
    window_from: datetime,
    window_to: datetime,
    business_id: str,
    release_sha: str = "unknown",
    truncated: bool = False,
    accept_metrics: dict[str, float] | None = None,
    min_samples: int = MIN_SAMPLES,
    ratified: bool = RATIFIED,
) -> SloReport:
    """Faptele ferestrei → raport. Pur: aceleași intrări, același verdict, mereu.

    `accept_metrics` = contoarele de margine (`web_turn_requests_total{outcome}`), singura sursă
    pentru „accept availability": un request respins la poartă nu creează rând de ledger, deci
    ledgerul nu îl poate număra. Absent ⇒ SLI-ul rămâne `UNKNOWN`, nu dispare din raport — un
    indicator lipsă trebuie să fie vizibil, nu absent.
    """
    report = SloReport(
        policy_version=SLO_POLICY_VERSION,
        window_from=window_from,
        window_to=window_to,
        business_id=business_id,
        release_sha=release_sha,
    )
    invalid: dict[str, int] = {}
    durations: list[float] = []
    for f in facts:
        d = f.duration_ms
        if d is None:
            continue
        if d < 0:
            # Ceas sărit între accept și commit (NTP, replică). Un sample negativ care intră în
            # percentile face p50-ul fals fără nicio urmă — îl scoatem ȘI îl numărăm.
            invalid["negative_duration"] = invalid.get("negative_duration", 0) + 1
            continue
        durations.append(d)

    report.slis.append(_accept_availability(accept_metrics, min_samples=min_samples))
    report.slis.append(_durable_terminal(facts, min_samples=min_samples))
    report.slis.append(_non_empty(facts, min_samples=min_samples))
    report.slis.append(_latency(durations, ratified=ratified, min_samples=min_samples))
    report.slis.append(_reclaim_health(facts, min_samples=min_samples))

    report.latency = {
        **percentiles(durations),
        "n": len(durations),
    }
    report.invalid_samples = invalid
    report.completeness = {
        "ledger_rows": len(facts),
        "truncated": truncated,
        "accept_metrics": accept_metrics is not None,
        "thresholds_ratified": ratified,
        # Ce NU putem calcula azi, spus pe față. Un raport care tace despre lipsuri e un raport
        # care minte prin omisiune.
        "missing": _missing_capabilities(facts, accept_metrics, truncated),
    }
    if truncated:
        for sli in report.slis:
            if sli.source == "ledger" and sli.verdict == VERDICT_PASS:
                sli.verdict = VERDICT_UNKNOWN
                sli.note = "set trunchiat — verdictul nu poate fi PASS pe date parțiale"
    report.burn = burn_rates(report.slis)
    return report


def _missing_capabilities(
    facts: list[TurnFact], accept_metrics: dict[str, float] | None, truncated: bool
) -> list[str]:
    missing: list[str] = []
    if accept_metrics is None:
        missing.append("accept_metrics")
    if not facts:
        missing.append("ledger_rows")
    if truncated:
        missing.append("complete_window")
    # `turn_class` nu se persistă în `web_turns` (NX-241 îl ține doar în runtime), deci latența nu
    # poate fi defalcată pe clasă din ledger. Se raportează global; defalcarea cere un câmp nou.
    missing.append("turn_class_breakdown")
    # Idem `release_sha`: ledgerul are `pipeline_version` (contract), nu versiunea de release.
    missing.append("per_row_release_sha")
    return missing


def _accept_availability(accept_metrics: dict[str, float] | None, *, min_samples: int) -> SliResult:
    sli = SliResult("accept_availability", "metrics", target=0.999 if accept_metrics else None)
    if not accept_metrics:
        sli.note = "fără contoare de margine în fereastră (proces repornit sau export stins)"
        sli.decide(min_samples=min_samples)
        return sli
    good = int(accept_metrics.get("accepted", 0) + accept_metrics.get("replayed", 0))
    # Excluse explicit din numitor: nu sunt promisiunea noastră, dar rămân vizibile.
    excluded = {
        "rejected": int(accept_metrics.get("rejected", 0)),
        "conflict": int(accept_metrics.get("conflict", 0)),
    }
    bad = int(accept_metrics.get("error", 0) + accept_metrics.get("failed", 0))
    sli.numerator = good
    sli.denominator = good + bad
    sli.excluded = excluded
    sli.decide(min_samples=min_samples)
    return sli


def _durable_terminal(facts: list[TurnFact], *, min_samples: int) -> SliResult:
    """Ture acceptate (inclusiv reclaimed) care au ajuns la un terminal durabil."""
    sli = SliResult("durable_terminal", "ledger", target=0.995)
    excluded: dict[str, int] = {}
    for f in facts:
        if f.status == "cancelled" and (f.safe_error_code or "") in EXCLUDED_CODES:
            excluded["user_cancelled"] = excluded.get("user_cancelled", 0) + 1
            continue
        sli.denominator += 1
        if f.terminal:
            sli.numerator += 1
    sli.excluded = excluded
    sli.decide(min_samples=min_samples)
    return sli


def _non_empty(facts: list[TurnFact], *, min_samples: int) -> SliResult:
    """P6: fiecare terminal are un ViewModel randabil. Zero toleranță — pragul e 1.0.

    Nu se aplică `MIN_SAMPLES`: la 100% cerut, o singură violare e deja verdict, indiferent de
    mărimea eșantionului. Un `INSUFFICIENT` aici ar ascunde exact bug-ul pe care îl căutăm.
    """
    sli = SliResult("non_empty_terminal", "ledger", target=1.0)
    for f in facts:
        if not f.terminal:
            continue
        sli.denominator += 1
        if f.renderable:
            sli.numerator += 1
    if sli.denominator > 0 and sli.numerator < sli.denominator:
        sli.verdict = VERDICT_FAIL
        sli.note = f"{sli.denominator - sli.numerator} terminale fără conținut randabil (P6)"
        return sli
    sli.decide(min_samples=1)
    return sli


def _latency(durations: list[float], *, ratified: bool, min_samples: int) -> SliResult:
    """Accept → terminal durabil, p90 global. Judecat DOAR cu praguri ratificate."""
    sli = SliResult("latency_p90", "ledger")
    sli.denominator = len(durations)
    if not durations:
        sli.decide(min_samples=min_samples)
        return sli
    p = percentiles(durations, (90,))
    sli.extra = {"p90_ms": p.get("p90"), "target_ms": 12_000 if ratified else None}
    if not ratified:
        sli.note = (
            f"prag NX-241 neratificat (manifest {LATENCY_SOURCE_MANIFEST}) — raportat, nu judecat"
        )
        sli.verdict = VERDICT_UNKNOWN
        return sli
    sli.target = 1.0
    sli.numerator = sum(1 for d in durations if d <= 12_000)
    sli.decide(min_samples=min_samples)
    return sli


def _reclaim_health(facts: list[TurnFact], *, min_samples: int) -> SliResult:
    """Ture duse la capăt din PRIMA încercare. Un reclaim nu e un eșec, dar o rată mare de
    reclaim e un sistem care se târăște — și e invizibilă în availability."""
    sli = SliResult("first_attempt_success", "ledger", target=0.99)
    for f in facts:
        if not f.terminal:
            continue
        sli.denominator += 1
        if f.attempt <= 1 and f.status == "completed":
            sli.numerator += 1
    sli.decide(min_samples=min_samples)
    return sli


def burn_rates(slis: list[SliResult], windows: tuple[str, ...] = ("1h", "6h", "3d")) -> dict:
    """Burn-rate din bugetul de eroare al indicatorilor cu prag.

    `burn = (1 - ratio) / (1 - target)`: 1.0 = consumi bugetul exact în ritmul ferestrei; 14.4 e
    pragul clasic de alertă rapidă. Fără prag ratificat sau fără date, întoarcem `None` — nu 0,
    fiindcă 0 ar arăta ca „nu ardem nimic", adică perfect sănătos.
    """
    out: dict[str, Any] = {"windows": list(windows), "by_sli": {}}
    for sli in slis:
        ratio = sli.ratio
        if sli.target is None or ratio is None or sli.target >= 1.0:
            out["by_sli"][sli.name] = None
            continue
        budget = 1.0 - sli.target
        out["by_sli"][sli.name] = round((1.0 - ratio) / budget, 3) if budget > 0 else None
    return out


def window_bounds(now: datetime, window: str) -> tuple[datetime, datetime]:
    """`1h` / `6h` / `7d` → (from, to) în UTC. Ridică pe format necunoscut: o fereastră greșit
    înțeleasă produce un raport cu granițe false, care e mai rău decât niciun raport."""
    unit = window[-1:].lower()
    try:
        amount = int(window[:-1])
    except ValueError as e:
        raise ValueError(f"fereastră invalidă: {window!r} (fix: 1h, 6h, 7d)") from e
    if amount <= 0:
        raise ValueError(f"fereastră invalidă: {window!r}")
    if unit == "h":
        delta = timedelta(hours=amount)
    elif unit == "d":
        delta = timedelta(days=amount)
    elif unit == "m":
        delta = timedelta(minutes=amount)
    else:
        raise ValueError(f"unitate de fereastră necunoscută: {window!r} (fix: m/h/d)")
    return now - delta, now
