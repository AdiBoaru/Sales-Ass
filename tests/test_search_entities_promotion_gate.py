"""NX-238 — poarta de promovare: candidatul NU poate fi selectat fără un GO semnat.

Fișierul ăsta e scris ca planul de atac al lui Codex, punct cu punct: ștergem artefactul,
falsificăm verdictul, recalculăm amprenta fără cheie, semnăm cu altă cheie, mutăm manifestul.
Fiecare atac trebuie să se termine în același loc — `current_live` — cu un cod de blocare distinct,
ca dashboardul să spună CARE apărare a ținut, nu doar că „n-a mers".
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from src.retrieval.selector import (
    BLOCK_ARTIFACT_MALFORMED,
    BLOCK_ARTIFACT_MISSING,
    BLOCK_FINGERPRINT_MISMATCH,
    BLOCK_MANIFEST_DRIFT,
    BLOCK_NO_SIGNING_KEY,
    BLOCK_NOT_GO,
    BLOCK_SIGNATURE_INVALID,
    BLOCK_UNSIGNED,
    VERDICT_GO,
    VERDICT_NO_GO,
    VERDICT_NOT_READY,
    compute_fingerprint,
    load_decision,
    select_provider,
    sign_fingerprint,
    verify_decision,
)

KEY = "cheia-corecta"
ARTIFACT = Path("reports/nx238/decision.json")


@dataclass
class _Settings:
    retrieval_candidate_enabled: bool = True
    retrieval_candidate_rollout_pct: int = 100
    retrieval_decision_path: str = ""
    retrieval_decision_key: str = KEY
    retrieval_pipeline_version: str = "retrieval.v1"


def _payload(**overrides) -> dict:
    payload = {
        "card": "NX-238",
        "verdict": VERDICT_GO,
        "decided_by": "Adi Boaru",
        "decided_at": "2026-08-13",
        "manifest": {"commit": "abc123", "retrieval_qrels_sha256": "sha256:dead"},
        "blocking_codes": [],
    }
    payload.update(overrides)
    return payload


def _write(tmp_path, payload: dict, *, sign_with: str | None = KEY) -> str:
    payload = dict(payload)
    payload["fingerprint"] = compute_fingerprint(payload)
    payload["signature"] = sign_fingerprint(payload["fingerprint"], sign_with) if sign_with else ""
    path = tmp_path / "decision.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)


def _block(tmp_path, payload_or_path, *, key: str = KEY, expected_manifest=None) -> str | None:
    path = (
        payload_or_path if isinstance(payload_or_path, str) else _write(tmp_path, payload_or_path)
    )
    decision, load_block = load_decision(path)
    if load_block:
        return load_block
    return verify_decision(decision, key=key, expected_manifest=expected_manifest)


# --- Artefactul REAL din repo -------------------------------------------------------


def test_the_committed_artifact_is_not_ready_and_is_not_promotable():
    """Verdictul măsurat, versionat în repo: NOT_READY, nesemnat, nepromovabil."""
    decision, load_block = load_decision(ARTIFACT)

    assert load_block is None, "artefactul de decizie trebuie să existe și să fie valid"
    assert decision is not None
    assert decision.verdict == VERDICT_NOT_READY
    assert decision.decided_by == ""
    assert decision.signature == ""
    assert verify_decision(decision, key=KEY) == BLOCK_NOT_GO


def test_the_committed_artifact_records_the_real_blockers():
    """Blockerii nu sunt decorativi: sunt exact ce a raportat readiness-ul."""
    decision, _ = load_decision(ARTIFACT)
    assert decision is not None
    codes = set(decision.blocking_codes)

    assert "quality_h3_sample_too_small" in codes
    assert "quality_holdout_unavailable" in codes
    assert "decision_policy_unavailable" in codes
    assert "nx209_retrieval_gate_blocked" in codes


def test_the_committed_artifact_keeps_the_candidate_off_end_to_end(monkeypatch):
    """Chiar cu flagul PORNIT și ramp 100%, artefactul real ține candidatul afară."""
    cfg = _Settings(retrieval_decision_path=str(ARTIFACT))
    selection = select_provider(business_id="biz-1", conversation_id="conv-1", settings=cfg)

    assert selection.provider_version == "current_live.v1"
    assert selection.blocking_code == BLOCK_NOT_GO


# --- Atac 1: ștergerea / falsificarea artefactului ----------------------------------


def test_attack_delete_artifact(tmp_path):
    assert _block(tmp_path, str(tmp_path / "sters.json")) == BLOCK_ARTIFACT_MISSING


def test_attack_flip_verdict_to_go_breaks_the_fingerprint(tmp_path):
    """Editezi `verdict` la GO în fișierul semnat → amprenta nu mai corespunde conținutului."""
    path = Path(_write(tmp_path, _payload(verdict=VERDICT_NO_GO)))
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["verdict"] = VERDICT_GO  # amprenta rămâne cea a lui NO_GO
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert _block(tmp_path, str(path)) == BLOCK_FINGERPRINT_MISMATCH


def test_attack_recompute_fingerprint_without_the_key(tmp_path):
    """Recalculezi amprenta corect, dar semnătura veche nu mai e a ei → invalidă."""
    path = Path(_write(tmp_path, _payload(verdict=VERDICT_NO_GO)))
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["verdict"] = VERDICT_GO
    payload["fingerprint"] = compute_fingerprint(payload)  # amprentă corectă…
    path.write_text(json.dumps(payload), encoding="utf-8")  # …semnătură veche

    assert _block(tmp_path, str(path)) == BLOCK_SIGNATURE_INVALID


def test_attack_sign_with_a_different_key(tmp_path):
    assert _block(tmp_path, _payload(), key=KEY) is None
    path = _write(tmp_path, _payload(), sign_with="cheia-atacatorului")
    assert _block(tmp_path, path) == BLOCK_SIGNATURE_INVALID


def test_attack_strip_the_signature(tmp_path):
    assert _block(tmp_path, _payload(), key=KEY) is None
    assert _block(tmp_path, _write(tmp_path, _payload(), sign_with=None)) == BLOCK_UNSIGNED


def test_attack_unsigned_go_without_a_human(tmp_path):
    """Software-ul nu poate emite GO: fără `decided_by`, verdictul nu e o decizie."""
    assert _block(tmp_path, _payload(decided_by="")) == BLOCK_UNSIGNED


def test_attack_malformed_and_unknown_verdicts(tmp_path):
    path = tmp_path / "decision.json"
    path.write_text("{nu e json", encoding="utf-8")
    assert _block(tmp_path, str(path)) == BLOCK_ARTIFACT_MALFORMED

    path.write_text(json.dumps({"verdict": "APROAPE_GATA"}), encoding="utf-8")
    assert _block(tmp_path, str(path)) == BLOCK_ARTIFACT_MALFORMED


def test_runtime_without_a_key_cannot_believe_any_go(tmp_path):
    """Fără cheie configurată nu putem VERIFICA nimic, deci nu avem voie să credem nimic."""
    assert _block(tmp_path, _payload(), key="") == BLOCK_NO_SIGNING_KEY


# --- Atac 2: drift de manifest -------------------------------------------------------


def test_manifest_drift_invalidates_a_valid_go(tmp_path):
    """Catalogul/qrels-ul s-au mișcat sub verdict → decizia a fost luată pe altă lume."""
    path = _write(tmp_path, _payload())
    assert _block(tmp_path, path, expected_manifest={"commit": "abc123"}) is None
    assert _block(tmp_path, path, expected_manifest={"commit": "altceva"}) == BLOCK_MANIFEST_DRIFT


# --- Verdicte legitime care NU promovează -------------------------------------------


@pytest.mark.parametrize("verdict", [VERDICT_NO_GO, VERDICT_NOT_READY])
def test_legitimate_non_go_verdicts_are_valid_artifacts_but_never_promote(tmp_path, verdict):
    """NO-GO și NOT-READY sunt rezultate LEGITIME: artefact valid, promovare zero."""
    path = _write(tmp_path, _payload(verdict=verdict))
    assert _block(tmp_path, path) == BLOCK_NOT_GO

    selection = select_provider(
        business_id="biz-1",
        conversation_id="conv-1",
        settings=_Settings(retrieval_decision_path=path),
    )
    assert selection.provider_version == "current_live.v1"


def test_a_correctly_signed_go_is_the_only_thing_that_promotes(tmp_path):
    """Control pozitiv: dacă poarta n-ar lăsa NIMIC să treacă, testele de sus n-ar dovedi nimic."""
    path = _write(tmp_path, _payload())
    assert _block(tmp_path, path) is None

    selection = select_provider(
        business_id="biz-1",
        conversation_id="conv-1",
        settings=_Settings(retrieval_decision_path=path),
    )
    assert selection.provider_version == "search_entities.v1"
