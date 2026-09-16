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
    named_topic_roots,
    resolve,
    resolve_any,
    topic_root_of,
    topic_switched,
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


def test_constant_dimension_is_rejected() -> None:
    """O cheie cu o singură valoare descrie RAFTUL, nu o alegere.

    Cazul real: `compliance` = `["CPNP"]` pe 2.711 din 2.758 de produse SOLE. Trece testul de
    repetiție (valorile se repetă) și pe cel de lungime (un cuvânt), dar nu desparte catalogul în
    nimic. Prezența ei nu e inofensivă: `resolve_any` încearcă TOATE dimensiunile, deci cheia
    concură pentru termenii clientului și îi consumă — măsurat, „seara" se rezolva pe `compliance`
    cu verdict UNKNOWN, iar filtrul de `routine_time` (populat pe 2.758/2.758) nu rula niciodată.
    """
    assert _keep_dimension([("CPNP", 2711)]) is False


def test_near_constant_dimension_is_rejected() -> None:
    """Și una în care o valoare acoperă practic tot: informația e la fel de aproape de zero."""
    assert _keep_dimension([("CPNP", 2711), ("ALTCEVA", 3)]) is False


def test_a_dominant_but_informative_dimension_is_kept() -> None:
    """Dar un dezechilibru NORMAL nu descalifică: `routine_time` pe SOLE e `am_pm` 1992 / `am` 255
    / `pm` 146 — dominantă, și totuși alegerea clientului separă catalogul."""
    assert _keep_dimension([("am_pm", 1992), ("am", 255), ("pm", 146)]) is True


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


# --- simetria tokenizării + subiectul conversației (incident 2026-09-16) -----
#
# Intrările de mai jos sunt cele REALE din catalogul SOLE (slug, etichetă, cale), fiindcă defectul
# a apărut din forma lor: etichete care se repetă pe două ramuri, un raft numit cu un singur
# cuvânt, și o subcategorie de machiaj („Fata") al cărei nume apare firesc într-o cerere de ten.

PAR = VocabEntry(key="par", label="Par", count=234, path="par")
PAR_ING = VocabEntry(
    key="par-ingrijirea-parului",
    label="Ingrijirea parului",
    count=205,
    path="par/ingrijirea-parului",
)
DERMA_PAR = VocabEntry(
    key="dermato-cosmetice-ingrijirea-parului",
    label="Ingrijirea parului",
    count=1,
    path="dermato-cosmetice/ingrijirea-parului",
)
MACHIAJ = VocabEntry(key="machiaj", label="Machiaj", count=681, path="machiaj")
MACHIAJ_BUZE = VocabEntry(key="machiaj-buze", label="Buze", count=298, path="machiaj/buze")
MACHIAJ_FATA = VocabEntry(key="machiaj-fata", label="Fata", count=274, path="machiaj/fata")
TEN = VocabEntry(key="ten", label="Ten", count=1461, path="ten")
TEN_ING = VocabEntry(
    key="ten-ingrijirea-tenului",
    label="Ingrijirea tenului",
    count=933,
    path="ten/ingrijirea-tenului",
)

_SOLE = (PAR, PAR_ING, DERMA_PAR, MACHIAJ, MACHIAJ_BUZE, MACHIAJ_FATA, TEN, TEN_ING)


def test_slug_spelling_resolves_like_label_spelling() -> None:
    """Defectul, în forma lui minimă: o CRATIMĂ decidea dacă filtrul de categorie rulează.

    Pe trafic real, modelul a cerut aceeași categorie de două ori în două tururi consecutive —
    «ingrijirea parului» (AMBIGUOUS, 206 produse în spate) și «ingrijirea-parului»
    (`UNKNOWN(not_in_vocabulary)`). Forma cu cratimă e cea pe care modelul o scrie natural,
    fiindcă slug-urile arată așa. Un UNKNOWN nu ajunge în `WHERE`, deci a doua cerere a rulat
    fără niciun filtru de raft."""
    vocab = _vocab(category=_SOLE)
    with_space = resolve(vocab, "ingrijirea parului", CATEGORY_DIMENSION)
    with_hyphen = resolve(vocab, "ingrijirea-parului", CATEGORY_DIMENSION)
    assert with_hyphen.constraint_keys, "categoria scrisă ca slug nu mai are voie să iasă UNKNOWN"
    assert with_hyphen.status is with_space.status
    assert set(with_hyphen.constraint_keys) == set(with_space.constraint_keys)


