"""NX-249 — evidence packetul: determinist, publicabil, și incapabil să treacă pe date lipsă.

Trei lucruri care fac diferența dintre un raport util și unul decorativ:

  1. **Nu conține identificatori.** Testul scanează recursiv artefactul și pică pe orice arată a
     UUID. Nu ne bazăm pe disciplina celui care adaugă mâine un câmp — un raport de release ajunge
     în tichete, ecrane partajate și arhive de CI.
  2. **E determinist.** Două rulări pe aceleași date dau aceeași amprentă, altfel „a driftat
     raportul?" nu are răspuns. `generated_at` stă în AFARA amprentei, deliberat.
  3. **Lipsa datelor nu e verde.** Fără cohort, fără artefacte sau cu fereastră trunchiată,
     verdictul e `UNKNOWN`/`INSUFFICIENT` — niciodată `PASS`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from src.release.gates import VERDICT_FAIL, VERDICT_INSUFFICIENT, VERDICT_PASS, VERDICT_UNKNOWN
from src.release.models import TRACK_CANDIDATE, TRACK_CHAMPION
from src.release.report import (
    NON_INFERIORITY_MARGIN_PP,
    aggregate_cohorts,
    artifact_hash,
    build_packet,
    load_artifact,
)
from tests.test_release_assignment import BID, policy

T0 = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


@dataclass
class Fact:
    """Un `CohortTurnFact` minimal — testele agregării nu au nevoie de Postgres."""

    track: str
    status: str = "completed"
    attempt: int = 1
    renderable: bool = True
    has_action: bool = False
    duration_ms: int | None = 1200
    accepted_at: datetime = T0

    @property
    def completed_at(self):
        if self.duration_ms is None:
            return None
        return self.accepted_at + timedelta(milliseconds=self.duration_ms)

    safe_error_code = None
    policy_id = "nx249-test"
    policy_revision = 1


def facts(*, candidate=200, control=200, candidate_failed=0, control_failed=0, actions=0):
    out: list[Fact] = []
    for _ in range(candidate - candidate_failed):
        out.append(Fact(TRACK_CANDIDATE))
    for _ in range(candidate_failed):
        out.append(Fact(TRACK_CANDIDATE, status="failed"))
    for _ in range(control - control_failed):
        out.append(Fact(TRACK_CHAMPION))
    for _ in range(control_failed):
        out.append(Fact(TRACK_CHAMPION, status="failed"))
    for _ in range(actions):
        out.append(Fact(TRACK_CANDIDATE, has_action=True))
        out.append(Fact(TRACK_CHAMPION, has_action=True))
    return out


#: Feedback SĂNĂTOS, cu eșantion suficient. E în fixture-ul implicit fiindcă altfel packetul
#: „complet" ar fi `INSUFFICIENT` — corect, dar ar face testele de mai jos să testeze altceva.
HEALTHY_FEEDBACK = {
    "by_release_track": {
        "candidate": {"n": 300, "positive": 285},
        "champion": {"n": 300, "positive": 280},
    }
}


def packet(**kw):
    defaults = dict(
        business_id=BID,
        window_from=T0 - timedelta(hours=72),
        window_to=T0,
        # 200 de ture de acțiune per cohort: sub ~150, limita Wilson pentru 0 eșecuri nu poate
        # exclude o regresie de 5pp față de un control la 0% — poarta ar rămâne `INSUFFICIENT`,
        # corect, dar restul testelor ar măsura altceva.
        facts=facts(actions=200),
        feedback_artifact=HEALTHY_FEEDBACK,
        slo_artifact={"verdict": "PASS", "policy_version": "slo_policy.v1"},
        quality_artifact={"verdict": "PASS"},
        e2e_artifact={"verdict": "PASS"},
        deploy_evidence={"verdict": "READY"},
        artifact_hashes={
            "quality_packet_hash": "sha256:q",
            "e2e_packet_hash": "sha256:e",
            "deploy_manifest_hash": "sha256:d",
        },
        generated_at=T0.isoformat(),
    )
    defaults.update(kw)
    return build_packet(kw.pop("policy", None) or policy(percent=5), **defaults)


# ── Agregare ────────────────────────────────────────────────────────────────────────────────
def test_agregarea_separa_cohorturile():
    stats = aggregate_cohorts(facts(candidate=10, control=20, candidate_failed=3))
    assert stats[TRACK_CANDIDATE].turns == 10
    assert stats[TRACK_CANDIDATE].failed == 3
    assert stats[TRACK_CHAMPION].turns == 20
    assert stats[TRACK_CHAMPION].failed == 0


def test_un_sample_cu_durata_negativa_se_arunca_si_nu_intra_in_percentile():
    """Ceas sărit între accept și commit: un p50 negativ ar face candidate să pară instantaneu."""
    stats = aggregate_cohorts([Fact(TRACK_CANDIDATE, duration_ms=-5000), Fact(TRACK_CANDIDATE)])
    assert len(stats[TRACK_CANDIDATE].durations_ms) == 1


def test_turele_fara_captura_ajung_in_cohortul_unknown():
    stats = aggregate_cohorts([Fact("unknown"), Fact(TRACK_CANDIDATE)])
    assert stats["unknown"].turns == 1


# ── Verdicte ────────────────────────────────────────────────────────────────────────────────
def test_pachetul_complet_si_sanatos_trece():
    p = packet()
    assert p.verdict == VERDICT_PASS
    assert p.stage == "3-pilot"


def test_un_hard_stop_bate_tot_restul_verde():
    """Chiar cu feedback pozitiv și cifre bune: un preț inventat oprește progresia."""
    p = packet(
        hard_stops=["invented_fact"],
        feedback_artifact={
            "by_release_track": {
                "candidate": {"n": 500, "positive": 495},
                "champion": {"n": 500, "positive": 400},
            }
        },
    )  # candidate are feedback MAI BUN decât control — și tot nu contează
    assert p.verdict == VERDICT_FAIL
    assert "hard_stops" in p.gates.blocking


def test_lipsa_cohortului_candidate_da_unknown_nu_pass():
    p = packet(facts=facts(candidate=0, control=200))
    assert p.verdict == VERDICT_UNKNOWN


def test_fereastra_trunchiata_nu_poate_fi_pass():
    assert packet(truncated=True).verdict == VERDICT_UNKNOWN


def test_artefact_de_la_alt_release_da_fail():
    """Un raport verde de acum trei zile n-are voie să promoveze digestul de azi."""
    p = packet(
        artifact_hashes={
            "quality_packet_hash": "sha256:ALTCEVA",
            "e2e_packet_hash": "sha256:e",
            "deploy_manifest_hash": "sha256:d",
        }
    )
    assert p.verdict == VERDICT_FAIL
    assert "artifact_match" in p.gates.blocking


def test_artefact_lipsa_da_unknown_nu_fail():
    """Distincția care contează: „n-am comparat" cere altă acțiune decât „am comparat și diferă"."""
    p = packet(artifact_hashes={})
    gate = next(g for g in p.gates.gates if g.name == "artifact_match")
    assert gate.verdict == VERDICT_UNKNOWN


def test_verdict_upstream_not_ready_blocheaza_promovarea():
    """Starea REALĂ de azi: NX-238 e NOT-READY, gate-ul de calitate NX-246 la fel."""
    p = packet(quality_artifact={"verdict": "NOT-READY"})
    assert p.verdict != VERDICT_PASS
    assert "quality_holdout" in p.gates.blocking


def test_terminal_gol_e_fail_indiferent_de_proportie():
    """P6 are prag zero: o singură violare e verdict, nu o rată sub un prag."""
    bad = facts()
    bad.append(Fact(TRACK_CANDIDATE, renderable=False))
    p = packet(facts=bad)
    assert p.verdict == VERDICT_FAIL
    assert "non_empty_terminal" in p.gates.blocking


def test_regresia_pe_cohortul_de_actiuni_opreste_media_globala_inselatoare():
    """Candidate e bun global, dar prost DOAR pe acțiuni — media n-are voie să ascundă asta."""
    # Volum mare de text sănătos, ca rata GLOBALĂ să rămână sub marjă (50/2050 ≈ 2,4%),
    # dar cohortul de acțiuni să fie catastrofal (50/50 eșecuri).
    data = facts(candidate=2000, control=2000)
    for _ in range(50):
        data.append(Fact(TRACK_CANDIDATE, has_action=True, status="failed"))
        data.append(Fact(TRACK_CHAMPION, has_action=True))
    p = packet(facts=data)
    action_gate = next(g for g in p.gates.gates if g.name == "action_cohort_failure_rate")
    assert action_gate.verdict == VERDICT_FAIL
    global_gate = next(g for g in p.gates.gates if g.name == "terminal_failure_rate")
    assert global_gate.verdict != VERDICT_FAIL, "fixture-ul trebuie să fie sănătos global"
    assert p.verdict == VERDICT_FAIL


def test_feedback_absent_nu_devine_verde():
    """Fără artefact de feedback, poarta rămâne `INSUFFICIENT` — nu se presupune „ca la control"."""
    gate = next(
        g for g in packet(feedback_artifact=None).gates.gates if g.name == "negative_feedback_rate"
    )
    assert gate.verdict == VERDICT_INSUFFICIENT
    assert packet(feedback_artifact=None).verdict != VERDICT_PASS


