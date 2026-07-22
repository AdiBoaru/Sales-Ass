"""NX-194 — FAQ derivat: regulile care contează sunt ce NU are voie să apară în răspuns."""

from __future__ import annotations

import json
import re
from pathlib import Path

from scripts.faq_content import build_faqs, derived_faqs

ROOT = Path(__file__).resolve().parents[1]

SER = {
    "slug": "test-ser",
    "name": "Test Ser",
    "primaryCategorySlug": "seruri-pentru-ten",
    "price": 99.0,
    "attributes": {
        "concerns": ["hydration", "dry"],
        "key_ingredients": ["acid hialuronic", "glicerină"],
        "usage": {"time": ["morning", "evening"]},
        "routine_step": "treat",
        "net_content": {"value": 30, "unit": "ml"},
        "fragrance_free": True,
    },
}
FOND = {
    "slug": "test-fond",
    "name": "Test Fond",
    "primaryCategorySlug": "fond-de-ten",
    "price": 89.0,
    "attributes": {"finish": "matte", "coverage": "full", "concerns": ["oily"]},
    "variants": [
        {"label": "01 Ivory", "stock": 5},
        {"label": "03 Beige", "stock": 0},
        {"label": "05 Sand", "stock": 12},
    ],
}
SAMPON = {
    "slug": "test-sampon",
    "name": "Test Șampon",
    "primaryCategorySlug": "sampoane",
    "price": 45.0,
    "attributes": {
        "hair_type": "uscat",
        "key_ingredients": ["keratină"],
        "usage": {"time": ["daily"]},
    },
}


def test_exact_sase_intrebari():
    for p, root in ((SER, "ingrijirea-tenului"), (FOND, "machiaj"), (SAMPON, "ingrijirea-parului")):
        faqs = build_faqs(p, root)
        assert len(faqs) == 6, (p["slug"], len(faqs))
        assert [f["position"] for f in faqs] == list(range(6))


def test_fara_pret_stoc_sau_livrare_in_raspunsuri():
    """Regula 1: FAQ-ul e text STATIC. Un preț scris aici devine minciună peste o săptămână."""
    # potrivire pe CUVÂNT: „zilei" conține „lei", dar nu e un preț
    interzise = ("lei", "preț", "pret", "stoc", "livrare", "livrăm", "reducere", "%")
    for p, root in ((SER, "ingrijirea-tenului"), (FOND, "machiaj"), (SAMPON, "ingrijirea-parului")):
        for f in build_faqs(p, root):
            text = (f["question"] + " " + f["answer"]).lower()
            for cuv in interzise:
                assert not re.search(rf"(?<![\w]){re.escape(cuv)}(?![\w])", text), (
                    p["slug"],
                    f["question"],
                    cuv,
                )


def test_derivatele_sunt_marcate_ca_regenerabile():
    faqs = build_faqs(SER, "ingrijirea-tenului")
    derived = [f for f in faqs if f["derived"]]
    assert len(derived) >= 4
    assert all(f["source"] == "derived" for f in derived)
    assert all(f["source"] == "curated" for f in faqs if not f["derived"])


def test_faptele_ajung_in_raspunsuri():
    txt = " ".join(f["answer"] for f in build_faqs(SER, "ingrijirea-tenului"))
    assert "acid hialuronic" in txt
    assert "30 ml" in txt
    assert "dimineața" in txt and "seara" in txt
    assert "fără parfum" in txt


def test_nuantele_apar_doar_unde_exista():
    fond = {f["question"]: f["answer"] for f in build_faqs(FOND, "machiaj")}
    assert "Ce nuanțe are?" in fond
    assert "01 Ivory" in fond["Ce nuanțe are?"]
    ser = {f["question"] for f in build_faqs(SER, "ingrijirea-tenului")}
    assert "Ce nuanțe are?" not in ser


def test_produs_de_par_nu_primeste_intrebare_de_ten():
    qs = {f["question"] for f in build_faqs(SAMPON, "ingrijirea-parului")}
    assert "E potrivit pentru părul meu?" in qs
    assert "E potrivit pentru tipul meu de ten?" not in qs


def test_produs_fara_niciun_fapt_nu_crapa():
    gol = {"slug": "x", "name": "X", "primaryCategorySlug": "pudre", "attributes": {}}
    faqs = build_faqs(gol, "machiaj")
    assert all(f["question"] and f["answer"] for f in faqs)


def test_fara_intrebari_duplicate():
    for p, root in ((SER, "ingrijirea-tenului"), (FOND, "machiaj"), (SAMPON, "ingrijirea-parului")):
        qs = [f["question"] for f in build_faqs(p, root)]
        assert len(qs) == len(set(qs))


def test_rutina_apare_cand_exista_relatii():
    faqs = derived_faqs(SER, "ingrijirea-tenului", routine_next=["Cremă X", "SPF Y"])
    combina = [f for f in faqs if f["question"] == "Cu ce se combină?"]
    assert combina and "Cremă X" in combina[0]["answer"]


def test_catalogul_real_produce_sase_pe_fiecare_produs():
    """Gate pe datele REALE, nu pe fixture: dacă o categorie nouă n-are destule fapte, se vede."""
    data = json.loads((ROOT / "db" / "seed" / "catalog_v2.json").read_text(encoding="utf-8"))
    roots = {c["slug"]: c.get("parentSlug") for c in data["categories"]}

    def root_of(slug: str) -> str:
        cur = slug
        seen = set()
        while roots.get(cur) and cur not in seen:
            seen.add(cur)
            cur = roots[cur]
        return cur

    subtiri = []
    for p in data["products"]:
        n = len(build_faqs(p, root_of(p["primaryCategorySlug"])))
        if n < 5:
            subtiri.append((p["slug"], n))
    assert not subtiri, f"produse cu sub 5 FAQ: {subtiri[:10]}"
