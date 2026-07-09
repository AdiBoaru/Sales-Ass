"""NX-148 — teste pure (fără DB): whitelist + DomainPack (felia 1) + facts_block + extractor
parsing (felia 2)."""

from types import SimpleNamespace

from src.db.queries.facts import select_whitelisted_facts
from src.domain.loader import load_domain_pack
from src.models import BusinessConfig
from src.worker.context import facts_block
from src.worker.profile import ProfileDelta, build_profile_prompt

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


# --- felia 2: facts_block (injectare) + extractor parsing --------------------


def test_facts_block_formats_and_budgets():
    ctx = SimpleNamespace(
        facts=[
            {"fact_type": "budget_band", "fact_value": "100-200"},
            {"fact_type": "skin_type", "fact_value": "sensitive"},
        ]
    )
    out = facts_block(ctx)
    assert out.startswith("Ce știu despre client:")
    # NX-160: etichete prezentabile (nu snake_case brut) — canonical/raw humanizat.
    assert "Buget: 100-200" in out
    assert "Tip de ten: sensitive" in out


def test_facts_block_empty_when_no_facts():
    assert facts_block(SimpleNamespace(facts=[])) == ""
    assert facts_block(SimpleNamespace(facts=None)) == ""


def test_facts_block_skips_empty_values():
    ctx = SimpleNamespace(facts=[{"fact_type": "budget_band", "fact_value": ""}])
    assert facts_block(ctx) == ""


def test_profile_delta_parses_facts():
    d = ProfileDelta.model_validate(
        {
            "profile_patch": {"skin_type": "dry"},
            "facts": [{"fact_type": "budget_band", "fact_value": "100", "confidence": 0.8}],
        }
    )
    assert len(d.facts) == 1
    assert d.facts[0].fact_type == "budget_band"
    assert d.facts[0].confidence == 0.8


def test_profile_delta_facts_default_empty():
    d = ProfileDelta.model_validate({"profile_patch": {}})
    assert d.facts == []


def test_prompt_requests_facts_only_when_enabled():
    msg = SimpleNamespace(body="caut o cremă sub 100 lei")
    sys_on, user_on = build_profile_prompt([], msg, "ro", include_facts=True)
    sys_off, user_off = build_profile_prompt([], msg, "ro", include_facts=False)

    # ON: promptul cere facts (cheia JSON + instrucțiunea din user).
    assert '"facts"' in sys_on
    assert "facts" in user_on
    # OFF (kill-switch): feature-flag COMPLET oprit — nimic despre facts în prompt.
    assert '"facts"' not in sys_off
    assert "facts" not in user_off
    # profile/lead rămân în ambele (extractorul de profil nu e afectat).
    assert "profile_patch" in sys_off and "lead_signals" in sys_off
