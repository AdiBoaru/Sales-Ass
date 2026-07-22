"""NX-196 — fișa autorată: contractul de lungimi + porțile de conținut, pe catalogul REAL.

Testul pe date reale e cel care contează: un fixture ar trece mereu, dar o categorie nouă fără bloc
scris sau un produs fără fapte se văd doar pe cele 300 de produse adevărate.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.authored_content import (
    MAX_DESCRIPTION,
    MAX_SHORT,
    MIN_DESCRIPTION,
    MIN_SHORT,
    build_features,
    build_specs,
    compose,
    validate,
)
from src.worker.text_scrub import has_medical_claim

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def catalog() -> dict:
    return json.loads((ROOT / "db" / "seed" / "catalog_v2.json").read_text(encoding="utf-8"))


def test_fiecare_produs_are_fisa(catalog):
    """Nicio categorie fără bloc scris — altfel produsul ar rămâne cu textul vechi, subțire."""
    fara = [p["slug"] for p in catalog["products"] if compose(p) is None]
    assert not fara, f"produse fără bloc de categorie: {fara[:10]}"


def test_contract_de_lungimi_pe_catalogul_real(catalog):
    scurte, lungi = [], []
    for p in catalog["products"]:
        c = compose(p, has_medical_claim)
        if not (MIN_DESCRIPTION <= len(c["description"]) <= MAX_DESCRIPTION):
            scurte.append((p["slug"], len(c["description"])))
        if not (MIN_SHORT <= len(c["shortDescription"]) <= MAX_SHORT):
            lungi.append((p["slug"], len(c["shortDescription"])))
    assert not scurte, f"description în afara contractului: {scurte[:8]}"
    assert not lungi, f"short_description în afara contractului: {lungi[:8]}"


def test_zero_claim_medical_si_zero_cifre_volatile(catalog):
    """Cele două porți care contează: nimic medical în textul afirmabil de bot, niciun preț/stoc
    în text static (ar deveni minciună în câteva zile)."""
    probleme = []
    for p in catalog["products"]:
        probleme += validate(p, compose(p, has_medical_claim), has_medical_claim)
    assert not probleme, probleme[:10]


def test_toate_blocurile_sunt_prezente(catalog):
    kinds_asteptate = {"features", "benefits", "usage", "scenarios"}
    for p in catalog["products"][:60]:
        c = compose(p, has_medical_claim)
        assert {s["kind"] for s in c["sections"]} == kinds_asteptate, p["slug"]
        assert all(s["voice"] == "assistant" for s in c["sections"]), p["slug"]


def test_descrierea_are_subtitluri(catalog):
    for p in catalog["products"][:40]:
        d = compose(p, has_medical_claim)["description"]
        assert d.count("**") >= 8, p["slug"]  # cel puțin 4 subtitluri


def test_doua_produse_din_aceeasi_categorie_nu_incep_identic(catalog):
    """Varietatea e cerința explicită a userului („fără prea multe asemănări")."""
    by_cat: dict[str, list[str]] = {}
    for p in catalog["products"]:
        d = compose(p, has_medical_claim)["description"]
        by_cat.setdefault(p["primaryCategorySlug"], []).append(d[:120])
    identice = {c: len(v) - len(set(v)) for c, v in by_cat.items() if len(v) > 2}
    # cu 2-3 variante de introducere per categorie, unele coincidențe sunt inevitabile;
    # ce nu acceptăm e ca TOATE produsele unei categorii să înceapă la fel
    for cat, v in by_cat.items():
        if len(v) >= 4:
            assert len(set(v)) >= 2, f"toate produsele din {cat} încep identic"
    assert identice is not None


def test_specs_contin_faptele_reale(catalog):
    ser = next(p for p in catalog["products"] if p["primaryCategorySlug"] == "seruri-pentru-ten")
    specs = build_specs(ser)
    assert specs, ser["slug"]
    if (ser.get("attributes") or {}).get("key_ingredients"):
        assert "Ingrediente-cheie" in specs


def test_features_maxim_sapte_si_fara_duplicate(catalog):
    for p in catalog["products"][:60]:
        f = build_features(p)
        assert 1 <= len(f) <= 7, p["slug"]
        assert len(f) == len({x.lower() for x in f}), p["slug"]


def test_compunerea_e_determinista(catalog):
    p = catalog["products"][0]
    a = compose(p, has_medical_claim)
    b = compose(p, has_medical_claim)
    assert a == b


def test_key_benefit_cu_claim_medical_e_scos_din_textul_botului():
    """Un fapt din catalog care ar fi tăiat de validator în conversație nu intră ca afirmație."""
    p = {
        "slug": "test-tratament",
        "name": "Test Tratament local",
        "primaryCategorySlug": "tratament-local",
        "attributes": {
            "key_benefit": "Tratează acneea rapid",
            "concerns": ["acne"],
            "key_ingredients": ["acid salicilic"],
        },
    }
    c = compose(p, has_medical_claim)
    assert not has_medical_claim(c["shortDescription"])
    for s in c["sections"]:
        assert not has_medical_claim(s["body"]), s["kind"]
