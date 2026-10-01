"""NX-376 — un raft numit prin CHEIA lui nu mai pierde în fața unui subraft cu aceeași etichetă.

Rularea pe producție din 2026-10-01 (`tasks/stage1/KERNEL-LIVE-2026-10-01.md`, clasa A4, k5 T2-T3):
«pentru mâini» / «și una de corp pentru piele foarte uscată» ⇒ `category = corp`. Vocabularul
pune cheile și etichetele în același index, iar `_best` alege nodul cel mai ADÂNC: rădăcina `corp`
(cheie exactă) pierdea în fața subraftului «Ingrijire personala > Corp» (aceeași etichetă, un
singur produs).
Cele 12 creme de mâini și cele 34 de creme de corp stau sub rădăcina `corp`.

Măsurat pe vocabularul REAL SOLE (830 de termeni, chei + etichete, toate dimensiunile): se schimbă
exact doi, «corp» și «Corp», amândoi spre rădăcină. Zero DB în teste.
"""

from __future__ import annotations

import pytest

from src.catalog.vocabulary import (
    CATEGORY_DIMENSION,
    CatalogVocabulary,
    ResolutionStatus,
    VocabEntry,
    resolve,
)

#: Forma rafturilor SOLE implicate (cheie, etichetă, cale, produse).
SOLE = CatalogVocabulary(
    business_id="b",
    dimensions={
        CATEGORY_DIMENSION: (
            VocabEntry("corp", "Corp", 60, path="corp"),
            VocabEntry("corp-ingrijirea-corpului", "Ingrijirea corpului", 40, path="corp/ingr"),
            VocabEntry("ingrijire-personala", "Ingrijire personala", 5, path="ip"),
            VocabEntry("ingrijire-personala-corp", "Corp", 1, path="ip/corp"),
            VocabEntry("ten-ingrijirea-tenului", "Ingrijirea tenului", 900, path="ten/i"),
            VocabEntry(
                "dermato-cosmetice-ingrijirea-tenului", "Ingrijirea tenului", 30, path="d/i"
            ),
            VocabEntry("machiaj", "Machiaj", 680, path="machiaj"),
            VocabEntry("machiaj-fata", "Fata", 300, path="machiaj/fata"),
        )
    },
)


@pytest.mark.parametrize("term", ["corp", "Corp", "CORP"])
def test_the_root_key_wins_over_a_deeper_label(term):
    res = resolve(SOLE, term, CATEGORY_DIMENSION)
    assert (res.status, res.key) == (ResolutionStatus.KNOWN, "corp")


def test_the_subshelf_is_still_reachable_by_its_own_key():
    res = resolve(SOLE, "ingrijire-personala-corp", CATEGORY_DIMENSION)
    assert (res.status, res.key) == (ResolutionStatus.KNOWN, "ingrijire-personala-corp")


def test_two_labels_at_the_same_depth_stay_ambiguous():
    """Regula de azi între etichete rămâne: «Ingrijirea tenului» e de două ori, la aceeași
    adâncime, deci întrebare, nu alegere."""
    res = resolve(SOLE, "Ingrijirea tenului", CATEGORY_DIMENSION)
    assert res.status == ResolutionStatus.AMBIGUOUS
    assert {c.key for c in res.candidates} == {
        "ten-ingrijirea-tenului",
        "dermato-cosmetice-ingrijirea-tenului",
    }


def test_a_label_only_match_keeps_the_most_specific_node():
    """«Fata» e doar etichetă (Machiaj > Fata): regula de azi, neschimbată."""
    res = resolve(SOLE, "Fata", CATEGORY_DIMENSION)
    assert (res.status, res.key) == (ResolutionStatus.KNOWN, "machiaj-fata")


def test_on_another_domain_the_key_wins_too():
    """Electronice: rădăcina `audio` și subraftul «Accesorii > Audio»."""
    vocab = CatalogVocabulary(
        business_id="b",
        dimensions={
            CATEGORY_DIMENSION: (
                VocabEntry("audio", "Audio", 200, path="audio"),
                VocabEntry("accesorii-audio", "Audio", 3, path="accesorii/audio"),
            )
        },
    )
    assert resolve(vocab, "audio", CATEGORY_DIMENSION).key == "audio"
    assert resolve(vocab, "accesorii-audio", CATEGORY_DIMENSION).key == "accesorii-audio"
