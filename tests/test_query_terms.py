"""046 — termenii de căutare dintr-o frază de client (pur, fără DB).

Cazurile de aici sunt frazele REALE pe care s-a măsurat defectul pe catalogul SOLE, nu exemple
plauzibile: fiecare din ele întorcea zero rezultate cu clauza de dinainte.
"""

import pytest

from src.catalog.query_terms import (
    content_terms,
    fold,
    relaxed_query,
    stopwords,
    strict_query,
)


def test_fold_oglindeste_ro_unaccent():
    """Normalizarea din Python trebuie să dea EXACT ce dă `ro_unaccent` în SQL (033). Dacă cele
    două capete diverg, potrivirea nu se produce — e defectul pe care 033 l-a reparat."""
    assert fold("Șampon PĂRUL Îngrijit") == "sampon parul ingrijit"
    # formele cu sedilă (U+015F/U+0163), care apar în text copiat din surse vechi
    assert fold("şampon ţinuta") == "sampon tinuta"


def test_cuvintele_functionale_dispar_termenii_de_produs_raman():
    assert content_terms("sampon pentru par gras", "ro") == ["sampon", "par", "gras"]
    assert content_terms("vreau ceva pentru par uscat si deteriorat", "ro") == [
        "par",
        "uscat",
        "deteriorat",
    ]
    assert content_terms("ce imi recomanzi pentru riduri", "ro") == ["riduri"]


def test_termenii_scurti_care_poarta_sens_supravietuiesc():
    """Nu există prag pe lungime: „c" din „vitamina c" și „50" din „spf 50" discriminează."""
    assert content_terms("ser cu vitamina c", "ro") == ["ser", "vitamina", "c"]
    assert content_terms("protectie solara spf 50", "ro") == ["protectie", "solara", "spf", "50"]


def test_duplicatele_se_string_pastrand_ordinea():
    """«fond de ten pentru ten gras» — „ten" de două ori nu schimbă potrivirea, dar umflă
    tsquery-ul și rangul."""
    assert content_terms("fond de ten pentru ten gras", "ro") == ["fond", "ten", "gras"]


def test_niciodata_gol_cand_fraza_e_numai_umplutura():
    """P6: o cerere fără niciun termen ar deveni tăcere. Preferăm o căutare slabă, recuperabilă
    de treapta de relaxare, unei căutări fără niciun termen."""
    assert content_terms("ce imi recomanzi", "ro") == ["ce", "imi", "recomanzi"]
    assert content_terms("vreau ceva", "ro") == ["vreau", "ceva"]


def test_fara_text_nu_inventeaza_termeni():
    assert content_terms("", "ro") == []
    assert content_terms("   ???   ", "ro") == []


def test_locala_e_cheie_nu_constanta():
    """P11/D3: nu aplicăm româna peste o limbă pe care n-o cunoaștem — o locale necunoscută
    păstrează fraza întreagă, adică exact comportamentul de dinainte de 046."""
    assert stopwords("hu") == frozenset()
    assert stopwords(None) == frozenset()
    assert content_terms("sampon pentru par gras", "hu") == ["sampon", "pentru", "par", "gras"]
    assert content_terms("sampon pentru par gras", None) == ["sampon", "pentru", "par", "gras"]


def test_ro_ro_si_ro_sunt_aceeasi_limba():
    assert stopwords("ro-RO") == stopwords("ro")
    assert stopwords("RO") == stopwords("ro")


@pytest.mark.parametrize(
    "cuvant",
    ["crema", "ulei", "par", "ten", "ser", "masca", "sampon", "buze", "ochi", "acnee", "riduri"],
)
def test_niciun_cuvant_de_produs_nu_e_in_lista_de_cuvinte_goale(cuvant):
    """Regula de includere în listă: un cuvânt intră DOAR dacă nu poate numi niciodată un produs,
    un brand sau o nevoie. Testul o ține, ca lista să nu crească în cuvinte care taie recall."""
    assert cuvant not in stopwords("ro")


def test_forma_interogarilor_pentru_websearch_to_tsquery():
    """`websearch_to_tsquery` tratează „or" ca operator; termenii sunt deja `[0-9a-z]`, deci
    niciunul nu poate introduce sintaxă."""
    terms = ["sampon", "par", "gras"]
    assert strict_query(terms) == "sampon par gras"
    assert relaxed_query(terms) == "sampon or par or gras"


def test_termenii_nu_pot_purta_sintaxa_de_tsquery():
    """Injecție: orice nu e literă sau cifră devine separator, deci `&`, `|`, `!`, `:`, `'`
    și parantezele nu pot ajunge în tsquery."""
    got = content_terms("crema & (ulei | !ser) :* 'x'", "ro")
    assert got == ["crema", "ulei", "ser", "x"]
    assert all(t.isalnum() for t in got)
