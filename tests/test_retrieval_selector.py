"""NX-238 — selectorul: bucket stabil, pipeline version, kill switch, fallback canonic."""

from __future__ import annotations

import json
from dataclasses import dataclass

from src.retrieval.current_live import CurrentLiveRetrievalAdapter
from src.retrieval.search_entities import SearchEntitiesAdapter
from src.retrieval.selector import (
    BLOCK_ARTIFACT_MISSING,
    BLOCK_FLAG_OFF,
    BLOCK_OUT_OF_BUCKET,
    VERDICT_GO,
    ProviderSelection,
    build_port,
    compute_fingerprint,
    select_provider,
    sign_fingerprint,
    stable_bucket,
)

KEY = "test-decision-key"


@dataclass
class _Settings:
    """Doar câmpurile pe care le citește selectorul (evită un Settings complet în teste)."""

    retrieval_candidate_enabled: bool = True
    retrieval_candidate_rollout_pct: int = 100
    retrieval_decision_path: str = ""
    retrieval_decision_key: str = KEY
    retrieval_pipeline_version: str = "retrieval.v1"


def _signed_go(tmp_path, **overrides) -> str:
    payload = {
        "card": "NX-238",
        "verdict": VERDICT_GO,
        "decided_by": "Adi Boaru",
        "decided_at": "2026-08-13",
        "manifest": {"commit": "abc123"},
        "blocking_codes": [],
    }
    payload.update(overrides)
    payload["fingerprint"] = compute_fingerprint(payload)
    payload["signature"] = sign_fingerprint(payload["fingerprint"], KEY)
    path = tmp_path / "decision.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)


# --- Bucket stabil -----------------------------------------------------------------


def test_bucket_is_deterministic_and_in_range():
    first = stable_bucket("biz-1", "conv-1")
    assert first == stable_bucket("biz-1", "conv-1")  # stabil între apeluri
    assert 0 <= first <= 99


def test_bucket_differs_per_tenant_and_per_conversation():
    """Bucketul separă tenanții ȘI conversațiile — un ramp nu poate lovi un tenant întreg."""
    buckets = {
        stable_bucket("biz-1", "conv-1"),
        stable_bucket("biz-2", "conv-1"),
        stable_bucket("biz-1", "conv-2"),
    }
    assert len(buckets) > 1


def test_same_conversation_keeps_its_provider_across_turns(tmp_path):
    """Nu comutăm providerul în mijlocul conversației: selecția e o funcție pură de (biz, conv)."""
    cfg = _Settings(
        retrieval_decision_path=_signed_go(tmp_path), retrieval_candidate_rollout_pct=50
    )
    picks = {
        select_provider(
            business_id="biz-1", conversation_id="conv-42", settings=cfg
        ).provider_version
        for _ in range(5)
    }
    assert len(picks) == 1


# --- Kill switch și ramp ------------------------------------------------------------


def test_kill_switch_off_never_touches_the_decision_artifact(tmp_path):
    """OFF e prima verificare: nici măcar nu citim de pe disc."""
    cfg = _Settings(
        retrieval_candidate_enabled=False,
        retrieval_decision_path=str(tmp_path / "nu-exista.json"),
    )
    selection = select_provider(business_id="biz-1", conversation_id="conv-1", settings=cfg)

    assert selection.provider_version == "current_live.v1"
    assert selection.blocking_code == BLOCK_FLAG_OFF
    assert selection.reason == "kill_switch_off"


def test_rollout_zero_keeps_everyone_on_current_live_even_with_signed_go(tmp_path):
    """Deploy dark: cod prezent, GO semnat, dar zero conversații pe candidat."""
    cfg = _Settings(retrieval_decision_path=_signed_go(tmp_path), retrieval_candidate_rollout_pct=0)
    selection = select_provider(business_id="biz-1", conversation_id="conv-1", settings=cfg)

    assert selection.provider_version == "current_live.v1"
    assert selection.blocking_code == BLOCK_OUT_OF_BUCKET
    assert selection.bucket is not None


def test_full_rollout_with_signed_go_selects_the_candidate(tmp_path):
    cfg = _Settings(retrieval_decision_path=_signed_go(tmp_path))
    selection = select_provider(business_id="biz-1", conversation_id="conv-1", settings=cfg)

    assert selection.provider_version == "search_entities.v1"
    assert selection.is_candidate is True
    assert selection.reason == "signed_go_canary"
    assert selection.blocking_code is None


def test_kill_switch_returns_to_current_live_in_one_flag(tmp_path):
    """Rollback: același artefact semnat, flagul stins → traseul live, fără migrare."""
    path = _signed_go(tmp_path)
    assert select_provider(
        business_id="biz-1",
        conversation_id="conv-1",
        settings=_Settings(retrieval_decision_path=path),
    ).is_candidate
    assert not select_provider(
        business_id="biz-1",
        conversation_id="conv-1",
        settings=_Settings(retrieval_decision_path=path, retrieval_candidate_enabled=False),
    ).is_candidate


# --- Pipeline version --------------------------------------------------------------


def test_pipeline_version_is_captured_in_the_selection(tmp_path):
    cfg = _Settings(
        retrieval_decision_path=_signed_go(tmp_path), retrieval_pipeline_version="retrieval.v7"
    )
    selection = select_provider(business_id="biz-1", conversation_id="conv-1", settings=cfg)
    assert selection.pipeline_version == "retrieval.v7"


def test_missing_artifact_falls_back_without_raising(tmp_path):
    cfg = _Settings(retrieval_decision_path=str(tmp_path / "lipsa.json"))
    selection = select_provider(business_id="biz-1", conversation_id="conv-1", settings=cfg)

    assert selection.provider_version == "current_live.v1"
    assert selection.blocking_code == BLOCK_ARTIFACT_MISSING


# --- build_port ---------------------------------------------------------------------


def test_build_port_returns_the_adapter_named_by_the_selection():
    live = build_port(
        object(),
        object(),
        ProviderSelection("current_live.v1", "x", "retrieval.v1"),
    )
    candidate = build_port(
        object(),
        object(),
        ProviderSelection("search_entities.v1", "x", "retrieval.v1"),
    )
    assert isinstance(live, CurrentLiveRetrievalAdapter)
    assert isinstance(candidate, SearchEntitiesAdapter)


def test_unknown_provider_degrades_to_current_live_instead_of_raising():
    """Principiul 6: o valoare necunoscută nu rupe turul; cade pe traseul canonic."""
    port = build_port(
        object(), object(), ProviderSelection("provider-inventat", "x", "retrieval.v1")
    )
    assert isinstance(port, CurrentLiveRetrievalAdapter)
