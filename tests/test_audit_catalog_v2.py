"""NX-168d — Product Contract v3: audit versionat (R7-R13) + findings machine-readable
`{message, product_slugs}` + severitate violations/warnings. Pur, fără DB.

Regulile v2 (R1-R6) sunt testate în test_catalog_audit.py; aici testăm DOAR v3."""

import json
from pathlib import Path

from scripts.audit_catalog_v2 import _gtin_valid, audit
from scripts.seed_catalog_v2 import gate_violations

ROOT = Path(__file__).resolve().parents[1]
LEGACY = ROOT / "db" / "seed" / "catalog.json"


def _cats():
    return [
        {"slug": "machiaj", "name": "Machiaj"},
        {"slug": "fond-de-ten", "name": "Fond de ten", "parentSlug": "machiaj"},
        {
            "slug": "pensule-si-bureti-de-machiaj",
            "name": "Pensule și bureți de machiaj",
            "parentSlug": "machiaj",
        },
        {"slug": "ingrijirea-tenului", "name": "Îngrijirea tenului"},
        {
            "slug": "seruri-pentru-ten",
            "name": "Seruri pentru ten",
            "parentSlug": "ingrijirea-tenului",
        },
    ]


def _fond(slug="fond-ok"):
    """Fond de ten contract-complet v3 (trece TOATE R1-R13)."""
    return {
        "slug": slug,
        "name": "Velora Perfect Fond de ten",
        "brandSlug": "velora",
        "primaryCategorySlug": "fond-de-ten",
        "price": 59.9,
        "ai_summary": "Fond matifiant pentru ten gras, cu ținută lungă.",
        "attributes": {
            "finish": "matte",
            "coverage": "full",
            "suitable_for": ["oily"],
            "texture": "fluid",
            "best_for": "ten gras care vrea matifiere",
            "concerns": ["oily"],
        },
        "variants": [{"label": "Bej 01", "sku": f"{slug}-01", "price": 59.9, "stock": 10}],
    }


def _data(*products):
    return {"categories": _cats(), "products": list(products)}


def _viol(data):
    v = audit(data, contract="v3")["violations"]
    return {k: val for k, val in v.items() if val}


def _n_viol(data):
    return sum(len(v) for v in audit(data, contract="v3")["violations"].values())


# --- Happy: contract complet trece -------------------------------------------------------------


def test_v3_complete_fond_passes():
    assert _n_viol(_data(_fond())) == 0


def test_v3_accessory_without_ingredients_passes():
    # Edge: accesoriu (pensule) NU cere ingrediente/concerns; cere key_benefit+differentiators.
    tool = {
        "slug": "set-pensule",
        "name": "Aria Set Pensule de machiaj",
        "brandSlug": "aria",
        "primaryCategorySlug": "pensule-si-bureti-de-machiaj",
        "price": 89.9,
        "attributes": {
            "best_for": "începători care vor un set complet",
            "key_benefit": "cinci pensule esențiale",
            "differentiators": ["set 5 piese", "peri sintetici moi"],
        },
        "variants": [{"label": "Set", "sku": "APM-5", "price": 89.9, "stock": 5}],
    }
    assert _n_viol(_data(tool)) == 0


# --- Failure (violation) per regulă ------------------------------------------------------------


def test_r12_ai_summary_ingredient_unfounded():
    p = _fond()
    p["ai_summary"] = "Fond cu retinol pentru ten gras."  # retinol NU e în key_ingredients
    assert _viol(_data(p)).get("ai_summary_unfounded")


def test_r8_contraindication_hard_without_source():
    p = _fond()
    p["attributes"]["not_recommended_for"] = [
        {"value": "sensitive", "level": "hard", "reason": "acid"}
    ]
    assert _viol(_data(p)).get("claim_provenance")


def test_r10_foundation_missing_finish():
    p = _fond()
    del p["attributes"]["finish"]
    assert _viol(_data(p)).get("required_attrs_v3")


def test_r11_missing_best_for():
    p = _fond()
    del p["attributes"]["best_for"]
    assert _viol(_data(p)).get("missing_best_for")


def test_r7_positive_finish_contradiction():
    p = _fond()
    p["attributes"]["finish"] = "dewy"  # dar ai_summary spune „matifiant" (pozitiv) → contradicție
    assert _viol(_data(p)).get("desc_attr_contradiction")


def test_r9_invalid_gtin_checksum():
    p = _fond()
    p["variants"][0]["gtin"] = "1111111111111"  # checksum GS1 invalid
    assert _viol(_data(p)).get("sku_gtin")


def test_r13_variant_without_price():
    p = _fond()
    p["variants"][0].pop("price")
    assert _viol(_data(p)).get("variants_incomplete")


def test_r8_key_ingredient_without_provenance():
    p = _fond()
    p["attributes"]["key_ingredients"] = ["niacinamidă"]  # fără claim_provenance
    assert _viol(_data(p)).get("claim_provenance")


def test_r8_badge_without_provenance():
    p = _fond()
    p["attributes"]["badges"] = ["vegan"]  # fără claim_provenance
    assert _viol(_data(p)).get("claim_provenance")


# --- Happy R8: cu proveniență corespunzătoare → trece ------------------------------------------