def test_topic_switch_detected_without_any_help_from_triage() -> None:
    """Cazul incidentului: discuție despre un ruj, apoi „vreau sa vad produse de par". Triajul nu
    rulează pe turul ăsta (vine după o clarificare), deci detectarea trebuie să vină din catalog."""
    assert topic_switched(_vocab(category=_SOLE), "vreau sa vad produse de par", "machiaj-buze")


def test_refinement_is_not_a_topic_switch() -> None:
    """O rafinare nu numește niciun raft ⇒ stiva se păstrează, exact ca înainte."""
    assert (
        topic_switched(_vocab(category=_SOLE), "mai ieftin, sub 100 lei", "machiaj-buze") is False
    )


def test_naming_the_same_shelf_is_not_a_switch() -> None:
    """„alt sampon de par" numește raftul CURENT: e o lărgire, nu o schimbare de subiect."""
    assert (
        topic_switched(_vocab(category=_SOLE), "alt sampon de par", "par-ingrijirea-parului")
        is False
    )


def test_subcategory_name_does_not_trigger_a_switch() -> None:
    """De ce doar rădăcinile: «Fata» și «Buze» sunt subcategorii de MACHIAJ, iar cuvintele lor apar
    firesc în cereri de pe alt raft. „o crema de fata" e o cerere de ten; dacă subcategoriile ar
    conta, ar reseta stiva unei discuții despre ten."""
    vocab = _vocab(category=_SOLE)
    assert topic_switched(vocab, "vreau o crema de fata", "ten-ingrijirea-tenului") is False
    assert named_topic_roots(vocab, "vreau o crema de fata") == frozenset()


def test_unknown_previous_shelf_never_switches() -> None:
    """Fail-closed: dacă nu putem afla pe ce raft stă stiva, nu inventăm o schimbare de subiect."""
    vocab = _vocab(category=_SOLE)
    assert topic_switched(vocab, "vreau sa vad produse de par", "categorie-stearsa") is False
    assert topic_switched(vocab, "vreau sa vad produse de par", None) is False


def test_empty_vocabulary_never_switches() -> None:
    """Vocabular indisponibil (DB jos) ⇒ comportamentul de dinainte, nu un reset ghicit."""
    assert topic_switched(_vocab(), "vreau sa vad produse de par", "machiaj-buze") is False


def test_topic_root_is_the_first_path_segment() -> None:
    vocab = _vocab(category=_SOLE)
    assert topic_root_of(vocab, "machiaj-buze") == "machiaj"
    assert topic_root_of(vocab, "par-ingrijirea-parului") == "par"
    assert topic_root_of(vocab, "inexistent") is None


def test_known_false_positive_is_pinned_not_hidden() -> None:
    """Costul acceptat al detectorului, scris ca test ca să nu se schimbe tăcut.

    «Par» e și nume de raft, și formă verbală: „mi se par cam scumpe" resetează stiva degeaba.
    E jumătatea ieftină a asimetriei (pierdere de context, nu răspuns greșit), iar alternativa —
    o regulă gramaticală per limbă — ar contrazice P11. Dacă cineva o repară, testul ăsta trebuie
    să pice și să fie rescris, nu ocolit."""
    assert topic_switched(_vocab(category=_SOLE), "mi se par cam scumpe", "ten-ingrijirea-tenului")
