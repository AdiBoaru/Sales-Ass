"""NX-286 — proza produsului: ce afirmă despre sine vs ce te sfătuiește să faci cu altceva.

Fiecare caz e o frază REALĂ din catalogul SOLE, nu una inventată: cele 10 care produceau valori
greșite de `spf` și cele corecte care trebuie să supraviețuiască gărzii. Motivul e în
`src/catalog/prose.py`.
"""

from __future__ import annotations

import re

import pytest

from src.catalog.prose import is_advice, sentence_with, sentences

SPF = re.compile(r"\bspf\s*([0-9]{1,2})\s*\+?", re.IGNORECASE)

#: Exact rădăcinile declarate pe fațeta `spf` în `db/seed/domain_pack_sole_ro.json`.
MARKERS = (
    "recomand",
    "folosit",
    "foloses",
    "folositi",
    "aplicati",
    "aplici",
    "sfaturi",
    "intotdeauna",
    "obligatoriu",
    "urmati",
    "nu uita",
)

#: Fraze reale care trimit către ALT produs. Toate au produs un `spf` greșit înainte de gardă.
ADVICE_REAL = [
    "Sfaturi pentru utilizare sigura: Folositi crema cu SPF 50+ in timpul zilei, deoarece "
    "retinolul face pielea mai sensibila la soare.",
    "Aplicati intotdeauna protectie solara (SPF 30 sau mai mare) pe timpul zilei pentru a preveni "
    "sensibilizarea pielii cauzata de retinoizi.",
    "Obligatoriu: Foloseste crema cu SPF 50 in fiecare dimineata, deoarece retinolul poate "
    "sensibiliza pielea la soare.",
    "Pentru utilizare in timpul zilei, urmati cu o protectie solara cu spectru larg evaluata "
    "SPF 30 sau mai mare.",
    "Deoarece produsul contine acizi (AHA/BHA), pe timp de zi se recomanda aplicarea unui produs "
    "cu protectie solara (SPF 30 sau mai mare).",
    "Deoarece retinolul creste sensibilitatea pielii la soare, este recomandat sa aplici zilnic, "
    "pe durata utilizarii, in rutina de zi, o crema cu SPF 50.",
]

#: Fraze reale în care SPF-ul E al produsului. Trebuie să treacă — sunt 33 din 44 pe catalog.
PROPERTY_REAL = [
    "Si pentru ca Purito asculta nevoile tale, au integrat si protectie solara SPF 30 PA+++, "
    "ideala pentru rutina ta zilnica.",
    "Da, ofera SPF 30 pentru protectie UVB si PA+++ pentru protectie UVA.",
    "Beneficii: -Protectie UV puternica SPF 50+ / PA ++++ protejeaza pielea impotriva expunerii "
    "la UV.",
    "Protectie UV completa cu SPF50+ si PA++++ impotriva razelor UVA si UVB.",
    "VT Mushroom Airy Sun Cream SPF 50+ PA++++ este o crema solara cu o textura extrem de usoara.",
    "RIEMANN P20 Sensitive Skin SPF 50+ este o crema solara inovatoare, conceputa pentru "
    "persoanele cu piele sensibila.",
]


@pytest.mark.parametrize("text", ADVICE_REAL)
def test_frazele_de_sfat_sunt_recunoscute(text):
    assert is_advice(text.lower(), MARKERS), text


@pytest.mark.parametrize("text", PROPERTY_REAL)
def test_frazele_care_descriu_produsul_trec(text):
    assert not is_advice(text.lower(), MARKERS), text


def test_fara_markeri_garda_nu_respinge_nimic():
    """P6: fără vocabular declarat în pachet, garda nu presupune nimic. Altfel o fațetă ar pierde
    valori pe un tenant care n-a declarat markeri, fără niciun semnal."""
    assert not is_advice(ADVICE_REAL[0].lower(), ())


def test_fraza_goala_nu_e_sfat():
    assert not is_advice(None, MARKERS)
    assert not is_advice("", MARKERS)


# --- segmentarea e parte din corectitudine, nu cosmetică ---------------------------------


