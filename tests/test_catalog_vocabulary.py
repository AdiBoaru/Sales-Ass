"""Teste pentru vocabularul derivat + rezoluția tri-state (`src/catalog/vocabulary.py`).

Fiecare test de mai jos ține un defect MĂSURAT, nu unul imaginat: toate au fost găsite rulând
modulul pe catalogul real al tenantului demo. Cele două cu adevărat importante sunt
`test_overlay_target_dead_*` (defectul care a rulat cinci săptămâni nedetectat) și
`test_known_requires_evidence` (invarianta care îl face imposibil de repetat).

Pur: fără DB, fără rețea, fără credite.
"""

from __future__ import annotations

import pytest

from src.catalog.vocabulary import (
    CATEGORY_DIMENSION,
    CatalogVocabulary,
    Resolution,
    ResolutionStatus,
    VocabEntry,
    _keep_dimension,
    resolve,
    resolve_any,
)


def _vocab(**dimensions: tuple[VocabEntry, ...]) -> CatalogVocabulary:
    return CatalogVocabulary(business_id="biz-1", dimensions=dict(dimensions))


CREME = VocabEntry(key="creme-hidratante", label="Creme hidratante", count=7, path="ten/creme")
MASTI = VocabEntry(key="masti-pentru-ten", label="Măști pentru ten", count=16, path="ten/masti")
SERURI = VocabEntry(key="seruri-pentru-ten", label="Seruri pentru ten", count=15, path="ten/seruri")
DRY = VocabEntry(key="dry", label="dry", count=83)
OILY = VocabEntry(key="oily", label="oily", count=23)


# --- invarianta centrală -----------------------------------------------------


def test_known_requires_evidence() -> None:
    """`KNOWN` fără produse în spate nu poate fi CONSTRUIT. Asta e toată apărarea: un token mort
    n-are cum să iasă din rezolvare arătând ca unul valid."""
    with pytest.raises(ValueError, match="fără dovadă"):
        Resolution(
            status=ResolutionStatus.KNOWN,
            term="ten",
            dimension=CATEGORY_DIMENSION,
            key="ten",
            count=0,
        )


def test_unknown_never_constrains() -> None:
    """Un termen nerezolvat nu produce NICIODATĂ chei pentru `WHERE` — exact pasul care lipsea."""
    vocab = _vocab(category=(CREME,))
    r = resolve(vocab, "electrica", CATEGORY_DIMENSION)
    assert r.status is ResolutionStatus.UNKNOWN
    assert r.constraint_keys == ()
    assert r.evidence == 0


def test_ambiguous_constrains_on_union() -> None:
    """«Ten» nu se poate rezolva la o categorie anume, dar RĂMÂNE o cerere despre ten: constrângem
    pe uniunea candidaților, care nu poate fi goală. Fără asta, măștile ar câștiga pe text."""
    vocab = _vocab(category=(MASTI, SERURI))
    r = resolve(vocab, "ten", CATEGORY_DIMENSION)
    assert r.status is ResolutionStatus.AMBIGUOUS
    assert set(r.constraint_keys) == {"masti-pentru-ten", "seruri-pentru-ten"}
    assert r.evidence == 31


# --- regresia care a rulat cinci săptămâni -----------------------------------


def test_overlay_target_dead_does_not_become_a_filter() -> None:
    """Harta tenantului traduce „ten uscat" → „ten uscat", dar catalogul are „dry".

    Înainte, traducerea „reușea" și producea un filtru care golea orice căutare, tăcut. Acum e
    `UNKNOWN` cu motiv propriu, deci raportabil — și, mai important, nu ajunge în `WHERE`.
    """
    vocab = _vocab(concerns=(DRY, OILY))
    r = resolve(vocab, "ten uscat", "concerns", overlay={"ten uscat": "ten uscat"})
    assert r.status is ResolutionStatus.UNKNOWN
    assert r.reason == "overlay_target_dead"
    assert r.constraint_keys == ()


def test_overlay_target_alive_resolves_with_evidence() -> None:
    """Aceeași hartă, reparată: ținta există ⇒ `KNOWN`, cu dovada din catalog."""
    vocab = _vocab(concerns=(DRY, OILY))
    r = resolve(vocab, "ten uscat", "concerns", overlay={"ten uscat": "dry"})
    assert r.status is ResolutionStatus.KNOWN
    assert r.key == "dry"
    assert r.count == 83
    assert r.matched_by == "overlay"


