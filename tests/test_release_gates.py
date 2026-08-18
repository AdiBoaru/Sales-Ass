"""NX-249 — porțile: hard stops peste orice scor, patru verdicte, „no data" nu e verde.

Trei proprietăți se verifică aici, fiindcă sunt exact cele pe care un gate le pierde primul:

  1. **Un singur hard stop bate orice.** Feedback pozitiv, latență bună, conversie mai mare — nimic
     nu compensează un preț inventat sau un leak cross-tenant.
  2. **`INSUFFICIENT` ≠ `FAIL` ≠ `UNKNOWN`.** Se rezolvă prin trei acțiuni diferite (așteaptă,
     oprește, repară instrumentul). Colapsate într-un „nu", devin toate „mai încercăm".
  3. **Verdictul agregat e cel mai PESIMIST.** Un `FAIL` nu se mediază cu trei `PASS`-uri.
"""

from __future__ import annotations

from src.observability.contract import (
    RELEASE_ASSIGNMENT_REASONS,
    RELEASE_DECISIONS,
    RELEASE_GATE_VERDICTS,
    RELEASE_MODES,
    RELEASE_POLICY_AGE_BUCKETS,
    RELEASE_POLICY_OUTCOMES,
)
from src.release import policy_store
from src.release.gates import (
    HARD_STOP_SET,
    VERDICT_FAIL,
    VERDICT_INSUFFICIENT,
    VERDICT_PASS,
    VERDICT_UNKNOWN,
    CohortStats,
    GateReport,
    GateResult,
    gate_completeness,
    gate_hard_stops,
    gate_non_inferiority,
    gate_stage_window,
    gate_upstream,
    next_stage,
    percentiles,
    wilson_interval,
    worst,
)
from src.release.models import (
    ASSIGNMENT_REASONS,
    DECISION_CANDIDATE,
    DECISION_CONTROL,
    DECISION_DRAIN,
    MODE_CLOSED,
    MODES,
    STAGES_BY_INDEX,
)
from tests.test_release_assignment import policy


# ── Sincronizarea vocabularelor ─────────────────────────────────────────────────────────────
def test_vocabularul_de_release_e_sincron_cu_contractul():
    """`contract.py` reproduce vocabularul (n-are dependențe din `src/`) — deci driftul se prinde
    aici, nu într-un dashboard care rămâne verde pe o serie care nu mai există."""
    assert RELEASE_ASSIGNMENT_REASONS == ASSIGNMENT_REASONS
    assert RELEASE_MODES == MODES
    assert RELEASE_DECISIONS == {DECISION_CONTROL, DECISION_CANDIDATE, DECISION_DRAIN}
    assert RELEASE_POLICY_OUTCOMES == policy_store.POLICY_CODES
    assert RELEASE_POLICY_AGE_BUCKETS == {policy_store.age_bucket(t) for t in (0, 10, 100, 1000)}
    assert RELEASE_GATE_VERDICTS == {
        VERDICT_PASS,
        VERDICT_FAIL,
        VERDICT_INSUFFICIENT,
        VERDICT_UNKNOWN,
    }


# ── Hard stops ──────────────────────────────────────────────────────────────────────────────
def test_niciun_hard_stop_inseamna_pass():
    assert gate_hard_stops([]).verdict == VERDICT_PASS


def test_un_singur_hard_stop_da_fail():
    g = gate_hard_stops(["invented_fact"])
    assert g.verdict == VERDICT_FAIL
    assert "invented_fact" in g.note


def test_un_cod_necunoscut_tot_da_fail():
    """Nu ignorăm ce nu înțelegem: un cod nou raportat în incident nu trebuie să treacă tăcut."""
    g = gate_hard_stops(["ceva_nou_si_urat"])
    assert g.verdict == VERDICT_FAIL
    assert g.detail["unknown_codes"] == ["ceva_nou_si_urat"]


def test_vocabularul_de_hard_stop_acopera_lista_din_card():
    for code in (
        "cross_tenant_leak",
        "secret_or_pii_leak",
        "invented_fact",
        "empty_terminal",
        "duplicate_execution",
        "false_receipt",
        "artifact_mismatch",
        "slo_fast_burn",
        "rollback_impossible",
    ):
        assert code in HARD_STOP_SET


# ── Etapă: timp ȘI eșantion ─────────────────────────────────────────────────────────────────
def test_etapa_cere_si_timp_si_esantion():
    stage = STAGES_BY_INDEX[3]  # pilot 5%: ≥48h ȘI ≥200 ture candidate
    assert gate_stage_window(stage, elapsed_hours=72, candidate_turns=201).verdict == VERDICT_PASS
    # Timp destul, eșantion prea mic → INSUFFICIENT (nu FAIL: se rezolvă așteptând).
    short_sample = gate_stage_window(stage, elapsed_hours=72, candidate_turns=12)
    assert short_sample.verdict == VERDICT_INSUFFICIENT
    assert "eșantion" in short_sample.note
    # Eșantion destul, timp prea puțin → tot INSUFFICIENT.
    short_time = gate_stage_window(stage, elapsed_hours=2, candidate_turns=900)
    assert short_time.verdict == VERDICT_INSUFFICIENT
    assert "timp" in short_time.note


def test_etapa_urmatoare_e_pasul_declarat_nu_orice_procent():
    """Progresia are pași ficși: după 5% vine 20%, iar după etapa 6 vine închiderea v1."""
    assert next_stage(policy(percent=5)).percent == 20
    assert next_stage(policy(percent=20)).percent == 50
    assert next_stage(policy(percent=100, stage=6)).index == 7
    assert next_stage(policy(mode=MODE_CLOSED, percent=100)) is None, "etapa 7 e ultima"