def test_nbsp_nu_mai_topeste_descrierea_in_O_fraza():
    """Falsul pozitiv măsurat: *SKIN1004 Air-Fit Suncream SPF30*, o protecție solară REALĂ, era
    marcată ca sfat fiindcă descrierea folosește `&nbsp;` în loc de spațiu — fraza extrasă avea 908
    de caractere și înghițea textul de sfat de mai jos."""
    text = (
        "Air-Fit Suncream Light SPF30+ PA++++&nbsp;este un produs de protectie solara inovator."
        "&nbsp;Sfaturi: folositi zilnic."
    )
    first = sentence_with(text, SPF)
    assert first is not None
    assert len(first) < 120, first
    assert not is_advice(first.lower(), MARKERS)


def test_punctul_lipit_de_majuscula_e_granita():
    """„…volumizare.Deoarece produsul…" — sursa SOLE omite spațiul după punct. Fără tratarea asta,
    fraza de proprietate și cea de sfat se unesc, iar garda devine imprecisă în ambele direcții."""
    got = sentences("Ofera SPF 30 real.Deoarece contine acizi, folositi protectie solara.")
    assert len(got) == 2
    assert not is_advice(got[0].lower(), MARKERS)
    assert is_advice(got[1].lower(), MARKERS)


def test_se_judeca_FRAZA_nu_tot_textul():
    """Inima defectului: regexul căuta în TOT textul, deci o descriere care afirmă SPF-ul propriu
    și mai jos dă și un sfat era judecată ca un bloc. Garda izolează fraza cifrei."""
    text = (
        "Crema ofera protectie solara SPF 50 pe toata durata zilei. "
        "Va recomandam sa folositi si un produs cu SPF 30 pe corp."
    )
    first = sentence_with(text, SPF)
    assert first is not None and "SPF 50" in first
    assert not is_advice(first.lower(), MARKERS)


def test_substantivul_aplicarea_NU_e_un_imperativ():
    """Al doilea fals pozitiv măsurat, și cel mai instructiv: *Beauty of Joseon Ginseng Moist Sun
    Serum* e o protecție solară REALĂ, marcată greșit fiindcă markerul „aplica" se potrivea în
    interiorul substantivului „aplicarea". Fraza descrie produsul, nu dă o instrucțiune.

    Două lucruri l-au produs, amândouă pinuite aici: markerul din pachet era „aplica " (cu spațiu,
    ca să fie cuvânt întreg), iar `normalize()` i-a tăiat spațiul tăcut. Lista păstrează acum doar
    formele imperative, iar potrivirea e ancorată la început de cuvânt.
    """
    real = (
        "Beauty of Joseon Ginseng Moist Sun Serum SPF 50+ PA++++ a fost conceput cu ideea de a "
        "face aplicarea protectiei solare usoara si placuta."
    )
    assert not is_advice(real.lower(), MARKERS)
    assert "aplica" not in MARKERS, "markerul lacom nu trebuie reintrodus"


def test_un_marker_nu_poate_potrivi_in_INTERIORUL_unui_cuvant():
    """Garda generală peste clasa de greșeală de mai sus, independentă de lista de markeri."""
    assert not is_advice("produsul reaplicatie ceva", ("aplicati",))
    assert is_advice("aplicati produsul", ("aplicati",))


def test_sentence_with_intoarce_None_cand_nu_exista_potrivire():
    assert sentence_with("Fara nicio cifra aici.", SPF) is None
    assert sentence_with(None, SPF) is None
    assert sentences(None) == []


# --- pachetul și codul nu pot diverge ----------------------------------------------------


def test_markerii_din_test_sunt_cei_din_pachet():
    """Dacă pachetul se schimbă și testul nu, cazurile de mai sus măsoară altceva decât
    producția."""
    import json
    from pathlib import Path

    pack = json.loads(
        (
            Path(__file__).resolve().parent.parent / "db" / "seed" / "domain_pack_sole_ro.json"
        ).read_text(encoding="utf-8")
    )
    spf = next(f for f in pack["facets"] if f["key"] == "spf")
    assert tuple(spf["prose_advice_markers"]) == MARKERS
