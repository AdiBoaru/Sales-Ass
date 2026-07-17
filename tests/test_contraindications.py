"""NX-173 (P0) — gate determinist de contraindicații: detecție de context, excludere, degradare
sigură pe date incomplete + testul ADVERSARIAL prin `search_products_tool` (assert pe ID-uri
surfaced, nu pe cuvinte în reply)."""

from dataclasses import dataclass, field
from typing import Any

import pytest

from src.safety.contraindications import (
    check_product,
    contexts_for_turn,
    detect_contexts,
    filter_products,
    has_verifiable_ingredients,
    load_registry,
    safety_note,
)

# --- fixture de produs -------------------------------------------------------------------------

SAFE = {
    "id": "safe-1",
    "name": "Ser Bakuchiol Gentle",
    "price": 84.0,
    "attributes": {"key_ingredients": ["bakuchiol", "squalan"], "concerns": ["anti_aging"]},
}
UNSAFE_INGREDIENT = {  # retinoid DOAR în key_ingredients (numele nu-l trădează)
    "id": "unsafe-1",
    "name": "LumaDerm Renew Ser",
    "price": 149.0,
    "attributes": {"key_ingredients": ["retinal", "squalan"], "concerns": ["anti_aging"]},
}
UNSAFE_NAME = {  # retinoid DOAR în nume (fără key_ingredients — cazul celor 178 produse fără câmp)
    "id": "unsafe-2",
    "name": "Auralis Retinol Ser de noapte",
    "price": 119.0,
    "attributes": {"concerns": ["anti_aging"]},
}
UNKNOWN = {  # fără NICIO informație de ingrediente → nu-l putem judeca
    "id": "unknown-1",
    "name": "Nova Botanics Ser",
    "price": 99.0,
    "attributes": {"concerns": ["anti_aging"]},
}

PREG = frozenset({"pregnancy"})


# --- registru ----------------------------------------------------------------------------------


def test_registry_loads_with_provenance():
    """Fiecare regulă e DATĂ CURATĂ: sursă + referință + dată de verificare (nu inferență)."""
    reg = load_registry()
    assert reg.rules, "registrul de siguranță e gol"
    for r in reg.rules:
        assert r.source and r.source_ref and r.verified_at, f"regula {r.id} fără provenance"
        assert r.contexts, f"regula {r.id} fără context"


# --- detecție de context -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "sunt însărcinată, ce cremă antirid pot folosi?",
        "sunt insarcinata in 3 luni",  # fără diacritice
        "sunt gravidă, ce recomanzi pentru riduri?",
        "ce ser antirid pot folosi în sarcină?",
        "SUNT ÎNSĂRCINATĂ",  # case-insensitive
    ],
)
def test_detect_pregnancy(text):
    assert "pregnancy" in detect_contexts(text)


def test_detect_breastfeeding():
    assert "breastfeeding" in detect_contexts("alăptez, ce pot folosi?")


@pytest.mark.parametrize("text", ["vreau un ser cu niacinamidă", "am tenul gras", "", None])
def test_detect_no_context(text):
    assert detect_contexts(text) == frozenset()


def test_bakuchiol_does_not_trigger_retinol_rule():
    """Graniță de cuvânt: `retinol` NU se potrivește în interiorul altui cuvânt."""
    assert check_product(SAFE, PREG) is None


# --- gate --------------------------------------------------------------------------------------


def test_safe_product_passes():
    assert check_product(SAFE, PREG) is None


def test_unsafe_by_ingredient_blocked():
    b = check_product(UNSAFE_INGREDIENT, PREG)
    assert b is not None
    assert b.rule_id == "pregnancy-retinoids" and b.matched == "retinal"


def test_unsafe_by_name_blocked_when_ingredients_missing():
    """Degradare sigură: fără `key_ingredients`, numele rămâne semnal real."""
    b = check_product(UNSAFE_NAME, PREG)
    assert b is not None and b.matched == "retinol"


def test_no_context_no_block():
    """Fără context declarat NU excludem preventiv — un retinoid e un produs legitim."""
    assert check_product(UNSAFE_INGREDIENT, frozenset()) is None
    kept, blocked = filter_products([SAFE, UNSAFE_INGREDIENT], frozenset())
    assert len(kept) == 2 and not blocked


