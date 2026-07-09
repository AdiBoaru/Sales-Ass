"""NX-160 — teste PURE pentru memoria generică v2 (canonicalizer + safety gate).

Zero DB / zero LLM. Acoperă direcția `capture broad → classify safety → canonicalize`:
- canonicalizer generic pe 4+ tipuri de business (beauty/auto/restaurant/service);
- whitelist devine ȚINTĂ de canonicalizare, nu poartă fail-closed;
- safety gate: PII/financial → drop; medical → candidate; preferință comercială → inject.
"""

from __future__ import annotations

from src.domain.pack import DomainPack
from src.worker.canonicalize import (
    UNIVERSAL_CANONICAL,
    canonical_keys_for,
    memory_key,
    resolve_canonical,
)
from src.worker.memory_safety import classify


def _pack(**kw) -> DomainPack:
    return DomainPack(vertical=kw.pop("vertical", "other"), **kw)


# --- canonicalizer: nucleu universal + alias-uri -------------------------------------------


def test_universal_alias_brand_and_budget():
    pack = _pack()
    assert resolve_canonical("preferred_brand", pack) == "fav_brands"
    assert resolve_canonical("budget_max_lei", pack) == "budget_band"
    assert resolve_canonical("fragrance_free_preference", pack) == "restriction"


def test_identity_when_already_canonical():
    pack = _pack()
    assert resolve_canonical("budget_band", pack) == "budget_band"
    assert resolve_canonical("Preferred Time", pack) == "preferred_time"  # normalizat


def test_domainpack_keys_are_canonical():
    # beauty: skin_type e în fact_type_whitelist → identitate, chiar dacă nu-i în nucleu.
    pack = _pack(vertical="beauty", fact_type_whitelist=frozenset({"skin_type", "hair_type"}))
    assert resolve_canonical("skin_type", pack) == "skin_type"


def test_unmapped_stays_none():
    # cheie liberă necunoscută → None (rămâne raw candidate, nu ghicim).
    assert resolve_canonical("mood_today", _pack()) is None


def test_auto_vertical_aliases():
    pack = _pack(vertical="auto", fact_type_whitelist=frozenset({"vehicle_model", "part_category"}))
    assert resolve_canonical("vehicle_make_model", pack) == "vehicle_model"
    assert resolve_canonical("part_needed", pack) == "part_category"


def test_canonical_keys_for_includes_core_and_pack():
    pack = _pack(fact_type_whitelist=frozenset({"skin_type"}))
    keys = canonical_keys_for(pack)
    assert "budget_band" in keys and "skin_type" in keys
    assert keys == sorted(set(keys))  # dedupe + sortat (byte-stabil pt prompt)


def test_canonical_keys_for_none_pack():
    keys = canonical_keys_for(None)
    assert set(keys) == set(UNIVERSAL_CANONICAL)


def test_memory_key_canonical_vs_raw():
    assert memory_key("preferred_brand", "fav_brands") == "canonical:fav_brands"
    assert memory_key("mood_today", None) == "raw:mood_today"


# --- safety gate ---------------------------------------------------------------------------


def test_pii_value_dropped():
    v = classify("note", None, "sună-mă la 0722123456")
    assert v.safety_class == "pii" and v.visibility == "drop"


def test_pii_key_dropped():
    v = classify("email", None, "ceva@ceva.ro")
    assert v.visibility == "drop"


def test_financial_dropped():
    v = classify("card", None, "4111 1111 1111 1111")
    assert v.safety_class == "financial" and v.visibility == "drop"


def test_medical_condition_is_candidate_not_inject():
    # „sunt diabetic" NU se injectează — semnal intern.
    v = classify("health_condition", None, "diabetic")
    assert v.safety_class == "health" and v.visibility == "candidate"


def test_medical_term_in_value_under_innocent_key():
    # chiar sub o cheie inocentă, un diagnostic în valoare → candidate.
    v = classify("note", None, "sunt însărcinată")
    assert v.visibility == "candidate"


def test_commercial_preference_from_medical_is_safe():
    # DIFERENȚA critică: „fără zahăr" ca restricție de produs → inject.
    v = classify("restriction", "restriction", "fără zahăr")
    assert v.safety_class == "safe" and v.visibility == "inject"
    v2 = classify("diet_preference", "restriction", "fără gluten")
    assert v2.visibility == "inject"


def test_plain_commercial_facts_inject():
    assert classify("budget_band", "budget_band", "sub 100 lei").visibility == "inject"
    assert classify("fav_brands", "fav_brands", "CeraVe").visibility == "inject"
    assert classify("vehicle_model", "vehicle_model", "Golf 7").visibility == "inject"
