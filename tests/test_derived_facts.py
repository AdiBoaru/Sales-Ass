"""NX-268 — faptele derivate: nu pentru relevanță, pentru garanții.

Un reranker poate să spună „ăsta pare potrivit". Un fapt verificat poate să spună „ăsta chiar n-are
parfum". Diferența e tot rostul cardului, iar prețul ei e că un fapt GREȘIT nu mai e prins de nimeni
în aval: validatorul stagiului 8 și `grounding_guard` (NX-240) verifică afirmațiile FAȚĂ DE tabela
de fapte, deci un atribut greșit se confirmă singur.

De aia testele astea sunt despre PRECIZIE, nu despre acoperire. Cazurile din card sunt marcate
(happy 1-2, edge 3-5, failure 6-7)."""

from __future__ import annotations

import json
import pathlib

from scripts.facet_coverage import compute_coverage
from src.catalog.derivation import (
    MAX_GAP,
    IngredientVocabulary,
    build_matchers,
    ingredient_head,
    match_keys,
    owned_facets,
    phrase_span,
    signal_name,
    tokens,
)
from src.catalog.query_terms import stopwords
from src.domain.facets import FacetSource, FacetType, TypedFacet
from src.domain.normalize import normalize

ROOT = pathlib.Path(__file__).resolve().parent.parent
POLICY = ROOT / "tests" / "derived_precision_policy.json"

# Vocabular MINIM, ca într-un pachet de tenant. Nu e „vocabularul SOLE": e forma pe care o are un
# `concern_map`, cu două chei care se pot confunda — exact ce trebuie să deosebească mecanismul.
CONCERN_MAP = {
    "ten gras": "oily",
    "exces de sebum": "oily",
    "ten uscat": "dry",
    "puncte negre": "acne",
}


def _matchers(stems=None, excludes=None):
    return build_matchers(CONCERN_MAP, stems=stems, excludes=excludes, normalize=normalize)


def _hits(text: str, **kw):
    return match_keys(tokens(normalize(text)), _matchers(**kw))


# --- potrivirea: frază, cu toleranță de flexiune ------------------------------------------------


def test_flexiunea_nu_mai_pierde_potrivirea():
    """Happy 1 — „ten gras" trebuie să prindă formele reale din text, nu doar forma de dicționar."""
    for text in (
        "potrivit pentru ten gras",
        "recomandat tenului gras",
        "pentru tenuri grase si mixte",
        "daca ai ten foarte gras",
    ):
        assert "oily" in _hits(text), text


def test_fraza_ramane_fraza():
    """Edge 3 — cazul care omoară stemurile: „acizi grași" NU e o mențiune de ten gras.

    Măsurat, e clasa de fals pozitiv din care venea cifra „93 → 1.120" a cardului. Cu potrivirea pe
    frază dispare prin construcție: „ten" pur și simplu nu apare. Nu e nevoie de nicio excludere —
    și tocmai de aia excluderile din pachet sunt goale."""
    assert _hits("ulei bogat in acizi grasi mononesaturati") == {}
    assert _hits("contine acid gras esential") == {}


def test_distanta_dintre_tokeni_e_marginita():
    """Un „ten" într-o propoziție și un „gras" în următoarea nu spun împreună nimic."""
    far = "ten " + " ".join(["cuvant"] * (MAX_GAP + 2)) + " gras"
    assert _hits(far) == {}
    near = "ten " + " ".join(["cuvant"] * MAX_GAP) + " gras"
    assert "oily" in _hits(near)


def test_ordinea_conteaza():
    """„gras" înaintea lui „ten" nu e fraza „ten gras" — e altă propoziție."""
    assert _hits("continut gras pentru orice ten") == {}


