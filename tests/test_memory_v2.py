"""NX-160 — teste PURE pentru memoria generică v2 (canonicalizer + safety gate).

Zero DB / zero LLM. Acoperă direcția `capture broad → classify safety → canonicalize`:
- canonicalizer generic pe 4+ tipuri de business (beauty/auto/restaurant/service);
- whitelist devine ȚINTĂ de canonicalizare, nu poartă fail-closed;
- safety gate: PII/financial → drop; medical → candidate; preferință comercială → inject.
"""

from __future__ import annotations

from src.domain.pack import DomainPack
from src.models import Author, Direction, Message
from src.worker.canonicalize import (
    UNIVERSAL_CANONICAL,
    canonical_keys_for,
    memory_key,
    resolve_canonical,
)
from src.worker.memory import process_facts
from src.worker.memory_safety import classify
from src.worker.profile import build_ref_map


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


# --- process_facts (orchestrare capture → classify → canonicalize) -------------------------


def test_process_facts_beauty_end_to_end():
    pack = _pack(vertical="beauty", fact_type_whitelist=frozenset({"skin_type"}))
    candidates = [
        {"raw_key": "preferred_brand", "fact_value": "CeraVe", "confidence": 0.86},
        {"raw_key": "budget_max_lei", "fact_value": "sub 100 lei", "confidence": 0.9},
        {"raw_key": "fragrance_free_preference", "fact_value": "fără parfum", "confidence": 0.8},
        {"raw_key": "skin_type", "fact_value": "sensibil", "confidence": 0.95},
    ]
    proc = process_facts(candidates, pack, source_message_id="msg-1")
    # NX-160 vs NX-148: toate 4 supraviețuiesc (whitelist-ul vechi arunca 3 din 4).
    assert proc.injectable == 4 and proc.dropped == 0
    canon = {r["canonical_key"] for r in proc.rows}
    assert {"fav_brands", "budget_band", "restriction", "skin_type"} <= canon
    assert all(r["source_message_id"] == "msg-1" for r in proc.rows)
    assert all(r["visibility"] == "inject" for r in proc.rows)


def test_process_facts_drops_pii_keeps_rest():
    proc = process_facts(
        [
            {"raw_key": "phone", "fact_value": "0722123456", "confidence": 0.9},
            {"raw_key": "budget", "fact_value": "100 lei", "confidence": 0.8},
        ],
        _pack(),
    )
    assert proc.dropped == 1 and proc.injectable == 1
    assert all(r["safety_class"] != "pii" for r in proc.rows)  # PII nu se persistă deloc


def test_process_facts_medical_is_candidate_not_injected():
    proc = process_facts(
        [{"raw_key": "health_condition", "fact_value": "diabetic", "confidence": 0.9}], _pack()
    )
    assert proc.candidate == 1 and proc.injectable == 0
    assert proc.rows[0]["visibility"] == "candidate"  # stocat ca semnal, nu injectat


def test_process_facts_dedupe_by_memory_key():
    # două raw_key sinonime → același memory_key → un singur rând (confidence maxim).
    proc = process_facts(
        [
            {"raw_key": "preferred_brand", "fact_value": "CeraVe", "confidence": 0.7},
            {"raw_key": "brand_preference", "fact_value": "CeraVe", "confidence": 0.9},
        ],
        _pack(),
    )
    assert len(proc.rows) == 1 and proc.rows[0]["confidence"] == 0.9


def test_process_facts_backcompat_fact_type():
    # un model care încă emite `fact_type` (nu `raw_key`) e tolerat.
    proc = process_facts([{"fact_type": "budget", "fact_value": "150 lei"}], _pack())
    assert proc.injectable == 1 and proc.rows[0]["canonical_key"] == "budget_band"


def test_canonicalize_flag_off_keeps_raw():
    # fix review Codex #201: MEMORY_CANONICALIZE_ENABLED=false → canonical_key rămâne None REAL.
    cands = [{"raw_key": "preferred_brand", "fact_value": "CeraVe", "confidence": 0.9}]
    proc = process_facts(cands, _pack(), canonicalize=False)
    assert proc.canonicalized == 0
    assert proc.rows[0]["canonical_key"] is None
    assert proc.rows[0]["memory_key"] == "raw:preferred_brand"  # nu 'canonical:fav_brands'


def test_source_ref_maps_to_real_message_id():
    # fix review Codex #201: source_ref → id-ul mesajului-sursă, nu al turului.
    ref_map = {"m1": "id-vechi", "m3": "id-sursa"}
    proc = process_facts(
        [{"raw_key": "budget", "fact_value": "100 lei", "confidence": 0.9, "source_ref": "m3"}],
        _pack(),
        source_message_id="id-tur-curent",
        ref_map=ref_map,
    )
    assert proc.rows[0]["source_message_id"] == "id-sursa"


def test_source_ref_fallback_to_turn_message():
    # fără source_ref (sau nemapabil) → fallback la mesajul turului (nenul garantat).
    proc = process_facts(
        [{"raw_key": "budget", "fact_value": "100 lei", "confidence": 0.9}],
        _pack(),
        source_message_id="id-tur-curent",
        ref_map={"m1": "x"},
    )
    assert proc.rows[0]["source_message_id"] == "id-tur-curent"


def test_build_ref_map_numbers_only_nonempty_with_id():
    hist = [
        Message(direction=Direction.INBOUND, author=Author.CONTACT, body="caut cremă", id="id-1"),
        Message(direction=Direction.OUTBOUND, author=Author.BOT, body="", id="id-2"),  # gol → sărit
        Message(direction=Direction.INBOUND, author=Author.CONTACT, body="buget 100", id="id-3"),
        Message(direction=Direction.INBOUND, author=Author.CONTACT, body="fără id", id=None),
    ]
    ref_map = build_ref_map(hist)
    # gol sărit → „buget 100" e m2, nu m3; mesajul fără id nu apare deloc.
    assert ref_map == {"m1": "id-1", "m2": "id-3"}
