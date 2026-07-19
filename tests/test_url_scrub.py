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