def test_feedback_negativ_peste_marja_da_fail():
    p = packet(
        feedback_artifact={
            "by_release_track": {
                "candidate": {"n": 200, "positive": 100},
                "champion": {"n": 200, "positive": 190},
            }
        }
    )
    gate = next(g for g in p.gates.gates if g.name == "negative_feedback_rate")
    assert gate.verdict == VERDICT_FAIL
    assert gate.detail["margin_pp"] == NON_INFERIORITY_MARGIN_PP


def test_esantion_prea_mic_pentru_etapa_da_insufficient():
    p = packet(facts=facts(candidate=40, control=40, actions=20))
    stage_gate = next(g for g in p.gates.gates if g.name == "stage_window")
    assert stage_gate.verdict == VERDICT_INSUFFICIENT
    assert p.verdict != VERDICT_PASS


# ── Alocare vs trafic observat ──────────────────────────────────────────────────────────────
def test_raportul_arata_si_alocarea_si_traficul_observat():
    """5% conversații poate însemna 12% ture — și asta nu e un bug, e o proprietate."""
    p = packet(facts=facts(candidate=300, control=700)).as_dict()
    assert p["allocation"]["policy_percent"] == 5
    assert p["allocation"]["observed_turn_share"] == 0.3
    assert p["allocation"]["candidate_turns"] == 300


