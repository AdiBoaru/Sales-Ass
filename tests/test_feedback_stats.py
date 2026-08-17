"""NX-246 (felia 2) — statistica: de ce un procent gol e o minciună politicoasă.

Testul central: sub prag NU există procent. Nu 0%, nu 100%, nu „n/a" care s-ar citi ca zero.
Restul verifică de ce intervalul e Wilson și nu Wald, și că raportul nu se numește niciodată CSAT.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.observability.feedback_stats import (
    MIN_FEEDBACK_SAMPLE,
    VERDICT_INSUFFICIENT,
    VERDICT_OK,
    Tally,
    build_report,
    wilson_interval,
)

T0 = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
BID = "biz-1"


def _report(tallies, **over):
    kw = dict(
        window_from=T0 - timedelta(days=7),
        window_to=T0,
        business_id=BID,
        taxonomy_version="feedback.v1",
    )
    kw.update(over)
    return build_report(tallies, **kw)


def test_sub_prag_nu_exista_procent():
    r = _report(
        [Tally("positive", None, "champion", 7), Tally("negative", "too_long", "champion", 1)]
    )
    payload = r.as_dict()
    assert payload["verdict"] == VERDICT_INSUFFICIENT
    assert payload["positive_feedback_rate"] is None, "87% din 8 voturi"
    assert payload["confidence_interval_95"] is None
    assert payload["n"] == 8


def test_peste_prag_procentul_vine_cu_n_si_interval():
    r = _report([Tally("positive", None, "champion", 80), Tally("negative", None, "champion", 20)])
    payload = r.as_dict()
    assert payload["verdict"] == VERDICT_OK
    assert payload["positive_feedback_rate"] == 0.8
    low, high = payload["confidence_interval_95"]
    assert low < 0.8 < high, "intervalul nu conține estimarea"
    assert payload["n"] == 100


def test_wilson_ramane_onest_la_capete():
    """Wald ar da (1.0, 1.0) la 10/10 — „certitudine absolută din zece voturi"."""
    low, high = wilson_interval(10, 10)
    assert low < 1.0, f"intervalul e degenerat: ({low}, {high})"
    low0, high0 = wilson_interval(0, 12)
    assert low0 >= 0.0 and high0 > 0.0, "intervalul coboară sub zero (Wald)"


def test_fara_date_nu_stim_nimic():
    assert wilson_interval(0, 0) == (0.0, 1.0)
    payload = _report([]).as_dict()
    assert payload["n"] == 0 and payload["positive_feedback_rate"] is None


def test_cohortul_mic_nu_primeste_procent_desi_totalul_e_mare():
    """Altfel exact comparația champion-vs-candidate s-ar face pe zgomot."""
    r = _report(
        [
            Tally("positive", None, "champion", 90),
            Tally("negative", None, "champion", 10),
            Tally("positive", None, "candidate", 4),
        ]
    )
    tracks = r.as_dict()["by_release_track"]
    assert tracks["champion"]["verdict"] == VERDICT_OK
    assert tracks["candidate"]["verdict"] == VERDICT_INSUFFICIENT
    assert tracks["candidate"]["positive_feedback_rate"] is None
    assert tracks["candidate"]["n"] == 4


def test_motivele_se_numara_doar_pe_voturile_negative():
    r = _report(
        [
            Tally("positive", None, "champion", 40),
            Tally("negative", "wrong_facts", "champion", 3),
            Tally("negative", None, "champion", 2),
        ]
    )
    assert r.by_reason == {"wrong_facts": 3, "none": 2}


def test_raportul_nu_se_numeste_niciodata_csat():
    payload = _report([Tally("positive", None, "champion", 50)]).as_dict()
    blob = str(payload).lower()
    assert "csat" not in blob and "satisfaction" not in blob
    assert "positive_feedback_rate" in payload


def test_pragul_e_configurabil_dar_explicit_in_raport():
    r = _report(
        [Tally("positive", None, "champion", 5)],
    )
    assert r.as_dict()["min_sample"] == MIN_FEEDBACK_SAMPLE
    permisiv = build_report(
        [Tally("positive", None, "champion", 5)],
        window_from=T0,
        window_to=T0,
        business_id=BID,
        taxonomy_version="feedback.v1",
        min_sample=1,
    )
    assert permisiv.verdict == VERDICT_OK


@pytest.mark.parametrize("successes,total", [(0, 30), (30, 30), (15, 30)])
def test_intervalul_e_intotdeauna_in_zero_unu(successes, total):
    low, high = wilson_interval(successes, total)
    assert 0.0 <= low <= high <= 1.0
