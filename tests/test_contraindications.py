"""NX-173 (P0) — stratul de DECIZIE: registru (validare strictă, fail-closed), detecție de context,
gate pe produs. Pur, fără DB/LLM. Contractul de compunere e în `test_safety_compose.py`; matricea
de căi în `test_contraindications_e2e.py`.
"""

import json

import pytest

from src.safety.contraindications import (
    RegistryError,
    _parse,
    check_product,
    detect_contexts,
    detect_contexts_in_turn,
    filter_products,
    has_verifiable_ingredients,
    load_registry,
    registry_healthy,
)

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
UNSAFE_NAME = {  # retinoid DOAR în nume (cazul celor 178 produse fără key_ingredients)
    "id": "unsafe-2",
    "name": "Auralis Retinol Ser de noapte",
    "price": 119.0,
    "attributes": {"concerns": ["anti_aging"]},
}
UNKNOWN = {"id": "unknown-1", "name": "Nova Botanics Ser", "price": 99.0, "attributes": {}}

PREG = frozenset({"pregnancy"})


def _raw(**over):
    """Registru minimal VALID → mutabil per test (proba de validare)."""
    raw = {
        "contexts": [{"id": "pregnancy", "context_patterns": ["insarcinat"]}],
        "rules": [
            {
                "id": "r1",
                "contexts": ["pregnancy"],
                "level": "hard",
                "match_ingredient_prefixes": ["retinol"],
                "source": "editorial_policy",
                "source_ref": "ref",
                "verified_at": "2026-07-17",
                "reviewed_by": "adi-boaru",
            }
        ],
    }
    raw.update(over)
    return raw


# --- registru: validare STRICTĂ + fail-closed --------------------------------------------------


def test_registry_loads_and_is_healthy():
    ok, info = registry_healthy()
    assert ok, info
    reg = load_registry()
    assert reg.rules and reg.contexts


def test_every_rule_has_full_provenance_and_human_review():
    """Contract: nicio regulă fără sursă verificabilă + aprobare umană reală."""
    for r in load_registry().rules:
        assert r.source and r.source_ref and r.verified_at, f"{r.id}: provenance incomplet"
        assert r.reviewed_by and "pending" not in r.reviewed_by.lower(), f"{r.id}: nerevizuit"


def test_unreviewed_rule_is_rejected():
    """`PENDING_HUMAN_REVIEW` NU se mai aplică tăcut (era ignorat de runtime — review Codex)."""
    raw = _raw()
    raw["rules"][0]["reviewed_by"] = "PENDING_HUMAN_REVIEW"
    with pytest.raises(RegistryError, match="NEREVIZUIT"):
        _parse(raw)


@pytest.mark.parametrize("field", ["source", "source_ref", "verified_at"])
def test_rule_without_provenance_is_rejected(field):
    raw = _raw()
    raw["rules"][0][field] = ""
    with pytest.raises(RegistryError, match=field):
        _parse(raw)


def test_rule_with_unknown_context_is_rejected():
    raw = _raw()
    raw["rules"][0]["contexts"] = ["nonexistent"]
    with pytest.raises(RegistryError, match="inexistente"):
        _parse(raw)


def test_duplicate_rule_id_is_rejected():
    raw = _raw()
    raw["rules"].append(dict(raw["rules"][0]))
    with pytest.raises(RegistryError, match="duplicat"):
        _parse(raw)


def test_invalid_level_is_rejected():
    raw = _raw()
    raw["rules"][0]["level"] = "maybe"
    with pytest.raises(RegistryError, match="level invalid"):
        _parse(raw)


def test_rule_without_matchers_is_rejected():
    raw = _raw()
    raw["rules"][0]["match_ingredient_prefixes"] = []
    with pytest.raises(RegistryError, match="fără matcheri"):
        _parse(raw)


def test_empty_registry_is_rejected():
    with pytest.raises(RegistryError):
        _parse({"contexts": [], "rules": []})


def test_corrupt_registry_file_raises(monkeypatch, tmp_path):
    """Fișier corupt → RegistryError (NU registru gol tăcut = protecție dispărută)."""
    bad = tmp_path / "bad.json"
    bad.write_text("{ nu e json", encoding="utf-8")
    monkeypatch.setattr("src.safety.contraindications._RULES_PATH", bad)
    load_registry.cache_clear()
    try:
        with pytest.raises(RegistryError, match="JSON invalid"):
            load_registry()
        ok, why = registry_healthy()
        assert ok is False and "JSON invalid" in why
    finally:
        load_registry.cache_clear()