# ── Non-inferioritate ───────────────────────────────────────────────────────────────────────
def test_esantion_mic_nu_decide():
    g = gate_non_inferiority(
        name="x", candidate_bad=0, candidate_n=10, control_bad=5, control_n=200, margin_pp=5
    )
    assert g.verdict == VERDICT_INSUFFICIENT


def test_candidate_clar_mai_prost_da_fail():
    g = gate_non_inferiority(
        name="x", candidate_bad=60, candidate_n=200, control_bad=10, control_n=200, margin_pp=5
    )
    assert g.verdict == VERDICT_FAIL


def test_candidate_la_fel_de_bun_trece():
    g = gate_non_inferiority(
        name="x", candidate_bad=2, candidate_n=1000, control_bad=20, control_n=1000, margin_pp=5
    )
    assert g.verdict == VERDICT_PASS


def test_diferenta_in_marja_dar_cu_interval_larg_e_insufficient():
    """Estimarea punctuală trece, intervalul nu exclude o regresie — nu promovăm pe zgomot."""
    g = gate_non_inferiority(
        name="x", candidate_bad=8, candidate_n=60, control_bad=5, control_n=60, margin_pp=5
    )
    assert g.verdict == VERDICT_INSUFFICIENT


def test_intervalul_wilson_nu_colapseaza_la_zece_din_zece():
    """Wald ar raporta „între 100% și 100%" — o afirmație despre aritmetică, nu despre lume."""
    low, high = wilson_interval(10, 10)
    assert low < 1.0
    assert high == 1.0
    assert low > 0.6


def test_wilson_pe_esantion_zero_nu_pretinde_nimic():
    assert wilson_interval(0, 0) == (0.0, 1.0)


# ── Upstream ────────────────────────────────────────────────────────────────────────────────
def test_verdict_upstream_absent_e_unknown():
    assert gate_upstream("q", None, source="NX-246").verdict == VERDICT_UNKNOWN


def test_verdict_upstream_necunoscut_nu_devine_pass():
    """Un vocabular schimbat în alt card trebuie să OPREASCĂ promovarea, nu s-o lase să treacă."""
    assert gate_upstream("q", "SOMEHOW_FINE", source="NX-246").verdict == VERDICT_UNKNOWN


def test_verdictele_upstream_cunoscute_se_traduc():
    assert gate_upstream("e", "PASS", source="s").verdict == VERDICT_PASS
    assert gate_upstream("e", "READY", source="s").verdict == VERDICT_PASS
    assert gate_upstream("e", "NOT-READY", source="s").verdict == VERDICT_UNKNOWN
    assert gate_upstream("e", "NOT_READY", source="s").verdict == VERDICT_UNKNOWN
    assert gate_upstream("e", "BLOCKED", source="s").verdict == VERDICT_FAIL
    assert gate_upstream("e", "NO_GO", source="s").verdict == VERDICT_FAIL


# ── Completitudine ──────────────────────────────────────────────────────────────────────────
def test_lipsa_unui_cohort_nu_poate_fi_pass():
    g = gate_completeness(
        cohort_stats={"candidate": CohortStats("candidate", turns=500)},
        truncated=False,
        unknown_turns=0,
    )
    assert g.verdict == VERDICT_UNKNOWN


def test_fereastra_trunchiata_nu_poate_fi_pass():
    stats = {
        "candidate": CohortStats("candidate", turns=500),
        "champion": CohortStats("champion", turns=500),
    }
    assert gate_completeness(cohort_stats=stats, truncated=True, unknown_turns=0).verdict == (
        VERDICT_UNKNOWN
    )


def test_prea_multe_ture_fara_captura_dau_unknown():
    stats = {
        "candidate": CohortStats("candidate", turns=100),
        "champion": CohortStats("champion", turns=100),
    }
    assert gate_completeness(cohort_stats=stats, truncated=False, unknown_turns=50).verdict == (
        VERDICT_UNKNOWN
    )
    assert gate_completeness(cohort_stats=stats, truncated=False, unknown_turns=5).verdict == (
        VERDICT_PASS
    )


# ── Agregare ────────────────────────────────────────────────────────────────────────────────
def test_verdictul_agregat_e_cel_mai_pesimist():
    assert worst([VERDICT_PASS, VERDICT_PASS, VERDICT_FAIL]) == VERDICT_FAIL
    assert worst([VERDICT_PASS, VERDICT_UNKNOWN]) == VERDICT_UNKNOWN
    assert worst([VERDICT_PASS, VERDICT_INSUFFICIENT]) == VERDICT_INSUFFICIENT
    assert worst([VERDICT_PASS]) == VERDICT_PASS
    assert worst([]) == VERDICT_UNKNOWN


def test_un_hard_stop_bate_toate_porțile_verzi():
    report = GateReport(stage="3-pilot")
    report.gates.append(gate_hard_stops(["cross_tenant_leak"]))
    for name in ("slo", "quality", "e2e", "latency", "feedback"):
        report.gates.append(GateResult(name, VERDICT_PASS, "verde"))
    assert report.verdict == VERDICT_FAIL
    assert "hard_stops" in report.blocking


def test_raportul_spune_explicit_ca_pass_nu_promoveaza():
    payload = GateReport(stage="3-pilot").as_dict()
    assert "nu promovează singur" in payload["promotion"]


# ── Percentile ──────────────────────────────────────────────────────────────────────────────
def test_percentile_pe_esantion_de_unu_si_pe_lista_goala():
    assert percentiles([]) == {}
    assert percentiles([42.0])["p90"] == 42.0


def test_percentile_interpoleaza_ca_la_nx246():
    values = [float(i) for i in range(1, 101)]
    assert percentiles(values, (50,))["p50"] == 50.5
