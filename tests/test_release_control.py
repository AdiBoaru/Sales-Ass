"""NX-249 — CLI-ul de release: dry-run implicit, dovezi obligatorii, kill-switch fără dovezi.

Trei reguli, fiecare cu un mod concret de a face rău dacă lipsește:

  1. **`apply` fără `--confirm` nu scrie nimic.** Un kill-switch apăsat din greșeală în timp ce
     citești `--help` e un kill-switch care nu va fi folosit când trebuie.
  2. **Promovarea cere un packet cu verdict PASS și amprentă RECALCULATĂ.** Fără recalculare,
     „PASS" ar fi un cuvânt scris de cine vrea, iar `apply` ar deveni un formular de bifat.
  3. **Oprirea NU cere dovezi.** A cere un raport ca să oprești traficul e exact invers față de
     siguranță — de asta `force_control` sare peste poarta de evidence.

Testele nu ating DB-ul: `_cmd_apply` e testat prin funcțiile pure pe care se sprijină, iar CAS-ul
propriu-zis are testele lui în `test_release_policy.py`.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from scripts.release_control import (
    EXIT_INVALID,
    EXIT_OK,
    _cmd_plan,
    _cmd_validate,
    _evidence_ok,
    _load_policy,
)
from src.release.models import PolicyError

T0 = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
BID = "6098812a-50fc-44bd-a1ba-bc77e6399158"


def policy_file(tmp_path, **over):
    data = {
        "policy_id": "nx249-pilot",
        "revision": 0,
        "environment": "test",
        "created_at": (T0 - timedelta(hours=2)).isoformat(),
        "not_before": (T0 - timedelta(hours=1)).isoformat(),
        "expires_at": (T0 + timedelta(days=7)).isoformat(),
        "control_release_sha": "c0ntr0l1234567",
        "control_pipeline_version": "web-chat.v1",
        "candidate_release_sha": "cand1date7654321",
        "candidate_pipeline_version": "web-view.v2",
        "mode": "canary",
        "percent": 5,
        "stage": 3,
        "eligible_business_ids": [BID],
        "stable_salt_id": "salt-1",
        "quality_packet_hash": "sha256:q",
        "e2e_packet_hash": "sha256:e",
        "deploy_manifest_hash": "sha256:d",
        "slo_policy_version": "slo_policy.v1",
        "quality_policy_version": "nx246-gate-v1",
        "approved_by": "adi",
        "approved_at": T0.isoformat(),
        "change_ticket": "NX-249",
    }
    data.update(over)
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


class Args:
    def __init__(self, **kw):
        self.__dict__.update(kw)


# ── validate ────────────────────────────────────────────────────────────────────────────────
def test_validate_accepta_un_policy_corect(tmp_path, capsys):
    assert _cmd_validate(Args(policy=str(policy_file(tmp_path)))) == EXIT_OK
    out = capsys.readouterr().out
    assert "VALID" in out
    assert "3-pilot" in out
    assert "≥200 ture candidate" in out


def test_validate_respinge_un_policy_stricat(tmp_path, capsys):
    bad = policy_file(tmp_path, percent=7)
    assert _cmd_validate(Args(policy=str(bad))) == EXIT_INVALID
    assert "INVALID" in capsys.readouterr().err


def test_validate_respinge_un_fisier_inexistent(capsys):
    assert _cmd_validate(Args(policy="nu/exista.json")) == EXIT_INVALID


def test_incarcarea_ridica_policy_error_pe_continut_gresit(tmp_path):
    bad = tmp_path / "x.json"
    bad.write_text('{"policy_id": "x"}', encoding="utf-8")
    with pytest.raises(PolicyError):
        _load_policy(str(bad))


# ── plan ────────────────────────────────────────────────────────────────────────────────────
def test_plan_nu_scrie_niciun_identificator_real(tmp_path, capsys):
    """Manual drive-ul cardului cere explicit „zero ID-uri în report labels"."""
    _cmd_plan(Args(policy=str(policy_file(tmp_path)), ids=1000))
    out = capsys.readouterr().out
    assert "plan-conversation" not in out
    assert BID not in out
    assert "bucketuri ocupate" in out
    assert "χ²" in out


def test_plan_raporteaza_o_proportie_apropiata_de_procent(tmp_path, capsys):
    _cmd_plan(Args(policy=str(policy_file(tmp_path, percent=20, stage=4)), ids=5000))
    out = capsys.readouterr().out
    line = next(ln for ln in out.splitlines() if "în canary" in ln)
    pct = float(line.split("(")[1].split("%")[0].replace(",", "."))
    assert 17.0 <= pct <= 23.0, line


# ── evidence ────────────────────────────────────────────────────────────────────────────────
def packet_file(tmp_path, *, verdict="PASS", tamper=False):
    import hashlib

    body = {"schema_version": "release-evidence-packet.v1", "verdict": verdict, "stage": "3-pilot"}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    payload = dict(body)
    payload["fingerprint"] = "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()
    payload["generated_at"] = T0.isoformat()
    if tamper:
        payload["verdict"] = "PASS"  # amprenta rămâne cea a conținutului VECHI
    path = tmp_path / "packet.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_packet_pass_cu_amprenta_valida_e_acceptat(tmp_path):
    ok, detail = _evidence_ok(str(packet_file(tmp_path)))
    assert ok
    assert detail.startswith("sha256:")


def test_packet_editat_manual_e_respins(tmp_path):
    """Cine schimbă „FAIL" în „PASS" îi rupe amprenta — asta e toată poanta recalculării."""
    ok, detail = _evidence_ok(str(packet_file(tmp_path, verdict="FAIL", tamper=True)))
    assert not ok
    assert "amprenta" in detail


def test_packet_cu_verdict_care_nu_e_pass_e_respins(tmp_path):
    ok, detail = _evidence_ok(str(packet_file(tmp_path, verdict="INSUFFICIENT")))
    assert not ok
    assert "INSUFFICIENT" in detail


def test_packet_absent_e_respins():
    ok, detail = _evidence_ok("")
    assert not ok
    assert "lipsește" in detail


def test_packet_ilizibil_e_respins(tmp_path):
    bad = tmp_path / "p.json"
    bad.write_text("{nu e json", encoding="utf-8")
    ok, _ = _evidence_ok(str(bad))
    assert not ok


def test_generated_at_nu_participa_la_amprenta(tmp_path):
    """Altfel un packet regenerat identic ar fi respins ca „editat"."""
    path = packet_file(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["generated_at"] = "2030-01-01T00:00:00+00:00"
    path.write_text(json.dumps(payload), encoding="utf-8")
    ok, _ = _evidence_ok(str(path))
    assert ok