def test_missing_registry_file_raises(monkeypatch, tmp_path):
    monkeypatch.setattr("src.safety.contraindications._RULES_PATH", tmp_path / "nope.json")
    load_registry.cache_clear()
    try:
        with pytest.raises(RegistryError, match="necitibil"):
            load_registry()
    finally:
        load_registry.cache_clear()


def test_shipped_registry_carries_no_client_copy():
    """Registrul poartă DECIZIA, nu limba: copy-ul e pe chei în messages.py (review Codex)."""
    raw = json.loads(
        (__import__("pathlib").Path("db/seed/safety_rules.json")).read_text(encoding="utf-8")
    )
    for r in raw["rules"]:
        assert not any(k.endswith("_ro") for k in r), f"{r['id']}: text RO în registru"
    for c in raw["contexts"]:
        assert not any(k.endswith("_ro") for k in c), f"{c['id']}: text RO în registru"


# --- detecție de context -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "sunt însărcinată, ce cremă antirid pot folosi?",
        "sunt insarcinata in 3 luni",  # fără diacritice
        "sunt gravidă, ce recomanzi pentru riduri?",
        "ce ser antirid pot folosi în sarcină?",
        "SUNT ÎNSĂRCINATĂ",
    ],
)
def test_detect_pregnancy(text):
    assert "pregnancy" in detect_contexts(text)


def test_detect_breastfeeding():
    assert "breastfeeding" in detect_contexts("alăptez, ce pot folosi?")


@pytest.mark.parametrize("text", ["vreau un ser cu niacinamidă", "am tenul gras", "", None])
def test_detect_no_context(text):
    assert detect_contexts(text) == frozenset()


# --- gate pe produs ----------------------------------------------------------------------------


def test_safe_product_passes():
    assert check_product(SAFE, PREG) is None


def test_bakuchiol_does_not_match_retinol_prefix():
    """Graniță de cuvânt: `retinol` NU se potrivește în interiorul altui cuvânt."""
    assert check_product(SAFE, PREG) is None


def test_unsafe_by_ingredient_blocked():
    b = check_product(UNSAFE_INGREDIENT, PREG)
    assert b is not None and b.rule_id == "pregnancy-retinoids" and b.matched == "retinal"


def test_unsafe_by_name_blocked_when_ingredients_missing():
    b = check_product(UNSAFE_NAME, PREG)
    assert b is not None and b.matched == "retinol"


def test_block_carries_refs_not_text():
    """`Block` poartă rule_id/context_id (chei), nu copy — limba stă în messages.py."""
    b = check_product(UNSAFE_INGREDIENT, PREG)
    assert not any(f.endswith("_ro") for f in b.__dataclass_fields__)


def test_no_context_no_block():
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
                }
            ]
        },
    }
    b = check_product(p, PREG)
    assert b is not None and b.rule_id == "not_recommended_for"


def test_declared_soft_does_not_hard_block():
    p = {
        "id": "soft-1",
        "name": "Produs oarecare",
        "attributes": {"not_recommended_for": [{"value": "pregnancy", "level": "soft"}]},
    }
    assert check_product(p, PREG) is None


def test_filter_keeps_order_and_reports_blocked():
    kept, blocked = filter_products([SAFE, UNSAFE_INGREDIENT, UNKNOWN, UNSAFE_NAME], PREG)
    assert [p["id"] for p in kept] == ["safe-1", "unknown-1"]
    assert sorted(b.product_id for b in blocked) == ["unsafe-1", "unsafe-2"]


def test_unknown_ingredients_are_not_verifiable():
    assert has_verifiable_ingredients(UNKNOWN) is False
    assert has_verifiable_ingredients(SAFE) is True


# --- context pe tur (mesaj + istoric) ----------------------------------------------------------


class _Msg:
    def __init__(self, body, direction="inbound"):
        self.body, self.direction = body, direction


class _Ctx:
    def __init__(self, body, history=None):
        self.message = _Msg(body)
        self.history = history or []


def test_contexts_from_current_message():
    assert detect_contexts_in_turn(_Ctx("sunt însărcinată")) == PREG


def test_contexts_from_history_multi_turn():
    ctx = _Ctx(
        "arată-mi un ser antirid",
        [_Msg("sunt însărcinată"), _Msg("Sigur, ce cauți?", direction="outbound")],
    )
    assert detect_contexts_in_turn(ctx) == PREG


def test_bot_message_does_not_declare_context():
    ctx = _Ctx("arată-mi un ser", [_Msg("produse pentru sarcină nu recomand", "outbound")])
    assert detect_contexts_in_turn(ctx) == frozenset()