def test_turele_fara_captura_sunt_raportate_separat():
    data = facts(candidate=100, control=100) + [Fact("unknown") for _ in range(7)]
    p = packet(facts=data).as_dict()
    assert p["allocation"]["uncaptured_turns"] == 7


def test_costul_pe_tur_e_declarat_lipsa_nu_aproximat():
    """`web_turns` n-are coloană de cost. Un buget verificat pe o aproximare e neverificat."""
    assert "cost_per_turn_by_track" in packet().as_dict()["completeness"]["missing"]


# ── Determinism + siguranță ─────────────────────────────────────────────────────────────────
def test_amprenta_e_determinista_si_ignora_momentul_generarii():
    a = packet(generated_at="2026-08-18T10:00:00+00:00")
    b = packet(generated_at="2026-08-19T23:59:00+00:00")
    assert a.fingerprint == b.fingerprint
    assert a.as_dict()["generated_at"] != b.as_dict()["generated_at"]


def test_amprenta_se_schimba_daca_se_schimba_cifrele():
    assert packet().fingerprint != packet(facts=facts(candidate=199, actions=200)).fingerprint


def _walk(node):
    if isinstance(node, dict):
        for k, v in node.items():
            yield str(k)
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)
    else:
        yield str(node)


def test_pachetul_nu_contine_identificatori():
    """Scanare RECURSIVĂ: nu ne bazăm pe cine adaugă mâine un câmp să-și amintească regula."""
    payload = packet().as_dict()
    for value in _walk(payload):
        assert not UUID_RE.search(value), f"identificator în packet: {value!r}"
    assert BID not in json.dumps(payload)
    assert payload["tenant_bucket"].startswith("t")


def test_pachetul_nu_copiaza_artefactele_upstream_intregi():
    """Citate, nu copiate: altfel ar aduce înăuntru exact ce n-are voie să conțină."""
    p = packet(
        slo_artifact={"verdict": "PASS", "business_id": BID, "slis": [{"name": "x"}]}
    ).as_dict()
    assert p["upstream"]["slo"] == {
        "present": True,
        "verdict": "PASS",
        "policy_version": None,
    }


def test_riscurile_deschise_se_deriva_din_porti():
    """O listă scrisă de mână ar rămâne goală exact când raportul e roșu."""
    p = packet(hard_stops=["cross_tenant_leak"]).as_dict()
    assert any("hard_stops" in r for r in p["open_risks"])


def test_pachetul_spune_ca_pass_nu_promoveaza():
    p = packet().as_dict()
    assert p["human_decision"]["decision"] is None
    assert "nu promovează" in p["gates"]["promotion"]


# ── Artefacte ───────────────────────────────────────────────────────────────────────────────
def test_artefact_lipsa_se_citeste_ca_none_fara_exceptie():
    assert load_artifact("nu/exista/artefact.json") is None
    assert artifact_hash("nu/exista/artefact.json") == ""


def test_hashul_de_artefact_normalizeaza_crlf(tmp_path):
    """Fără normalizare, același artefact ar avea alt hash pe Windows și pe CI."""
    unix = tmp_path / "a.json"
    win = tmp_path / "b.json"
    unix.write_bytes(b'{\n  "verdict": "PASS"\n}\n')
    win.write_bytes(b'{\r\n  "verdict": "PASS"\r\n}\r\n')
    assert artifact_hash(unix) == artifact_hash(win)


def test_artefact_ilizibil_nu_ridica(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{nu e json", encoding="utf-8")
    assert load_artifact(bad) is None


@pytest.mark.parametrize("payload", ["[]", '"text"', "42"])
def test_artefact_care_nu_e_obiect_e_respins(tmp_path, payload):
    f = tmp_path / "x.json"
    f.write_text(payload, encoding="utf-8")
    assert load_artifact(f) is None