def test_coada_flexiunii_e_marginita():
    """Poarta care face prefixul sigur nu e lungimea tokenului, e cât adaugă flexiunea.

    „ten" trebuie să prindă „tenul" (+2) și „tenului" (+4), formele în care textul chiar scrie, dar
    nu „tendinta" (+5), care e alt cuvânt. Un prag pe lungimea TOKENULUI ar fi scos din joc exact
    cuvintele scurte care discriminează cel mai bine într-un catalog de cosmetice."""
    matchers = build_matchers({"ten gras": "oily"}, normalize=normalize)
    phrase = matchers["oily"].phrases[0]
    assert phrase_span(tokens(normalize("tendinta grasa")), phrase) is None
    assert phrase_span(tokens(normalize("tenului gras")), phrase) is not None


def test_backtracking_gaseste_a_doua_aparitie():
    """Fără backtracking, prima apariție a primului token consumă potrivirea și fraza reală de mai
    jos se pierde — măsurat, exact așa pierdea produse pe care fraza exactă le găsea."""
    assert "oily" in _hits("ten luminos si hidratat. potrivit pentru ten gras")


def test_produsul_fara_text_nu_primeste_nimic():
    """Edge 4 — 202 produse (7,3%) n-au nicio secțiune din care s-ar putea extrage ceva: pastă de
    dinți, pensule, creioane. Un LLM n-ar extrage nimic din ele fiindcă nu există text — de aia
    derivarea rămâne deterministă și tace, în loc să inventeze."""
    assert _hits("") == {}
    assert _hits("pensula profesionala numarul 12") == {}


# --- secțiunile negative -------------------------------------------------------------------------


def test_sectiunile_negative_nu_sunt_citite_deloc():
    """Edge 5 — `anti_fit` spune pentru cine NU e produsul. O potrivire acolo produce eticheta
    INVERSĂ: filtrul ar recomanda produsul greșit exact clientului care l-a exclus.

    Garanția e structurală, nu o convenție de scriere: secțiunea nu intră în niciuna dintre
    mulțimile citite. Testul e ieftin și prinde exact regresia care contează — cineva care adaugă
    „încă o secțiune utilă" fără să se uite ce spune ea."""
    from scripts.derive_product_attributes import FORMULA_SECTIONS, POSITIVE_SECTIONS

    assert "anti_fit" not in POSITIVE_SECTIONS
    assert "anti_fit" not in FORMULA_SECTIONS
    # și, pentru claritate: textul unei secțiuni negative, dacă AR fi citit, chiar ar produce
    # eticheta greșită — deci excluderea ei nu e pedanterie.
    assert "oily" in _hits("nu este potrivit pentru ten gras")


# --- excluderile -------------------------------------------------------------------------------


def test_excluderea_anuleaza_doar_potrivirea_din_interiorul_ei():
    """Failure 6, prima jumătate: o excludere taie potrivirea, nu produsul.

    Dacă excluderea s-ar aplica pe textul întreg, o singură mențiune nefericită ar șterge un produs
    chiar dedicat nevoii — „conține acizi grași" ar scoate o cremă pentru ten gras."""
    kw = {"excludes": {"oily": ["nu este pentru ten gras"]}}
    assert _hits("nu este pentru ten gras", **kw) == {}
    both = _hits("nu este pentru ten gras. dar merge pe ten gras combinat", **kw)
    assert "oily" in both and both["oily"].vetoed == 1


def test_stemul_din_pachet_e_marcat_ca_atare():
    """Un stem lărgește; fraza nu. Ce a intrat DOAR prin stem e setul de auditat cu prioritate."""
    hits = _hits("piele grasa fara alta mentiune", stems={"oily": ["grasa"]})
    assert hits["oily"].stem_only is True
    mixed = _hits("ten gras si piele grasa", stems={"oily": ["grasa"]})
    assert mixed["oily"].stem_only is False


def test_fara_stemuri_in_pachet_nu_exista_stemuri():
    """P9 — codul nu cunoaște niciun stem. Pachetul gol înseamnă potrivire doar pe fraze."""
    matchers = _matchers()
    assert all(m.stems == () for m in matchers.values())


# --- ingrediente -------------------------------------------------------------------------------


def test_capul_liniei_e_valoarea_nu_propozitia():
    """Edge — 10.392 de valori distincte nu e o fațetă, e text."""
    line = "ulei de macadamia - ulei bogat in acizi grasi ce reface lipidele cutanate"
    assert ingredient_head(line) == "ulei de macadamia"