def test_resolve_any_keeps_the_most_informative_reason() -> None:
    """Când nimic nu se rezolvă, `overlay_target_dead` (drift de configurare) trebuie să
    supraviețuiască peste un `not_in_vocabulary` întâmplător — altfel alarma se pierde."""
    vocab = _vocab(category=(CREME,), concerns=(DRY,))
    r = resolve_any(vocab, "ten uscat", overlays={"concerns": {"ten uscat": "ten uscat"}})
    assert r.status is ResolutionStatus.UNKNOWN
    assert r.reason == "overlay_target_dead"


# --- potriviri false găsite pe catalogul real --------------------------------


def test_single_word_entry_does_not_capture_longer_query() -> None:
    """`hair_type='uscat'` NU are voie să rezolve „ten uscat".

    Măsurat pe catalogul demo: cu subset în ambele sensuri fără restricție, o cerere de îngrijire
    a tenului ateriza pe produse de păr. Un singur cuvânt se potrivește exact, sau deloc.
    """
    vocab = _vocab(hair_type=(VocabEntry(key="uscat", label="uscat", count=17),))
    assert resolve(vocab, "ten uscat", "hair_type").status is ResolutionStatus.UNKNOWN
    assert resolve(vocab, "uscat", "hair_type").status is ResolutionStatus.KNOWN


def test_multiword_entry_still_matches_longer_query() -> None:
    """Restricția de mai sus nu trebuie să omoare potrivirile legitime: o intrare de două cuvinte
    rămâne recognoscibilă într-o cerere mai lungă."""
    vocab = _vocab(category=(CREME,))
    r = resolve(vocab, "creme hidratante pentru ten", CATEGORY_DIMENSION)
    assert r.status is ResolutionStatus.KNOWN
    assert r.key == "creme-hidratante"


def test_deeper_category_wins_over_shallower() -> None:
    """La potrivire textuală egală, frunza bate rădăcina: e specificitate, nu ambiguitate."""
    root = VocabEntry(key="ten", label="Ten", count=104, path="ten")
    leaf = VocabEntry(key="ten", label="Ten", count=7, path="ingrijire/ten")
    r = resolve(_vocab(category=(root, leaf)), "ten", CATEGORY_DIMENSION)
    assert r.status is ResolutionStatus.KNOWN
    assert r.count == 7


# --- descoperirea dimensiunilor ----------------------------------------------


def test_prose_dimension_is_rejected() -> None:
    """`best_for` / `key_benefit` au valori-frază, care conțin cuvinte comune și fură cereri.
    Testul e pe media cuvintelor, deci funcționează pe orice limbă și orice vertical."""
    prose = [
        ("cine vrea un finish luminos", 15),
        ("ten deshidratat și uscat și mixt", 11),
        ("cine vrea un finish satinat", 13),
    ]
    assert _keep_dimension(prose) is False


def test_identifier_dimension_is_rejected() -> None:
    """O cheie în care aproape fiecare produs are altă valoare e un SKU, nu vocabular."""
    assert _keep_dimension([(f"sku-{i}", 1) for i in range(50)]) is False


def test_real_vocabulary_dimension_is_kept() -> None:
    """Iar una în care valorile se repetă și sunt scurte E vocabular."""
    assert _keep_dimension([("dry", 83), ("oily", 23), ("acid hialuronic", 42)]) is True


def test_servable_labels_are_what_the_prompt_may_announce() -> None:
    """Promptul primește etichete de categorii care există; intrările fără produse nici nu ajung
    în vocabular, deci nu pot fi anunțate."""
    vocab = _vocab(category=(CREME, MASTI))
    assert vocab.servable_category_labels() == ("Creme hidratante", "Măști pentru ten")


def test_empty_dimension_is_never_a_filter() -> None:
    """Dimensiune inexistentă la tenant ⇒ `UNKNOWN`, nu un filtru care golește."""
    r = resolve(_vocab(category=(CREME,)), "orice", "dimensiune_inexistenta")
    assert r.status is ResolutionStatus.UNKNOWN
    assert r.reason == "unknown_dimension"
    assert r.constraint_keys == ()
