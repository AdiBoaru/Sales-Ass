"""P0-safety (CONV-COMMERCE) — guardrail pe sfat MEDICAL/beauty (răspundere juridică).

Funcții PURE + integrare validator/scrub/prompt. ZERO LLM/DB. Acoperă: detectorul
`has_medical_claim` (4 categorii periculoase + NU prinde cosmetice/redirect sigur), plasa de
proză (`_valid` → invalid), plasa căii bogate (`scrub_prose`/`scrub_intro` → DROP), kill-switch-ul,
și prezența instrucțiunii preventive în prompturi.
"""

from types import SimpleNamespace

import pytest

from src.agent import validator as val  # NX-142: safety kill-switch se citește din validator
from src.agent.prompt_builder import (
    PromptInputs,
    build_agent_system,
    build_reco_system,
    build_rich_system,
)
from src.worker import compose
from src.worker.stages.agent import _valid  # re-export
from src.worker.text_scrub import has_medical_claim

# --------------------------------------------------------------------------- #
# has_medical_claim — POZITIVE (claim periculos) și NEGATIVE (cosmetic / redirect sigur)
# --------------------------------------------------------------------------- #

_UNSAFE = [
    "Crema asta tratează acneea în două săptămâni.",  # tratează + afecțiune
    "Produsul vindecă eczema și dermatita.",  # vindecă + afecțiune
    "Combate psoriazisul eficient.",  # combate + afecțiune
    "Serul e sigur în sarcină.",  # verdict de siguranță în sarcină
    "Poți folosi în timpul alăptării fără grijă.",  # poți folosi + alăptare
    "100% fără alergeni, fără efecte adverse.",  # garanție absolută de inocuitate
    "Crema asta e recomandată de dermatolog.",  # falsă autoritate medicală
    "This product treats acne and is safe during pregnancy.",  # EN
]

_SAFE = [
    "Crema hidratează intens tenul uscat.",  # cosmetic
    "Reduce aspectul ridurilor și luminează tenul.",  # cosmetic (riduri ≠ afecțiune)
    "Pentru acnee, îți recomand să consulți un dermatolog.",  # redirect SIGUR (fără verb-tratament)
    "Pentru întrebări despre sarcină, te rog consultă medicul.",  # redirect SIGUR (fără «sigur»)
    "Ser cu vitamina C pentru strălucire și fermitate.",  # cosmetic
    "Calmează pielea iritată și roșeața.",  # «calmează» nu e verb terapeutic
    "",
    None,
]


@pytest.mark.parametrize("text", _UNSAFE)
def test_has_medical_claim_flags_unsafe(text):
    assert has_medical_claim(text) is True


@pytest.mark.parametrize("text", _SAFE)
def test_has_medical_claim_allows_safe(text):
    assert has_medical_claim(text) is False


# --------------------------------------------------------------------------- #
# Plasa de PROZĂ — _valid respinge un claim medical (gated de kill-switch)
# --------------------------------------------------------------------------- #

_PRODUCTS = [{"id": "p1", "name": "Crema A", "price": 80.0, "url": "https://shop/p1"}]


def _settings(monkeypatch, *, safety=True):
    monkeypatch.setattr(
        val,
        "get_settings",
        lambda: SimpleNamespace(
            validator_bare_numbers_enabled=False,
            validator_claims_enabled=False,
            validator_stock_claims_enabled=False,
            safety_medical_guardrail_enabled=safety,
        ),
    )


def test_valid_rejects_medical_claim(monkeypatch):
    _settings(monkeypatch, safety=True)
    # text fără preț/link inventat, dar cu claim medical → invalid (declanșează retry/fallback)
    assert _valid("Crema A tratează acneea.", _PRODUCTS) is False


def test_valid_accepts_cosmetic_claim(monkeypatch):
    _settings(monkeypatch, safety=True)
    assert _valid("Crema A hidratează tenul uscat.", _PRODUCTS) is True


def test_valid_safety_kill_switch_off_lets_claim_through(monkeypatch):
    _settings(monkeypatch, safety=False)  # OFF → claim medical nu mai e blocat de safety
    assert _valid("Crema A tratează acneea.", _PRODUCTS) is True


# --------------------------------------------------------------------------- #
# Plasa căii BOGATE — scrub_prose / scrub_intro DROP-uiesc câmpul cu claim medical
# --------------------------------------------------------------------------- #


def _compose_settings(monkeypatch, *, safety=True):
    monkeypatch.setattr(
        compose,
        "get_settings",
        lambda: SimpleNamespace(safety_medical_guardrail_enabled=safety),
    )


def test_scrub_prose_drops_medical_claim(monkeypatch):
    _compose_settings(monkeypatch, safety=True)
    assert compose.scrub_prose("Vindecă eczema rapid.") is None  # DROP
    assert compose.scrub_prose("Pentru pielea uscată, hidratare profundă.") is not None  # cosmetic


def test_scrub_prose_safety_off_keeps_claim(monkeypatch):
    _compose_settings(monkeypatch, safety=False)
    # safety OFF → scrub-ul medical nu mai rulează (NX-117 e separat, fără cifre/claim aici)
    assert compose.scrub_prose("Vindecă eczema rapid.") == "Vindecă eczema rapid."


def test_scrub_intro_drops_medical_claim(monkeypatch):
    _compose_settings(monkeypatch, safety=True)
    assert compose.scrub_intro("E sigur în sarcină.", set()) is None  # DROP
    assert compose.scrub_intro("Pentru mâini uscate.", set()) is not None  # cosmetic


# --------------------------------------------------------------------------- #
# Stratul PREVENTIV — instrucțiunea de siguranță e în toate prompturile agentului
# --------------------------------------------------------------------------- #


def test_safety_instruction_in_all_agent_prompts():
    inp = PromptInputs.build("Shop", "beauty", "ro", ["creme"], [])
    for prompt in (build_agent_system(inp), build_reco_system(inp), build_rich_system(inp)):
        low = prompt.lower()
        assert "sfat medical" in low or "siguranta" in low or "siguranță" in low
        assert "sarcin" in low  # menționează explicit sarcina/alăptarea
