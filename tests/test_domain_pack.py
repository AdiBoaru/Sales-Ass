"""NX-114 — DomainPack loader: merge default-JSON-per-vertical + settings override,
normalizare, fallback de vertical, locale-keyed risk/greetings, kill-switch.

Pur (fără DB/LLM): construiește BusinessConfig în memorie și verifică pack-ul rezultat."""

from types import SimpleNamespace

from src.domain.loader import load_domain_pack
from src.models import BusinessConfig
from src.tools.taxonomy import _BEAUTY


def _biz(vertical="beauty_salon", settings=None):
    return BusinessConfig(id="b", slug="s", name="n", vertical=vertical, settings=settings or {})


# --- happy ------------------------------------------------------------------


def test_beauty_salon_defaults():
    pack = load_domain_pack(_biz("beauty_salon"))
    assert pack is not None
    assert pack.concern_map["ten gras"] == "oily"
    assert "skin_type" in pack.profile_whitelist
    assert pack.currency == "RON"


def test_beauty_alias_maps_to_beauty_salon():
    # verticalul live „beauty" → fișierul canonic beauty_salon.json (alias).
    pack = load_domain_pack(_biz("beauty"))
    assert pack.concern_map["ten gras"] == "oily"


def test_concern_map_byte_equivalent_to_hardcoded():
    # beauty_salon.json normalizat == _BEAUTY hardcodat (taxonomy.py) — zero regresie.
    pack = load_domain_pack(_biz("beauty_salon"))
    assert pack.concern_map == _BEAUTY


def test_settings_override_merges_over_defaults():
    pack = load_domain_pack(
        _biz("beauty_salon", {"domain_pack": {"concern_map": {"par vopsit": "colored_hair"}}})
    )
    assert pack.concern_map["par vopsit"] == "colored_hair"  # cheia nouă
    assert pack.concern_map["ten gras"] == "oily"  # default păstrat


def test_offer_currency_from_settings():
    pack = load_domain_pack(_biz("beauty_salon", {"currency": "EUR"}))
    assert pack.currency == "EUR"


# --- Tier 2: comparison_facets (fațete de domeniu, generic) -------------------


def test_beauty_salon_comparison_facets_parsed():
    pack = load_domain_pack(_biz("beauty_salon"))
    # ordinea = ordinea de afișare a rândurilor (populate azi: key_benefit + concerns 500/500;
    # key_ingredients derivat din INCI de scripts/enrich_key_ingredients.py).
    # NX-169: + fațetele v3 (suitable_for/finish/coverage/texture) pt proiecția în view-uri.
    assert [f.key for f in pack.comparison_facets] == [
        "key_benefit",
        "key_ingredients",
        "concerns",
        "suitable_for",
        "finish",
        "coverage",
        "texture",
    ]
    kb = next(f for f in pack.comparison_facets if f.key == "key_benefit")
    assert kb.labels["ro"] == "Beneficiu principal"
    concerns = next(f for f in pack.comparison_facets if f.key == "concerns")
    assert concerns.labels["ro"] == "Potrivit pentru" and concerns.labels["en"] == "Suitable for"
    # DB stochează CANONICAL (aliniat cu map_concerns → filtrul prinde); afișarea re-mapează la RO.
    assert concerns.value_labels["dry"]["ro"] == "ten uscat"


def test_comparison_facets_override_replaces_and_skips_garbage():
    pack = load_domain_pack(
        _biz(
            "beauty_salon",
            {
                "domain_pack": {
                    "comparison_facets": [
                        {"key": "finish", "labels": {"ro": "Finisaj"}},
                        {"no_key": "x"},  # fără `key` → sărit (fail-safe)
                        "nu e dict",  # ne-dict → sărit
                    ]
                }
            },
        )
    )
    # override pe o LISTĂ înlocuiește (semantica deep-merge); doar intrarea validă rămâne
    assert [f.key for f in pack.comparison_facets] == ["finish"]


def test_ecommerce_default_has_no_facets():
    pack = load_domain_pack(_biz("ecommerce"))
    assert pack.comparison_facets == ()  # default fără fațete → tabel generic (ca azi)


def test_beauty_salon_searchable_facets():
    # Tier 2b p2: search-ul poate filtra pe key_ingredients („ceva cu niacinamidă").
    pack = load_domain_pack(_biz("beauty_salon"))
    assert pack.searchable_facets == ("key_ingredients",)


def test_ecommerce_no_searchable_facets():
    pack = load_domain_pack(_biz("ecommerce"))
    assert pack.searchable_facets == ()  # fără filtru de feature (default)


# --- locale-keyed (P11) -----------------------------------------------------


def test_risk_terms_keyed_on_locale():
    pack = load_domain_pack(_biz("beauty_salon"))
    assert "om real" in pack.risk_terms["ro"]["human_request"]
    assert pack.risk_terms.get("hu") is None  # locale absent → fără KeyError


def test_greetings_override_normalized_and_locale_keyed():
    pack = load_domain_pack(_biz("beauty_salon", {"domain_pack": {"greetings": {"ro": ["Bunăă"]}}}))
    assert pack.greetings["ro"] == ["bunaa"]  # normalizat (lower + fără diacritice)


# --- normalizare ------------------------------------------------------------


def test_concern_keys_normalized():
    pack = load_domain_pack(
        _biz("beauty_salon", {"domain_pack": {"concern_map": {"Ten Grăs": "oily"}}})
    )
    assert pack.concern_map["ten gras"] == "oily"  # diacritice + uppercase colapsate


# --- fallback + defensive (P6) ----------------------------------------------


def test_vertical_other_fallback():
    pack = load_domain_pack(_biz("other"))
    assert pack.concern_map == {}
    assert pack.profile_whitelist == frozenset({"budget_band", "fav_brands", "concerns"})


def test_unknown_vertical_falls_back_to_other():
    pack = load_domain_pack(_biz("hvac"))  # fără fișier dedicat → other.json
    assert pack is not None
    assert pack.concern_map == {}


def test_malformed_override_ignored():
    pack = load_domain_pack(_biz("beauty_salon", {"domain_pack": "nu e dict"}))
    assert pack.concern_map["ten gras"] == "oily"  # cade pe default, fără crash


def test_auto_service_profile_whitelist():
    pack = load_domain_pack(_biz("auto"))  # alias → auto_service.json
    assert "vehicle_make" in pack.profile_whitelist


# --- kill-switch ------------------------------------------------------------


def test_kill_switch_off_returns_none(monkeypatch):
    monkeypatch.setattr(
        "src.domain.loader.get_settings", lambda: SimpleNamespace(domain_pack_enabled=False)
    )
    assert load_domain_pack(_biz("beauty_salon")) is None
