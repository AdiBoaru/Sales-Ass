"""NX-173 — `merge_entries` (nucleul pur al backfill-ului): idempotență + nedistructivitate.

Contează înainte de a rula pe LIVE: re-rularea nu are voie să dubleze intrări, iar intrările curate
de om/furnizor nu au voie să dispară.
"""

from scripts.backfill_safety_flags import _planned_entries, merge_entries

HUMAN = {  # intrare scrisă de om/furnizor — fără `rule_id` → nu ne aparține
    "value": "sensitive",
    "level": "soft",
    "reason": "acid",
    "source": "manufacturer_label",
    "verified_at": "2026-01-01",
}
GEN = {
    "value": "pregnancy",
    "level": "hard",
    "reason": "retinoizi",
    "source": "editorial_policy",
    "source_ref": "ref",
    "verified_at": "2026-07-17",
    "rule_id": "pregnancy-retinoids",
    "matched_on": "retinol",
}


def test_planned_entries_for_retinoid_product():
    p = {"id": "1", "name": "Auralis Retinol Ser", "attributes": {}}
    planned = _planned_entries(p)
    assert {e["value"] for e in planned} == {"pregnancy", "breastfeeding"}
    for e in planned:
        assert e["level"] == "hard"
        assert e["source"] and e["source_ref"] and e["verified_at"]  # provenance INLINE completă
        assert e["rule_id"] == "pregnancy-retinoids"


def test_planned_entries_empty_for_safe_product():
    p = {"id": "1", "name": "Ser Bakuchiol", "attributes": {"key_ingredients": ["bakuchiol"]}}
    assert _planned_entries(p) == []


def test_merge_is_idempotent():
    """A doua rulare pe aceleași date → nicio schimbare (nu dublăm intrări)."""
    merged, changed = merge_entries(None, [GEN])
    assert changed is True and merged == [GEN]
    merged2, changed2 = merge_entries(merged, [GEN])
    assert changed2 is False and merged2 == [GEN]


def test_merge_preserves_human_entries():
    """Intrările care nu-s ale noastre rămân neatinse, în ordine."""
    merged, changed = merge_entries([HUMAN], [GEN])
    assert changed is True
    assert merged[0] == HUMAN and merged[1] == GEN


def test_merge_updates_our_entry_in_place_on_registry_change():
    """Registru schimbat (ex. `verified_at` nou) → intrarea NOASTRĂ se actualizează, nu se
    dublează."""
    updated = {**GEN, "verified_at": "2027-01-01"}
    merged, changed = merge_entries([HUMAN, GEN], [updated])
    assert changed is True
    assert merged == [HUMAN, updated]
    assert len([e for e in merged if e.get("rule_id")]) == 1


def test_merge_removes_our_stale_entry_when_rule_no_longer_matches():
    """Regula nu mai se potrivește (ex. ingredient corectat) → intrarea noastră dispare; a omului
    rămâne."""
    merged, changed = merge_entries([HUMAN, GEN], [])
    assert changed is True and merged == [HUMAN]