def test_declared_not_recommended_for_blocks_without_requested_concern():
    """Calea NX-170 (date): `hard` + provenance pe context ACTIV → exclus, chiar dacă `pregnancy`
    nu e un concern CERUT (exact ce `reason_codes.not_recommended_gate` rata)."""
    p = {
        "id": "declared-1",
        "name": "Produs oarecare",
        "attributes": {
            "not_recommended_for": [
                {
                    "value": "pregnancy",
                    "level": "hard",
                    "source": "manufacturer_label",
                    "verified_at": "2026-07-17",
                    "reason": "contraindicat de producător",
                }
            ]
        },
    }
    b = check_product(p, PREG)
    assert b is not None and b.rule_id == "not_recommended_for"


def test_declared_soft_does_not_hard_block():
    """`soft` / neverificat NU exclude dur — nu transformăm o inferență în contraindicație."""
    p = {
        "id": "soft-1",
        "name": "Produs oarecare",
        "attributes": {"not_recommended_for": [{"value": "pregnancy", "level": "soft"}]},
    }
    assert check_product(p, PREG) is None


def test_filter_keeps_order_and_reports_blocked():
    kept, blocked = filter_products([SAFE, UNSAFE_INGREDIENT, UNKNOWN, UNSAFE_NAME], PREG)
    assert [p["id"] for p in kept] == ["safe-1", "unknown-1"]  # ordinea păstrată
    assert sorted(b.product_id for b in blocked) == ["unsafe-1", "unsafe-2"]


def test_filter_all_blocked_returns_empty():
    kept, blocked = filter_products([UNSAFE_INGREDIENT, UNSAFE_NAME], PREG)
    assert kept == [] and len(blocked) == 2


# --- degradare sigură pe date incomplete -------------------------------------------------------


def test_unknown_ingredients_not_declared_safe():
    """Produsul fără ingrediente NU se blochează, dar nota interzice claim-ul de siguranță."""
    assert has_verifiable_ingredients(UNKNOWN) is False
    assert has_verifiable_ingredients(SAFE) is True
    note = safety_note(PREG, [UNKNOWN], [])
    assert note and "NU afirma" in note


def test_safety_note_declines_medical_advice():
    note = safety_note(PREG, [SAFE], [])
    assert note is not None
    assert "sarcină" in note
    assert "nu da sfat medical" in note.lower()
    assert "medicul" in note  # declinare → medic/farmacist, nu sfat inventat


def test_safety_note_none_without_context():
    assert safety_note(frozenset(), [SAFE], []) is None


def test_safety_note_mentions_exclusion_without_naming_product():
    _, blocked = filter_products([UNSAFE_INGREDIENT], PREG)
    note = safety_note(PREG, [], blocked)
    assert note and "EXCLUS" in note
    assert "LumaDerm" not in note  # nu-i dăm modelului numele produsului exclus


# --- context pe tur (mesaj + istoric) ----------------------------------------------------------


@dataclass
class _Msg:
    body: str | None
    direction: str = "inbound"


@dataclass
class _Ctx:
    message: Any
    history: list[Any] = field(default_factory=list)


def test_contexts_from_current_message():
    assert contexts_for_turn(_Ctx(message=_Msg("sunt însărcinată"))) == PREG


def test_contexts_from_history_multi_turn():
    """«sunt însărcinată» (t1) → «arată-mi un ser antirid» (t2): contextul rezistă peste tur."""
    ctx = _Ctx(
        message=_Msg("arată-mi un ser antirid"),
        history=[_Msg("sunt însărcinată"), _Msg("Sigur, ce cauți?", direction="outbound")],
    )
    assert contexts_for_turn(ctx) == PREG


def test_bot_message_does_not_declare_context():
    """Ce a scris BOTUL nu declară nimic despre client (altfel nota noastră se auto-declanșează)."""
    ctx = _Ctx(
        message=_Msg("arată-mi un ser"),
        history=[_Msg("produse pentru sarcină nu recomand", direction="outbound")],
    )
    assert contexts_for_turn(ctx) == frozenset()
