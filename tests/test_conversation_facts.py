"""NX-148 felia 1 — teste pure (fără DB) pentru conversation_facts: whitelist + DomainPack."""

from src.db.queries.facts import select_whitelisted_facts
from src.domain.loader import load_domain_pack
from src.models import BusinessConfig

WL = frozenset({"budget_band", "skin_type", "fav_brands"})


def test_whitelist_drops_unknown_fact_type():
    facts = [
        {"fact_type": "budget_band", "fact_value": "100-200", "confidence": 0.9},
        {"fact_type": "phone", "fact_value": "0722000111", "confidence": 0.9},  # aruncat
    ]
    out = select_whitelisted_facts(facts, WL)
    assert {f["fact_type"] for f in out} == {"budget_band"}


def test_dedup_keeps_max_confidence():
    facts = [
        {"fact_type": "skin_type", "fact_value": "dry", "confidence": 0.4},
        {"fact_type": "skin_type", "fact_value": "sensitive", "confidence": 0.8},
    ]
    out = select_whitelisted_facts(facts, WL)
    assert len(out) == 1
    assert out[0]["fact_value"] == "sensitive"
    assert out[0]["confidence"] == 0.8


def test_skips_empty_values():
    facts = [{"fact_type": "budget_band", "fact_value": "", "confidence": 0.9}]
    assert select_whitelisted_facts(facts, WL) == []


def test_cap_and_confidence_ordering():
    facts = [{"fact_type": f"t{i}", "fact_value": i, "confidence": i / 20} for i in range(15)]
    wl = frozenset({f"t{i}" for i in range(15)})
    out = select_whitelisted_facts(facts, wl, cap=5)
    assert [f["fact_type"] for f in out] == ["t14", "t13", "t12", "t11", "t10"]


def test_whitelist_fail_closed_on_empty():
    # whitelist gol → NIMIC nu trece (fail-closed, P12) — un tip inventat nu se strecoară.
    facts = [{"fact_type": "phone", "fact_value": "0722000111", "confidence": 0.9}]
    assert select_whitelisted_facts(facts, frozenset()) == []


def test_confidence_clamped_to_unit_interval():
    facts = [
        {"fact_type": "budget_band", "fact_value": "x", "confidence": 999},
        {"fact_type": "skin_type", "fact_value": "y", "confidence": -5},
    ]
    out = {f["fact_type"]: f["confidence"] for f in select_whitelisted_facts(facts, WL)}
    assert out["budget_band"] == 1.0
    assert out["skin_type"] == 0.0


def test_lower_confidence_does_not_override_value():
    # „oily @ 0.20" NU trebuie să înlocuiască „sensitive @ 0.95" (memorie falsă sigură).
    facts = [
        {"fact_type": "skin_type", "fact_value": "sensitive", "confidence": 0.95},
        {"fact_type": "skin_type", "fact_value": "oily", "confidence": 0.20},
    ]
    out = select_whitelisted_facts(facts, WL)
    assert len(out) == 1
    assert out[0]["fact_value"] == "sensitive"
    assert out[0]["confidence"] == 0.95


def test_pii_redacted_in_fact_value():
    # tip PERMIS cu telefon în valoare → redactat (whitelist-ul de tip nu apără valoarea).
    facts = [
        {"fact_type": "budget_band", "fact_value": "nu suna la 0722 123 456", "confidence": 0.8}
    ]
    out = select_whitelisted_facts(facts, WL)
    assert "0722" not in out[0]["fact_value"]
    assert "***" in out[0]["fact_value"]


def test_domain_pack_loads_fact_type_whitelist():
    pack = load_domain_pack(BusinessConfig(id="b", slug="s", name="n", vertical="beauty"))
    assert pack is not None
    assert "skin_type" in pack.fact_type_whitelist
    assert "budget_band" in pack.fact_type_whitelist
    # profile_whitelist rămâne separat (mecanisme distincte, chiar dacă se suprapun parțial)
    assert isinstance(pack.fact_type_whitelist, frozenset)