def test_capul_trunchiat_de_separator_nu_devine_valoare():
    """Măsurat pe catalogul real: „extract de" ajunsese a cincea valoare canonică, cu 321 de
    produse. Nu e un ingredient, e o frază tăiată. Testul e al LIMBII (un nume nu se termină în
    prepoziție), iar lista de cuvinte funcționale vine din `catalog.query_terms`, pe locale."""
    ro = stopwords("ro")
    assert ingredient_head("extract de - ceva", function_words=ro) is None
    assert ingredient_head("acid hialuronic - hidratare", function_words=ro) == "acid hialuronic"


def test_o_propozitie_intreaga_nu_e_un_nume():
    long_line = "un complex avansat de peptide biomimetice si antioxidanti naturali"
    assert ingredient_head(long_line) is None


def test_vocabularul_canonic_e_plafonat():
    """DoD — sub 300 de valori distincte. Ce nu intră NU se aruncă: rămâne text în secțiune, unde
    era oricum util pentru căutare; doar că nu devine o valoare pe care nimeni n-o poate filtra."""
    vocab = IngredientVocabulary(limit=2)
    for head, times in (("acid hialuronic", 10), ("niacinamida", 5), ("ceva rar", 1)):
        for _ in range(times):
            vocab.observe(head)
    canonical = vocab.canonical()
    assert canonical == {"acid hialuronic", "niacinamida"}
    assert "ceva rar" not in canonical


# --- provenance + idempotență -------------------------------------------------------------------


def test_semnalul_poarta_fateta_si_valoarea():
    """Tabela are o singură coloană `signal`, iar cheia ei unică e ce face re-derivarea idempotentă:
    (business, produs, semnal, regulă, locale). Fără fațeta în nume, `oily` de la `skin_type` și
    `oily` de la `concerns` ar fi același rând."""
    assert signal_name("skin_type", "oily") == "skin_type:oily"
    assert signal_name("concerns", "oily") != signal_name("skin_type", "oily")


def test_derivarea_e_determinista():
    """A doua trecere trebuie să producă exact aceleași valori — altfel „zero scrieri la a doua
    rulare" ar fi noroc, nu idempotență."""
    text = "pentru tenul gras cu puncte negre"
    assert _hits(text) == _hits(text)


# --- poarta de precizie -------------------------------------------------------------------------

_FF = TypedFacet(
    key="fragrance_free",
    value_type=FacetType.BOOL,
    source=FacetSource.ATTRIBUTE,
    source_key="fragrance_free",
    operators=("eq",),
    provenance="claim",
    min_coverage=0.4,
)


def test_acoperirea_mare_nu_deschide_enforcement():
    """Failure 7 — o fațetă sub pragul de precizie NU primește `enforce_ready`, chiar la 100%
    acoperire. Cifra mare fără audit e exact greșeala pe care a mai făcut-o proiectul o dată:
    92% acoperire pe „Buze", cu valori corecte și complet irelevante."""
    prods = [{"price": 50, "category_slug": "s", "attributes": {"fragrance_free": True}}] * 4
    rows = compute_coverage([_FF], {"s": prods}, min_products=1)
    assert rows[0]["coverage"] == 1.0
    assert rows[0]["enforce_ready"] is False


def test_politica_de_precizie_e_preinregistrata_si_completa():
    """Pragurile se scriu ÎNAINTE de audit. Un prag scris după ce vezi rezultatul e o justificare,
    nu o poartă — de aia politica e un artefact separat, amprentat în raport."""
    policy = json.loads(POLICY.read_text(encoding="utf-8"))
    assert policy.get("version"), "politica trebuie să declare o versiune"
    for facet, spec in policy["facets"].items():
        assert 0 < spec["min_precision"] <= 1, facet
        assert spec["min_sample"] >= 1 and spec["sample_size"] >= spec["min_sample"], facet
        assert spec["promise"] in ("claim", "rank_signal"), facet


