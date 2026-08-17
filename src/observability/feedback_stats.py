"""NX-246 (felia 2) — statistica feedbackului. Pură: numere in, numere out.

Un procent fără `n` e o minciună politicoasă. „87% pozitiv" din 8 voturi și „87% pozitiv" din
8000 sunt afirmații complet diferite, iar diferența decide dacă promovezi un release. De aceea
raportul de aici nu poate produce un procent gol:

  • sub `MIN_FEEDBACK_SAMPLE` verdictul e `insufficient_sample` — nu 87%, nu 0%, nu „n/a";
  • peste prag, procentul vine ÎNTOTDEAUNA cu `n` și cu un interval de încredere;
  • intervalul e **Wilson**, nu normal (Wald). Wald e formula pe care o știe toată lumea și e
    exact cea care se strică la extreme: la 10 din 10 pozitive dă un interval de lățime zero
    („între 100% și 100%"), iar la 0 din 12 dă un interval care coboară sub zero. Wilson rămâne
    onest la capete, adică fix acolo unde e un produs nou.

**Nu se numește CSAT.** CSAT are o metodologie (scală, moment, populație, rată de răspuns) pe
care nu o îndeplinim: strângem voturi one-tap, de la cine vrea, pe turele care au primit prompt.
`positive_feedback_rate` spune exact ce e — proporția de voturi pozitive dintre voturile primite.
Cardul o cere explicit, iar diferența nu e cosmetică: „CSAT 87%" într-un raport de vânzări e o
promisiune despre clienți, `positive_feedback_rate` e o observație despre butoane.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

#: Sub atâtea voturi nu emitem procent. 30 e același prag ca la SLI (`slo.MIN_SAMPLES`), din
#: același motiv: sub el, intervalul de încredere e mai lat decât orice decizie pe care ai lua-o.
MIN_FEEDBACK_SAMPLE = 30

VERDICT_OK = "ok"
VERDICT_INSUFFICIENT = "insufficient_sample"


def wilson_interval(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """Interval Wilson (95% implicit). `(0.0, 1.0)` pentru `total <= 0` — nu știm nimic.

    Wilson în loc de Wald fiindcă la capete Wald minte: 10/10 ⇒ Wald dă (1.0, 1.0), adică
    „certitudine absolută din zece voturi".
    """
    if total <= 0:
        return (0.0, 1.0)
    p = successes / total
    z2 = z * z
    denom = 1.0 + z2 / total
    center = (p + z2 / (2 * total)) / denom
    margin = (z * math.sqrt(p * (1 - p) / total + z2 / (4 * total * total))) / denom
    return (max(0.0, center - margin), min(1.0, center + margin))


@dataclass(frozen=True)
class Tally:
    """O linie de agregat (ce întoarce `db.queries.feedback.tally_feedback`)."""

    rating: str
    reason_code: str | None
    release_track: str
    n: int


@dataclass
class FeedbackReport:
    """Raportul unei ferestre. `rate` e `None` sub prag — nu 0.0, care ar arăta ca „toată lumea
    e nemulțumită" într-un panel care nu citește `verdict`."""

    window_from: datetime
    window_to: datetime
    business_id: str
    taxonomy_version: str
    n: int = 0
    positive: int = 0
    negative: int = 0
    rate: float | None = None
    ci_low: float | None = None
    ci_high: float | None = None
    verdict: str = VERDICT_INSUFFICIENT
    by_reason: dict[str, int] = field(default_factory=dict)
    by_track: dict[str, dict[str, Any]] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "window": {
                "from": self.window_from.isoformat(),
                "to": self.window_to.isoformat(),
                "utc": True,
            },
            "business_id": self.business_id,
            "taxonomy_version": self.taxonomy_version,
            "verdict": self.verdict,
            "n": self.n,
            "positive": self.positive,
            "negative": self.negative,
            # Numele câmpului e contractul: NU „csat", NU „satisfaction".
            "positive_feedback_rate": None if self.rate is None else round(self.rate, 4),
            "confidence_interval_95": (
                None
                if self.ci_low is None
                else [round(self.ci_low, 4), round(self.ci_high or 0.0, 4)]
            ),
            "min_sample": MIN_FEEDBACK_SAMPLE,
            "by_reason": dict(sorted(self.by_reason.items())),
            "by_release_track": {k: self.by_track[k] for k in sorted(self.by_track)},
        }


def build_report(
    tallies: list[Tally],
    *,
    window_from: datetime,
    window_to: datetime,
    business_id: str,
    taxonomy_version: str,
    min_sample: int = MIN_FEEDBACK_SAMPLE,
) -> FeedbackReport:
    """Agregatele SQL → raport. Pur: aceleași numere, același raport.

    Defalcarea pe `release_track` are propriul ei prag: un cohort de 4 voturi nu primește procent
    doar fiindcă totalul general e mare — altfel exact comparația champion-vs-candidate, care e
    scopul întregului mecanism, s-ar face pe zgomot.
    """
    report = FeedbackReport(
        window_from=window_from,
        window_to=window_to,
        business_id=business_id,
        taxonomy_version=taxonomy_version,
    )
    per_track: dict[str, dict[str, int]] = {}
    for t in tallies:
        report.n += t.n
        if t.rating == "positive":
            report.positive += t.n
        elif t.rating == "negative":
            report.negative += t.n
            report.by_reason[t.reason_code or "none"] = (
                report.by_reason.get(t.reason_code or "none", 0) + t.n
            )
        bucket = per_track.setdefault(t.release_track, {"n": 0, "positive": 0})
        bucket["n"] += t.n
        if t.rating == "positive":
            bucket["positive"] += t.n

    if report.n >= min_sample:
        report.rate = report.positive / report.n
        report.ci_low, report.ci_high = wilson_interval(report.positive, report.n)
        report.verdict = VERDICT_OK

    for track, bucket in per_track.items():
        entry: dict[str, Any] = {"n": bucket["n"], "positive": bucket["positive"]}
        if bucket["n"] >= min_sample:
            low, high = wilson_interval(bucket["positive"], bucket["n"])
            entry["positive_feedback_rate"] = round(bucket["positive"] / bucket["n"], 4)
            entry["confidence_interval_95"] = [round(low, 4), round(high, 4)]
            entry["verdict"] = VERDICT_OK
        else:
            entry["positive_feedback_rate"] = None
            entry["verdict"] = VERDICT_INSUFFICIENT
        report.by_track[track] = entry
    return report
