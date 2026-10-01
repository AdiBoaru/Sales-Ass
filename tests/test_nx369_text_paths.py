"""NX-369 — textul modelului nu se mai pierde: regulile parafrazate și handle-urile din proză.

Ambele vin din setul `prod-2026-10-01`, înregistrate și rejucate (`tests/replay`):
- c14 T0: răspunsul corect despre retur, parafrazat, respins ⇒ „Momentan n-am găsit produse";
- c5 T0: „P1 are SPF 50…, P2 și P6 sunt…" ⇒ scrub-ul arunca propozițiile, trei carduri fără text.
"""

from __future__ import annotations

from src.agent.finalize import _handle_names, _resolve_handles
from src.agent.store_rules import quoted_rules

# Regulile magazinului SOLE, exact cum le primește modelul (`faq_tools`, `sources`).
RULES = [
    "30 de zile calendaristice de la cumpărare. E peste minimul legal, iar dreptul tău de "
    "retragere în 14 zile rămâne neatins.",
    "Ai 30 de zile de la cumpărare. Intri pe www.service-return.com, la secțiunea „Returnează un "
    'produs", completezi datele și validezi cererea, apoi curierul vine să ridice produsul '
    "împreună cu documentele.",
    "Taxa de transport pentru retur e 49,9 lei și se reține din suma pe care ți-o rambursăm. "
    "Dacă produsul e defect, returul e gratuit.",
    "Da, cât timp nu are urme de utilizare, iar cutia e întreagă și completă. Adaugă în colet și o "
    "copie a documentului de achiziție.",
    "Taxa de livrare e între 19,9 și 24,90 lei cu TVA inclus, iar suma exactă o vezi la "
    "finalizarea comenzii. Peste 199 lei transportul e gratuit, iar dacă ai mai comandat la noi "
    "pragul scade la 149 lei.",
]

C14_MODEL_TEXT = (
    "Da, poți solicita returul în 30 de zile de la cumpărare. Pentru o cremă deschisă, aceasta nu "
    "trebuie să aibă urme de utilizare, iar cutia trebuie să fie întreagă și completă. Inițiezi "
    "cererea pe www.service-return.com și incluzi o copie a documentului de achiziție."
)


def test_c14_paraphrase_maps_to_the_store_rules_it_restates():
    assert quoted_rules(C14_MODEL_TEXT, RULES, "ro") == [RULES[1], RULES[3]]


def test_c7_delivery_paraphrase_maps_to_the_delivery_rule():
    text = (
        "Livrarea costă între 19,90 și 24,90 lei, cu TVA inclus. Suma exactă apare la finalizarea "
        "comenzii."
    )
    assert quoted_rules(text, RULES, "ro") == [RULES[4]]


def test_unrelated_text_maps_to_nothing():
    text = "Îți recomand o cremă hidratantă cu ceramide, potrivită pentru tenul uscat."
    assert quoted_rules(text, RULES, "ro") == []


def test_short_sentences_link_nothing():
    assert quoted_rules("Vrei și altceva? Spune-mi.", RULES, "ro") == []


def test_no_sources_or_no_text():
    assert quoted_rules(C14_MODEL_TEXT, [], "ro") == []
    assert quoted_rules("", RULES, "ro") == []


def test_exact_duplicate_rules_take_the_first():
    dup = [RULES[4], RULES[4]]
    text = "Peste 199 lei transportul e gratuit, iar la a doua comandă pragul scade la 149 lei."
    assert quoted_rules(text, dup, "ro") == [RULES[4]]


# --- handle-urile din proză --------------------------------------------------------------------

PRODUCTS = [
    {
        "id": "a",
        "name": "REAL BARRIER Peach Fit Tone Up Sun Cream SPF 50+ PA++++ 50 ml, crema de fata "
        "formulata cu oxid de zinc si niacinamida, care contribuie la protectia solara",
    },
    {"id": "b", "name": "BEAUTY OF JOSEON Daily Tinted Fluid Sunscreen SPF30 PA+++"},
]
HANDLES = {"P1": "a", "P2": "b"}


def test_handles_in_prose_become_the_card_names():
    j = {
        "intro": "Pentru tenul gras, P1 are SPF 50, iar P2 e nuanțatoare.",
        "education": "Dacă te preocupă porii, P1 e alegerea potrivită.",
        "items": [{"product_id": "P1", "fit_clause": "Mai lejeră decât P2."}],
        "pick": {"product_id": "P2", "justification": "P2 lasă un finish natural."},
    }
    out = _resolve_handles(j, HANDLES, _handle_names(PRODUCTS, HANDLES))
    assert "P1" not in out["intro"] and "P2" not in out["intro"]
    assert out["intro"].startswith("Pentru tenul gras, REAL BARRIER Peach Fit Tone Up Sun Cream")
    assert "crema de fata formulata" not in out["intro"]  # numele scurt de pe card, nu cel lung
    assert out["items"][0]["product_id"] == "a"
    assert "BEAUTY OF JOSEON" in out["items"][0]["fit_clause"]
    assert out["pick"]["product_id"] == "b" and out["pick"]["justification"].startswith("BEAUTY")
    assert j["intro"].startswith("Pentru tenul gras, P1")  # copie, nu mutație (diagnoza brută)


def test_unknown_handles_and_lookalikes_stay():
    j = {"intro": "P9 nu există, iar SPF 50 și PA++++ nu sunt handle-uri.", "items": []}
    out = _resolve_handles(j, HANDLES, _handle_names(PRODUCTS, HANDLES))
    assert out["intro"] == j["intro"]


def test_without_names_the_old_behaviour_holds():
    j = {"intro": "P1 e bună.", "items": [{"product_id": "P1"}]}
    out = _resolve_handles(j, HANDLES)
    assert out["intro"] == "P1 e bună." and out["items"][0]["product_id"] == "a"
