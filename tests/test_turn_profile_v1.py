"""NX-304 — profilul de tur ajunge și pe calea v1, nu doar pe creierul unic.

Defectul pe care îl pinuiește: `routine_plan` intrase în toolsetul v1 la NX-297 și datele există
(pe SOLE, `attributes.routine_step` pe 2.019 din 2.758 de produse), dar `turn_profile.select` era
chemat EXCLUSIV în `brain.py`. Producția rulează calea v1, deci `ROUTINE_ENABLED=true` nu putea
schimba nimic: modelul primea o unealtă despre care nimic nu-i spunea să o cheme, `ctx.routine`
rămânea `None`, iar o cerere de rutină ieșea ca listă rankată de produse.

Fără DB și fără LLM: `_apply_turn_profile` e pur.
"""

from __future__ import annotations

import pytest

from src.config import get_settings
from src.domain.pack import DomainPack
from src.domain.routine_steps import build_spec
from src.models import BusinessConfig, Contact, InboundMessage, TurnContext
from src.worker.stages.agent import _apply_turn_profile

RAW = {
    "families": {"fata": ["curatare", "tratament", "hidratare"]},
    "by_product_type": {"gel de curatare": "fata:curatare"},
}

SYSTEM = "SYSTEM-BAZA"


def _ctx(body: str, *, with_families: bool = True) -> TurnContext:
    business = BusinessConfig(id="b", slug="d", name="D", vertical="ecommerce")
    business.domain_pack = (
        DomainPack(vertical="ecommerce", routine_steps=build_spec(RAW))
        if with_families
        else DomainPack(vertical="ecommerce")
    )
    ctx = TurnContext(
        turn_id="t",
        business=business,
        contact=Contact(id="c", business_id="b"),
        message=InboundMessage(provider_msg_id="m", body=body, channel_kind="webchat"),
        conversation_id="conv",
    )
    ctx.language = "ro"
    return ctx


@pytest.fixture
def _routine_on(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "routine_enabled", True, raising=False)
    monkeypatch.setattr(settings, "turn_profiles_enabled", False, raising=False)
    return settings


def test_cererea_de_rutina_primeste_sufixul_si_unealta(_routine_on):
    ctx = _ctx("vreau o rutina completa pentru ten")
    system, tools = _apply_turn_profile(ctx, SYSTEM, [])

    assert system != SYSTEM, "sufixul profilului nu s-a aplicat"
    assert system.startswith(SYSTEM), "prefixul static trebuie să rămână neatins (prompt caching)"
    assert "routine_plan" in system
    assert [t["function"]["name"] for t in tools] == ["routine_plan"]
    ev = [e for e in ctx.events if e.type == "turn_profile"]
    assert ev and ev[0].properties["name"] == "routine" and ev[0].properties["path"] == "v1"


def test_flagul_stins_lasa_turul_byte_identic(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "routine_enabled", False, raising=False)
    monkeypatch.setattr(settings, "turn_profiles_enabled", False, raising=False)
    ctx = _ctx("vreau o rutina completa pentru ten")
    system, tools = _apply_turn_profile(ctx, SYSTEM, [])

    assert system == SYSTEM and tools == []
    assert not [e for e in ctx.events if e.type == "turn_profile"]


def test_fara_familii_declarate_nu_se_promite_o_secventa(_routine_on):
    """Tenantul n-a declarat pași ⇒ `routine_plan` n-ar avea ce umple.

    `turn_profile.select` cade pe `recommend`, iar cu `TURN_PROFILES_ENABLED` stins acela nu se
    aplică. Un sufix care trimite modelul la o unealtă fără date ar fi mai rău decât niciun sufix:
    modelul ar umple golul singur."""
    ctx = _ctx("vreau o rutina completa pentru ten", with_families=False)
    system, tools = _apply_turn_profile(ctx, SYSTEM, [])

    assert system == SYSTEM and tools == []


def test_un_tur_obisnuit_nu_capata_sufixul_de_rutina(_routine_on):
    """Doar turele care CER o secvență sunt atinse — restul traficului rămâne neschimbat.

    Asta e diferența dintre `ROUTINE_ENABLED` și `TURN_PROFILES_ENABLED`: al doilea schimbă sufixul
    pentru TOT traficul și se decide pe golden (D15)."""
    ctx = _ctx("ai o crema hidratanta buna?")
    system, tools = _apply_turn_profile(ctx, SYSTEM, [])

    assert system == SYSTEM and tools == []


def test_unealta_deja_prezenta_nu_se_dubleaza(_routine_on):
    ctx = _ctx("vreau o rutina completa pentru ten")
    existing = [{"type": "function", "function": {"name": "routine_plan"}}]
    _, tools = _apply_turn_profile(ctx, SYSTEM, existing)

    assert [t["function"]["name"] for t in tools] == ["routine_plan"]
