"""NX-246 — `slo_policy.v1`: numărătorii, numitorii, excluderile și modurile de a NU ști.

Testul central al fișierului nu e „calculează procentul corect", ci: **lipsa datelor nu poate
produce PASS.** Un gate care trece pe „n-am găsit rânduri" e fail-open, adică exact opusul unui
gate. Restul verifică lucrurile pe care un raport de SLO le greșește în tăcere: replay numărat ca
request nou, anulare de client ascunsă în availability, clock skew intrat în percentile, set
trunchiat prezentat ca fereastră completă.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.observability.slo import (
    MIN_SAMPLES,
    SLO_POLICY_VERSION,
    VERDICT_FAIL,
    VERDICT_INSUFFICIENT,
    VERDICT_PASS,
    VERDICT_UNKNOWN,
    TurnFact,
    evaluate,
    percentiles,
    window_bounds,
)

T0 = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
BID = "6098812a-50fc-44bd-a1ba-bc77e6399158"


def fact(
    status="completed",
    *,
    code=None,
    attempt=1,
    duration_ms=1000,
    renderable=True,
    accepted=T0,
) -> TurnFact:
    completed = None if duration_ms is None else accepted + timedelta(milliseconds=duration_ms)
    return TurnFact(
        status=status,
        safe_error_code=code,
        attempt=attempt,
        accepted_at=accepted,
        completed_at=completed,
        deadline_at=accepted + timedelta(seconds=15),
        renderable=renderable,
    )


def run(facts, **over):
    kw = dict(
        window_from=T0 - timedelta(days=7),
        window_to=T0,
        business_id=BID,
        release_sha="deadbeef",
    )
    kw.update(over)
    return evaluate(facts, **kw)


def sli(report, name):
    return next(s for s in report.slis if s.name == name)


# ── Lipsa datelor ───────────────────────────────────────────────────────────────────────────


def test_fereastra_goala_nu_poate_da_pass():
    report = run([])
    assert report.verdict == VERDICT_UNKNOWN
    assert all(s.verdict != VERDICT_PASS for s in report.slis)
    assert "ledger_rows" in report.completeness["missing"]


def test_esantion_mic_da_insufficient_nu_pass():
    report = run([fact() for _ in range(MIN_SAMPLES - 1)])
    assert sli(report, "durable_terminal").verdict == VERDICT_INSUFFICIENT
    assert report.verdict != VERDICT_PASS


def test_set_trunchiat_degradeaza_pass_ul_la_unknown():
    """Un procent calculat pe primele N rânduri nu e procentul ferestrei."""
    facts = [fact() for _ in range(200)]
    assert sli(run(facts), "durable_terminal").verdict == VERDICT_PASS
    trunchiat = run(facts, truncated=True)
    assert sli(trunchiat, "durable_terminal").verdict == VERDICT_UNKNOWN
    assert "complete_window" in trunchiat.completeness["missing"]


def test_accept_availability_fara_metrici_ramane_unknown_dar_vizibil():
    """SLI-ul lipsă trebuie să APARĂ în raport, marcat necunoscut — nu să dispară din listă."""
    report = run([fact() for _ in range(50)])
    s = sli(report, "accept_availability")
    assert s.verdict == VERDICT_UNKNOWN and s.source == "metrics"
    assert "accept_metrics" in report.completeness["missing"]


def test_accept_availability_din_metrici():
    report = run(
        [fact() for _ in range(50)],
        accept_metrics={"accepted": 990, "replayed": 8, "error": 2, "rejected": 40},
    )
    s = sli(report, "accept_availability")
    assert s.denominator == 1000 and s.numerator == 998
    assert s.excluded["rejected"] == 40, "respinsele trebuie raportate, nu topite în numitor"
    assert s.verdict == VERDICT_FAIL  # 99,8% < 99,9%


# ── Excluderi explicite ─────────────────────────────────────────────────────────────────────


def test_anularea_clientului_e_exclusa_dar_numarata():
    facts = [fact() for _ in range(40)] + [fact("cancelled", code="cancelled") for _ in range(5)]
    s = sli(run(facts), "durable_terminal")
    assert s.denominator == 40, "anularea clientului nu e o promisiune încălcată"
    assert s.excluded["user_cancelled"] == 5, "dar nici nu are voie să dispară"


def test_esecul_intern_ramane_in_numitor():
    """Timeout/eroare internă NU se exclud — exact ele sunt promisiunea pe care o facem."""
    facts = [fact() for _ in range(38)] + [
        fact("failed", code="processing_error"),
        fact("failed", code="deadline_exceeded"),
    ]
    s = sli(run(facts), "durable_terminal")
    assert s.denominator == 40 and s.numerator == 40  # terminale, chiar dacă eșuate
    non_empty = sli(run(facts), "non_empty_terminal")
    assert non_empty.denominator == 40


# ── P6: non-empty are toleranță zero ────────────────────────────────────────────────────────


def test_un_singur_terminal_gol_da_fail_indiferent_de_esantion():
    facts = [fact() for _ in range(5)] + [fact(renderable=False)]
    s = sli(run(facts), "non_empty_terminal")
    assert s.verdict == VERDICT_FAIL, "P6 nu se mediază și nu așteaptă eșantion mare"
    assert run(facts).verdict == VERDICT_FAIL


def test_turele_nefinalizate_nu_intra_in_non_empty():
    facts = [fact() for _ in range(10)] + [fact("running", duration_ms=None)]
    assert sli(run(facts), "non_empty_terminal").denominator == 10


def test_turul_neterminat_scade_durable_terminal():
    facts = [fact() for _ in range(35)] + [fact("running", duration_ms=None) for _ in range(5)]
    s = sli(run(facts), "durable_terminal")
    assert s.denominator == 40 and s.numerator == 35
    assert s.verdict == VERDICT_FAIL


# ── Latență: raportată, nu judecată, până la ratificare ─────────────────────────────────────


def test_latenta_e_raportata_nu_judecata_fara_ratificare():
    report = run([fact(duration_ms=30_000) for _ in range(50)])
    s = sli(report, "latency_p90")
    assert s.verdict == VERDICT_UNKNOWN
    assert "neratificat" in s.note
    assert report.latency["p90"] == 30_000.0, "cifra se RAPORTEAZĂ chiar dacă nu se judecă"


def test_latenta_devine_verdict_dupa_ratificare():
    assert sli(
        run([fact(duration_ms=1000) for _ in range(50)], ratified=True), "latency_p90"
    ).verdict == (VERDICT_PASS)
    assert sli(
        run([fact(duration_ms=30_000) for _ in range(50)], ratified=True), "latency_p90"
    ).verdict == (VERDICT_FAIL)


def test_clock_skew_nu_intra_in_percentile():
    """Failure matrix: sample invalid + contor; nu intră silențios în p50."""
    bun = [fact(duration_ms=1000) for _ in range(10)]
    stricat = TurnFact("completed", None, 1, T0, T0 - timedelta(seconds=5), None, True)
    report = run([*bun, stricat])
    assert report.invalid_samples["negative_duration"] == 1
    assert report.latency["n"] == 10, "eșantionul negativ a intrat în percentile"


def test_percentile_interpolate():
    assert percentiles([10, 20, 30, 40], (50,))["p50"] == 25.0
    assert percentiles([5], (90,))["p90"] == 5.0
    assert percentiles([], (50,)) == {}


# ── Raportul ────────────────────────────────────────────────────────────────────────────────


def test_verdictul_global_e_cel_mai_pesimist():
    facts = [fact() for _ in range(40)] + [fact(renderable=False)]
    assert run(facts).verdict == VERDICT_FAIL, "un FAIL nu se mediază cu trei PASS-uri"


def test_raportul_poarta_fereastra_versiunile_si_lipsurile():
    payload = run([fact() for _ in range(40)]).as_dict()
    assert payload["policy_version"] == SLO_POLICY_VERSION
    assert payload["window"]["utc"] is True
    assert payload["release_sha"] == "deadbeef"
    assert payload["latency_manifest"].startswith("nx241.")
    # Ce NU putem calcula azi, spus pe față — nu omis.
    assert "turn_class_breakdown" in payload["completeness"]["missing"]
    assert "per_row_release_sha" in payload["completeness"]["missing"]


def test_burn_rate_e_none_nu_zero_fara_date():
    """Zero ar arăta ca „nu ardem nimic", adică perfect sănătos."""
    burn = run([]).burn["by_sli"]
    assert burn["durable_terminal"] is None
    assert burn["non_empty_terminal"] is None  # target 1.0 ⇒ buget zero, nu se poate arde


