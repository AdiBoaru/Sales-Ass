"""Codex R8 — `has_url` single-source (text_scrub) + matrice adversarială.

Protocol: testează CLASA, nu exemplul. URL în oricare formă (http/www/path/bare) NU are voie în
proză sau într-un fapt (anti-injecție/phishing); linkurile legitime vin din offer/checkout.
"""

import pytest

from src.worker.text_scrub import has_url


@pytest.mark.parametrize(
    "text,expected",
    [
        ("https://shop.example.com/p", True),  # http(s)://
        ("http://x.ro", True),
        ("Detalii pe www.example.com", True),  # www.
        ("Comandă la shop.sole-demo.ro/p/x", True),  # domeniu cu path
        ("Vezi example.com", True),  # domeniu GOL, TLD cunoscut
        ("magazin.ro acum", True),
        ("site.online", True),
        ("evil.ai", True),  # Codex R9: ccTLD/gTLD care lipseau
        ("shop.hu", True),
        ("brand.eu", True),
        ("example.co", True),
        ("shop.co.uk pagina", True),  # TLD compus
        ("Bun pentru ten uscat", False),  # proză curată
        ("Rezistă 8 ore", False),  # cifră reală, NU URL
        ("4.9 stele", False),
        ("n/a", False),
        ("e.g. produsul", False),  # nu e TLD
        ("S.R.L. Cosmetics", False),
        ("", False),
        (None, False),
    ],
)
def test_has_url_adversarial(text, expected):
    assert has_url(text) is expected


def test_clean_facts_output_drops_urls():
    # Codex R9: test pe OUTPUT-ul _clean_facts (nu doar has_url izolat)
    from src.worker.compose import _clean_facts

    out = _clean_facts(["Bun pentru ten uscat", "Vezi evil.ai", "Comandă shop.co.uk/p"])
    assert out == ["Bun pentru ten uscat"]


def test_evidence_menu_output_drops_urls():
    # Codex R9: test pe OUTPUT-ul _evidence_facts / evidence_menu
    from src.agent.envelope import evidence_menu

    p = {"id": "p1", "name": "A", "price": 50.0, "top_pros": ["Textură lejeră", "vezi brand.eu"]}
    facts = list(evidence_menu([p])["p1"].values())
    assert facts == ["Textură lejeră"]