def test_promisiunile_au_prag_mai_inalt_decat_semnalele_de_rang():
    """Pragul urmează ce PROMITE fațeta. `fragrance_free` e o afirmație pe care clientul o
    cumpără; `concerns` e un semnal de ordonare. Confundându-le, ori blochezi rankingul degeaba,
    ori lași o minciună comercială să treacă."""
    policy = json.loads(POLICY.read_text(encoding="utf-8"))["facets"]
    claims = [s["min_precision"] for s in policy.values() if s["promise"] == "claim"]
    signals = [s["min_precision"] for s in policy.values() if s["promise"] == "rank_signal"]
    assert min(claims) > max(signals)


# --- ce DEȚINE o rulare de derivare (NX-268, ștergerea faptelor moarte) -------------------------


def test_fatetele_detinute_nu_depind_de_ce_a_produs_rularea():
    """Mulțimea se calculează din PACHET, nu din rezultat.

    Diferența e chiar defectul pe care îl repară: dacă „deținut" ar însemna „a produs valori acum",
    o fațetă care nu mai potrivește nimic pe tot catalogul ar ieși din mulțime exact în rularea care
    ar trebui s-o curețe, iar valorile ei vechi ar rămâne în `attributes` și în semnale la infinit.
    Aici, `finish` e declarată și deținută chiar dacă niciun produs n-o mai poartă."""
    owned = owned_facets(
        facet_values={"concerns": {"hydration"}, "skin_type": {"dry"}, "finish": set()},
        name_derived=["finish"],
        claim_derived=[],
    )
    assert "finish" in owned


def test_fatetele_altor_joburi_nu_se_ating():
    """`routine_time` vine din import, `shade`/`shade_group` din NX-269. Toate trei sunt scrise în
    ACEEAȘI coloană `attributes`, deci o ștergere prea largă ar șterge munca altui job fără nicio
    eroare — s-ar vedea abia ca un catalog mai sărac la următoarea rulare."""
    owned = owned_facets(
        facet_values={
            "concerns": {"hydration"},
            "routine_time": {"am", "pm"},
            "shade": set(),
        },
        name_derived=[],
        claim_derived=[],
    )
    assert "routine_time" not in owned
    assert "shade" not in owned and "shade_group" not in owned
    assert "concerns" in owned


def test_vocabular_gol_nu_e_o_instructiune_de_stergere():
    """Fără valori declarate, potrivirea n-are ce căuta, deci rularea NU deține fațeta. Altfel un
    pachet golit din greșeală ar șterge nevoile de pe tot catalogul la prima rulare — o pierdere
    tăcută de date pornită de la o editare de config."""
    owned = owned_facets(
        facet_values={"concerns": set(), "skin_type": set()},
        name_derived=[],
        claim_derived=[],
    )
    assert "concerns" not in owned and "skin_type" not in owned
    # `spf` și `key_ingredients` rămân: nu depind de vocabular, se derivă structural (regex, capul
    # liniei), deci rularea le încearcă mereu și le deține mereu.
    assert set(owned) == {"spf", "key_ingredients"}


def test_stergerea_are_acelasi_scop_ca_proiectia():
    """Semnalele și `attributes` trebuie curățate pe ACEEAȘI mulțime de fațete. Dacă cele două
    scopuri ar diverge, ștergerea n-ar închide divergența, ar muta-o dintr-o parte în alta — iar
    partea rămasă murdară ar fi tocmai copia pe care o citește read-path-ul."""
    source = (ROOT / "scripts" / "derive_product_attributes.py").read_text(encoding="utf-8")
    # Ambele statement-uri de curățare primesc `owned`, iar sweep-ul are scop POZITIV pe produse
    # (`= any`), nu negativ: derivarea vede doar produsele `active`, deci un `not in` ar șterge și
    # faptele celor arhivate, pe care rularea nici nu le-a examinat.
    assert "_DELETE_ORPHAN_SIGNALS, args.business, locale, owned, empty" in source
    assert "_STRIP_ATTRS, args.business, empty, owned" in source
    assert "and product_id = any($4::uuid[])" in source