def test_r8_ingredient_and_badge_with_provenance_pass():
    p = _fond()
    p["attributes"]["key_ingredients"] = ["niacinamidă"]
    p["attributes"]["badges"] = ["vegan"]
    p["attributes"]["claim_provenance"] = [
        {
            "kind": "ingredient",
            "value": "niacinamidă",
            "source": "INCI",
            "source_ref": "eticheta",
            "verified_at": "2026-07-16",
        },
        {
            "kind": "badge",
            "value": "vegan",
            "source": "producător",
            "source_ref": "fișă",
            "verified_at": "2026-07-16",
        },
    ]
    assert _n_viol(_data(p)) == 0


# --- Machine-readable: duplicate marchează TOATE slug-urile ------------------------------------


def test_duplicate_sku_marks_all_slugs():
    a, b = _fond("prod-a"), _fond("prod-b")
    b["variants"][0]["sku"] = a["variants"][0]["sku"]  # SKU duplicat între 2 produse
    v = _viol(_data(a, b)).get("sku_gtin")
    assert v
    assert set(v[0]["product_slugs"]) == {"prod-a", "prod-b"}


# --- Warning (non-fatal): negație NU e violation -----------------------------------------------


def test_r7_negation_is_warning_not_violation():
    p = _fond()
    p["attributes"]["finish"] = "dewy"
    p["ai_summary"] = "Fond care NU lasă finish mat, aspect luminos toată ziua."
    res = audit(_data(p), contract="v3")
    assert not res["violations"]["desc_attr_contradiction"]  # negația NU pică
    assert res["warnings"]["desc_attr_contradiction"]  # dar e semnalată ca warning


def test_seed_gate_helper_excludes_warnings():
    # Exercită PORTA REALĂ a seed-ului (gate_violations), nu o formulă duplicată.
    # Catalog cu DOAR warning (R7 negație) → poarta = [] (warnings excluse structural).
    p = _fond()
    p["attributes"]["finish"] = "dewy"
    p["ai_summary"] = "Fond care NU lasă finish mat."
    assert gate_violations(_data(p), contract="v3") == []
    # Catalog cu violation → poarta non-goală.
    bad = _fond()
    del bad["attributes"]["best_for"]
    assert gate_violations(_data(bad), contract="v3")
    # Poarta implicită (v2) pe catalog v2-curat → [] (seed ar porni).
    assert gate_violations(_data(_fond())) == []


# --- R12 extins: finish + concern + negație ----------------------------------------------------


def test_r12_finish_claim_without_attribute():
    # ai_summary afirmă „matifiant" dar produsul n-are `finish` → violation (nesusținut).
    p = _fond()
    del p["attributes"]["finish"]
    p["ai_summary"] = "Fond matifiant cu ținută lungă."
    assert _viol(_data(p)).get("ai_summary_unfounded")


def test_r12_concern_claim_unfounded():
    # „ten sensibil" în ai_summary dar `sensitive` absent din concerns/suitable_for → violation.
    p = _fond()
    p["ai_summary"] = "Fond ideal pentru ten sensibil."  # _fond are doar oily
    assert _viol(_data(p)).get("ai_summary_unfounded")


def test_r12_negation_is_warning():
    # „fără retinol" (negație) → warning, NU violation.
    p = _fond()
    p["ai_summary"] = "Fond fără retinol, potrivit pentru ten gras."
    res = audit(_data(p), contract="v3")
    assert not res["violations"]["ai_summary_unfounded"]
    assert res["warnings"]["ai_summary_unfounded"]


# --- Schema v3: validare structurală per-categorie (exercită jsonschema în CI) -----------------


def test_v3_schema_validates_per_category():
    import jsonschema  # dep în requirements — forțează CI să exercite catalog_v3.schema.json

    schema = json.loads(
        (ROOT / "db" / "seed" / "catalog_v3.schema.json").read_text(encoding="utf-8")
    )
    validator = jsonschema.Draft202012Validator(schema)
    base = {
        "brands": [{"slug": "velora", "name": "Velora"}],
        "categories": [{"slug": "fond-de-ten", "name": "Fond"}],
    }
    assert not list(validator.iter_errors({**base, "products": [_fond()]}))  # complet → valid
    bad = _fond()
    del bad["attributes"]["finish"]
    assert list(validator.iter_errors({**base, "products": [bad]}))  # fond fără finish → invalid


# --- GS1 checksum ------------------------------------------------------------------------------


def test_gtin_checksum_helper():
    assert _gtin_valid("4006381333931")  # EAN-13 valid cunoscut
    assert not _gtin_valid("4006381333932")  # cifra de control greșită
    assert not _gtin_valid("123")  # lungime invalidă
    # malformate: NU se curăță non-cifrele (altfel ar trece cu checksum corect)
    assert not _gtin_valid("4006-3813-3393-1")  # cratime
    assert not _gtin_valid("EAN4006381333931")  # prefix literă
    assert not _gtin_valid("4006 3813 3393 1")  # spații


# --- Legacy catalog pică v3 --------------------------------------------------------------------


def test_legacy_catalog_fails_v3():
    if not LEGACY.exists():
        return  # legacy poate lipsi în unele checkout-uri
    raw = json.loads(LEGACY.read_text(encoding="utf-8"))
    data = raw if isinstance(raw, dict) else {"products": raw}
    if not data.get("products"):
        return
    assert _n_viol(data) > 0  # catalogul templatat NU respectă contractul v3