def test_burn_rate_se_calculeaza_din_bugetul_de_eroare():
    facts = [fact() for _ in range(39)] + [fact("running", duration_ms=None)]
    burn = run(facts).burn["by_sli"]
    # 2,5% eroare / buget 0,5% = 5×
    assert burn["durable_terminal"] == pytest.approx(5.0, rel=0.01)


def test_reclaim_health_vede_ce_availability_ascunde():
    """O rată mare de reclaim e un sistem care se târăște — și e invizibilă în availability."""
    facts = [fact() for _ in range(30)] + [fact(attempt=3) for _ in range(10)]
    assert sli(run(facts), "durable_terminal").verdict == VERDICT_PASS
    assert sli(run(facts), "first_attempt_success").verdict == VERDICT_FAIL


# ── Ferestre ────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "window,delta",
    [("1h", timedelta(hours=1)), ("6h", timedelta(hours=6)), ("7d", timedelta(days=7))],
)
def test_window_bounds(window, delta):
    start, end = window_bounds(T0, window)
    assert end == T0 and end - start == delta


@pytest.mark.parametrize("bad", ["", "7", "d7", "0h", "-1h", "7y"])
def test_fereastra_invalida_ridica(bad):
    """O fereastră greșit înțeleasă produce un raport cu granițe false — mai rău decât niciunul."""
    with pytest.raises(ValueError):
        window_bounds(T0, bad)
