"""NX-168a — audit-gate STATIC al catalogului v2 (contract="v2"). Pur, fără DB: regulile pe
dict-uri crafted + pe fixture-ul sample (trece). Confirmă că gate-ul prinde fiecare clasă de
incoerență și că `main()` întoarce exit 1 pe catalog rupt, 0 pe catalog curat.

NX-168d: `audit()` întoarce acum `{"violations": {...}, "warnings": {...}}` (machine-readable) —
testele accesează `["violations"][rule]`. Regulile R1-R6 (v2) au logică NESCHIMBATĂ."""

import json
import sys
from pathlib import Path

from scripts import audit_catalog_v2 as mod
from scripts.audit_catalog_v2 import audit, build_roots

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "db" / "seed" / "catalog_v2.sample.json"


def _v(data, rule):
    """Violations pt o regulă (contract v2)."""
    return audit(data)["violations"][rule]


def _cat(slug, parent=None):
    c = {"slug": slug, "name": slug.replace("-", " ").title()}
    if parent:
        c["parentSlug"] = parent
    return c


def _cats():
    return [
        _cat("machiaj"),
        _cat("fond-de-ten", "machiaj"),
        _cat("ingrijirea-parului"),
        _cat("sampoane", "ingrijirea-parului"),
    ]


def _fond(slug, **attrs_over):
    attrs = {"finish": "matte", "coverage": "full", "concerns": ["dry"]}
    attrs.update(attrs_over)
    return {
        "slug": slug,
        "name": "X Fond de ten",
        "primaryCategorySlug": "fond-de-ten",
        "attributes": attrs,
    }


# --- sample valid trece ------------------------------------------------------------------------


def test_sample_passes_all_rules():
    data = json.loads(SAMPLE.read_text(encoding="utf-8"))
    res = audit(data)
    leftover = {k: v for k, v in res["violations"].items() if v}
    assert not leftover, leftover


# --- structura rezultatului (NX-168d) ----------------------------------------------------------


def test_result_shape_violations_warnings():
    res = audit({"categories": _cats(), "products": [_fond("p")]})
    assert set(res.keys()) == {"violations", "warnings"}
    # fiecare entry e machine-readable
    data = {"categories": _cats(), "products": [_fond("p", concerns=["ten uscat"])]}
    entry = audit(data)["violations"]["canonical_enums"][0]
    assert set(entry.keys()) == {"message", "product_slugs"}
    assert entry["product_slugs"] == ["p"]


# --- build_roots -------------------------------------------------------------------------------


def test_build_roots_resolves_to_top():
    roots = build_roots(_cats())
    assert roots["fond-de-ten"] == "machiaj"
    assert roots["machiaj"] == "machiaj"
    assert roots["sampoane"] == "ingrijirea-parului"


# --- R1 enum-uri canonice ----------------------------------------------------------------------


def test_r1_flags_non_canonical_concern():
    data = {"categories": _cats(), "products": [_fond("p", concerns=["ten uscat"])]}
    assert _v(data, "canonical_enums")  # „ten uscat" nu e canonic (dry)


def test_r1_flags_bad_finish():
    data = {"categories": _cats(), "products": [_fond("p", finish="glossy")]}
    assert _v(data, "canonical_enums")


def test_r1_clean_on_canonical():
    data = {"categories": _cats(), "products": [_fond("p", concerns=["dry", "hydration"])]}
    assert not _v(data, "canonical_enums")


# --- R2 atribute obligatorii -------------------------------------------------------------------


def test_r2_flags_foundation_without_finish_coverage():
    p = {
        "slug": "p",
        "name": "X Fond de ten",
        "primaryCategorySlug": "fond-de-ten",
        "attributes": {"concerns": ["dry"]},  # lipsă finish + coverage
    }
    assert _v({"categories": _cats(), "products": [p]}, "required_attrs")


# --- R3 nume curate ----------------------------------------------------------------------------


def test_r3_flags_numeric_suffix():
    p = _fond("p")
    p["name"] = "X Fond de ten 250"
    assert _v({"categories": _cats(), "products": [p]}, "clean_names")


def test_r3_flags_duplicate_names():
    data = {"categories": _cats(), "products": [_fond("a"), _fond("b")]}  # ambele „X Fond de ten"
    assert _v(data, "clean_names")


def test_r3_allows_legit_spec_number():
    # „SPF 50" e o spec legitimă (număr precedat de majuscule), NU sufix rezidual de seed.
    p = _fond("p")
    p["name"] = "Solora Shield Cremă cu protecție solară SPF 50"
    assert not _v({"categories": _cats(), "products": [p]}, "clean_names")


# --- R4 coerență nume↔categorie ----------------------------------------------------------------


def test_r4_flags_brush_in_foundation_category():
    p = _fond("p")
    p["name"] = "X Pensula de machiaj pentru definire"  # unelte ≠ machiaj (categoria fond)
    assert _v({"categories": _cats(), "products": [p]}, "name_category_coherence")


# --- R5 categorySlugs în aceeași ramură --------------------------------------------------------


def test_r5_flags_hair_slug_on_makeup_product():
    p = _fond("p")
    p["categorySlugs"] = ["fond-de-ten", "sampoane"]  # sampoane root = ingrijirea-parului ≠ machiaj
    assert _v({"categories": _cats(), "products": [p]}, "categoryslug_roots")


# --- R6 diferențiatori la comparație -----------------------------------------------------------


def test_r6_flags_identical_products_same_category():
    def _p(slug):
        return {
            "slug": slug,
            "name": f"X Fond {slug}",  # nume diferite (izolăm R6 de R3)
            "primaryCategorySlug": "fond-de-ten",
            "price": 50,
            "rating": 4.5,
            "attributes": {"finish": "matte", "coverage": "full", "concerns": ["dry"]},
            "reviewSummary": {"topPros": ["bun"], "topCons": []},
        }

    v = _v({"categories": _cats(), "products": [_p("a"), _p("b")]}, "comparison_differentiators")
    assert v
    assert set(v[0]["product_slugs"]) == {"a", "b"}  # perechea marcată


# --- main(): exit codes (gate) -----------------------------------------------------------------


def test_main_passes_on_sample(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["audit", str(SAMPLE)])
    assert mod.main() == 0


def test_main_fails_on_broken_catalog(monkeypatch, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(
        json.dumps(
            {
                "categories": _cats(),
                "products": [
                    {
                        "slug": "p",
                        "name": "X Fond de ten 250",  # R3
                        "primaryCategorySlug": "fond-de-ten",
                        "attributes": {"concerns": ["ten uscat"]},  # R1 + R2
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(sys, "argv", ["audit", str(bad)])
    assert mod.main() == 1
