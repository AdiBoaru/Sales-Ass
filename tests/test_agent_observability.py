"""NX-146 felia 2 — teste pentru evenimentul agent_prompt (funcție pură)."""

from src.agent.observability import agent_prompt_event
from src.agent.validator import ValidationResult


def test_prompt_hash_stable_and_sensitive():
    a = agent_prompt_event("SYS", "user msg", [])
    b = agent_prompt_event("SYS", "user msg", [])
    c = agent_prompt_event("SYS v2", "user msg", [])

    assert a["prompt_hash"] == b["prompt_hash"]  # determinist pe același input
    assert a["prompt_hash"] != c["prompt_hash"]  # sensibil la schimbarea promptului
    assert len(a["prompt_hash"]) == 64  # sha256 hex


def test_retrieval_ids_extracted_ordered_deduped():
    retrieved = [
        {"product_id": "p1", "name": "A"},
        {"id": "p2", "name": "B"},
        {"product_id": "p1", "name": "A dup"},  # duplicat → sărit
        {"name": "fără id"},  # fără id → sărit
    ]
    ev = agent_prompt_event("s", "u", retrieved)

    assert ev["retrieval_ids"] == ["p1", "p2"]


def test_prompt_body_gated_by_kill_switch_and_redacted():
    off = agent_prompt_event("sistem", "sună la 0722 123 456", [])
    on = agent_prompt_event("sistem", "sună la 0722 123 456", [], store_prompt=True)

    assert "prompt_rendered" not in off  # default OFF → corpul NU se persistă
    assert "prompt_rendered" in on
    assert "0722" not in on["prompt_rendered"]  # redactat (P12)
    assert "***" in on["prompt_rendered"]


def test_empty_retrieved_is_empty_list():
    assert agent_prompt_event("s", "u", None)["retrieval_ids"] == []


def test_validator_absent_by_default():
    # DoD gap fixat: fără `validator`, evenimentul NU inventează un rezultat de validare.
    ev = agent_prompt_event("s", "u", [])
    assert "validator_ok" not in ev
    assert "validator_reasons" not in ev


def test_validator_ok_and_reasons_included_when_given():
    ok = agent_prompt_event("s", "u", [], validator=ValidationResult(ok=True, reasons=[]))
    assert ok["validator_ok"] is True
    assert ok["validator_reasons"] == []

    bad = agent_prompt_event(
        "s", "u", [], validator=ValidationResult(ok=False, reasons=["ungrounded_price"])
    )
    assert bad["validator_ok"] is False
    assert bad["validator_reasons"] == ["ungrounded_price"]
