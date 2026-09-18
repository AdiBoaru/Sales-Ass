"""NX-303 — când cuvântul clientului nu mai spune nimic peste filtre.

Turul real `c820226a` («vreau ceva sa scap de cosuri»): agentul a înțeles perfect și a trimis el
însuși sinonimele (`concerns=["coșuri","imperfecțiuni","acnee"]`, toate rezolvate pe `acne` = 518
produse). Dar `query` se leagă cu ȘI peste filtre, iar pe catalogul SOLE „cosuri" apare în textul
de căutare al **6** produse, „acnee" în 165, „imperfectiuni" în 183.

Testele de aici apără regula (`text_is_redundant_as_gate`) și, mai ales, LIMITELE ei: greșeala
scumpă e într-o singură direcție — a servi raftul când clientul chiar cerea altceva.
"""

from __future__ import annotations

from src.catalog.vocabulary import (
    CatalogVocabulary,
    VocabEntry,
    text_is_redundant_as_gate,
)


def _vocab() -> CatalogVocabulary:
    """Vocabular minimal, în forma pe care o produce catalogul: `concerns` cu o cheie reală."""
    return CatalogVocabulary(
        business_id="biz-1",
        dimensions={
            "concerns": (
                VocabEntry(key="acne", label="Acnee", count=518),
                VocabEntry(key="hydration", label="Hidratare", count=300),
            )
        },
    )


OVERLAYS = {"concerns": {"cosuri": "acne", "imperfectiuni": "acne", "acnee": "acne"}}


def test_the_clients_word_is_redundant_when_the_filter_already_carries_it() -> None:
    """Cazul măsurat: «cosuri» se rezolvă pe `acne`, iar `acne` e deja în WHERE."""
    assert text_is_redundant_as_gate(_vocab(), ["cosuri"], {"acne"}, overlays=OVERLAYS) is True


def test_a_single_unresolved_word_keeps_todays_path() -> None:
    """Poarta esențială. «crema pentru cosuri» fără filtru de tip: „crema" DISCRIMINEAZĂ real, deci
    textul nu e redundant și drumul rămâne cel de azi, byte-identic.

    Fără regula asta, cardul ar fi transformat o îngustare corectă („vreau o CREMĂ") într-o pagină
    de raft — adică exact defectul pe care îl repară, în oglindă."""
    terms = ["crema", "cosuri"]
    assert text_is_redundant_as_gate(_vocab(), terms, {"acne"}, overlays=OVERLAYS) is False


def test_a_word_resolving_outside_the_applied_filters_is_not_redundant() -> None:
    """«cosuri» purtat de filtru, dar «hidratare» nu: fraza cere ceva ce WHERE-ul nu garantează."""
    assert (
        text_is_redundant_as_gate(_vocab(), ["cosuri", "hidratare"], {"acne"}, overlays=OVERLAYS)
        is False
    )


def test_no_content_terms_is_not_redundancy() -> None:
    """`all()` peste mulțimea vidă e `True`, iar aici ar fi fost dezastruos: ORICE frază fără
    cuvinte de conținut ar fi sărit poarta de text. Mulțimea vidă nu e o dovadă."""
    assert text_is_redundant_as_gate(_vocab(), [], {"acne"}, overlays=OVERLAYS) is False


def test_an_unknown_word_is_not_redundancy() -> None:
    """Un cuvânt pe care catalogul nu-l cunoaște nu poate fi „purtat de filtre"."""
    assert text_is_redundant_as_gate(_vocab(), ["cablu"], {"acne"}, overlays=OVERLAYS) is False


def test_empty_applied_keys_never_triggers() -> None:
    """Fără filtru, treapta terminală nici nu există (NX-293): «nu am găsit» e răspunsul corect."""
    assert text_is_redundant_as_gate(_vocab(), ["cosuri"], set(), overlays=OVERLAYS) is False


# ── Cota pe tip: promptul și codul spun ACEEAȘI cifră ─────────────────────────────────────────


def test_the_type_quota_has_a_single_owner() -> None:
    """Înainte, `diversify_pool` plafona la 2 per tip iar promptul rich cerea „acoperă tipurile" —
    citit de model ca UNUL per tip. Două cifre pentru aceeași regulă, iar cea din prompt câștiga,
    fiindcă ea vorbește cu modelul. Măsurat pe turul real: pool de 6 produse cu 3 tipuri distincte
    ⇒ **3 carduri**, dintr-o nevoie pe care catalogul o acoperă cu 518 produse și 18 clase."""
    from src.agent.prompt_builder import _RICH_RULES
    from src.config import max_per_type

    assert "{MAX_PER_TYPE}" in _RICH_RULES, (
        "regula de tipuri nu mai citește marcatorul — cifra s-a întors în proza promptului"
    )
    assert "acoperă-le pe cele care ajută cererea" not in _RICH_RULES, (
        "formularea veche («acoperă tipurile») se citește ca UNUL per tip"
    )
    assert max_per_type() == 2


def test_the_prompt_renders_the_owners_number(monkeypatch) -> None:
    """Marcatorul chiar se substituie, și cu valoarea proprietarului — nu cu o constantă locală."""
    from src.config import Settings, get_settings

    assert Settings.model_fields["max_per_type"].default == 2
    monkeypatch.setattr(get_settings(), "max_per_type", 3, raising=False)
    from src.config import max_per_type

    assert max_per_type() == 3  # citit la APEL, nu înghețat la import


def test_none_still_disables_the_type_quota() -> None:
    """Contract preexistent al suitei: `max_per_type=None` = fără cotă. Default-ul nou citește
    configul printr-o SANTINELĂ tocmai ca să nu șteargă tăcut posibilitatea asta."""
    from src.tools import catalog_tools as ct

    cands = [
        {"id": f"p{i}", "name": f"Crema {i}", "price": 10.0 + i, "brand": f"B{i}"} for i in range(6)
    ]
    assert ct.diversify_pool(cands, 6, max_per_type=None) == cands
