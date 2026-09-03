"""NX-269 — nuanța se descoperă comparativ, sau nu se scrie deloc.

Un sfert din catalog (681 de produse de machiaj) n-are axa pe care se ia decizia de cumpărare:
toate variantele poartă „Standard", `shade` și `color_hex` sunt goale, iar nuanța trăiește în
numele produsului. Testele urmăresc exact ce separă o derivare de o invenție — și fiecare caz din
card e marcat (happy 1-2, edge 3-5, failure 6-7)."""

from __future__ import annotations

import json
import pathlib

from src.catalog.shade import (
    MAX_SHADE_TOKENS,
    derive_shades,
    shade_appears_in_name,
    tokenize,
)

ROOT = pathlib.Path(__file__).resolve().parent.parent
POLICY = ROOT / "tests" / "derived_precision_policy.json"
PACK = ROOT / "db" / "seed" / "domain_pack_sole_ro.json"

# Aliasuri de unitate, ca cele din registrul NX-266 al tenantului.
UNITS = {"gr": "weight", "ml": "volume", "g": "weight"}


def _p(pid: str, name: str, brand: str = "LAKA", root: str = "machiaj") -> dict[str, str]:
    return {"id": pid, "name": name, "brand": brand, "root": root}


def _derive(products, roots=frozenset({"machiaj"})):
    return derive_shades(products, unit_aliases=UNITS, roots=roots)


# --- happy path ---------------------------------------------------------------------------------


def test_trei_nuante_din_aceeasi_linie():
    """Happy 1 — sufixe diferite pe același trunchi → trei nuanțe distincte, aceeași linie."""
    line = "LAKA Fruity Glam Tint nuantator pentru buze 4.5 gr"
    got = _derive(
        [
            _p("a", f"{line} 116 Candid"),
            _p("b", f"{line} 117 Harmony"),
            _p("c", f"{line} 118 Adore"),
        ]
    )
    assert {a.shade for a in got.values()} == {"116 Candid", "117 Harmony", "118 Adore"}
    assert len({a.group for a in got.values()}) == 1  # o singură linie
    assert {a.shade_code for a in got.values()} == {"116", "117", "118"}


def test_nuanta_din_mijlocul_numelui():
    """Nuanța nu e neapărat la coadă: „…Cushion, 22N Shell Beige, 18 gr - fond de ten…".

    Prima variantă a algoritmului căuta doar un sufix și prindea 20% din catalog exact din cauza
    formei ăsteia de nume."""
    got = _derive(
        [
            _p("a", "TIRTIR Mask Fit Cushion 22N Shell Beige 18 gr fond de ten cushion", "TIRTIR"),
            _p("b", "TIRTIR Mask Fit Cushion 21N Ivory 18 gr fond de ten cushion", "TIRTIR"),
        ]
    )
    assert got["a"].shade == "22N Shell Beige"
    assert got["b"].shade == "21N Ivory"


def test_proza_rescrisa_intre_nuante_nu_devine_nuanta():
    """Cazul care a spart două variante de algoritm: la unele branduri, fiecare nuanță are și
    descrierea ei rescrisă de mână. Diff-ul întoarce atunci mai multe blocuri, iar cel comercial e
    mai lung decât cel de nuanță — un „ia ce diferă" naiv ar fi scris proza în catalog."""
    got = _derive(
        [
            _p("a", "LAKA Glam Tint formulat cu apa care contribuie la hidratare 116 Candid"),
            _p("b", "LAKA Glam Tint formulat din sucuri care contribuie la stralucire 117 Harmony"),
        ]
    )
    assert got["a"].shade == "116 Candid"
    assert got["b"].shade == "117 Harmony"


# --- edge ---------------------------------------------------------------------------------------


def test_produs_unic_in_linia_lui_nu_primeste_nuanta():
    """Edge 3 — fără al doilea membru nu există axă de variație, deci nu se extrage nimic din
    coada numelui. Tăcerea e răspunsul corect, nu o ghicire."""
    got = _derive([_p("a", "LAKA Singular Product Unic 42 Ceva"), _p("b", "ALTCEVA Total Diferit")])
    assert got == {}


def test_gramajul_nu_e_o_nuanta():
    """Edge 4 — „4.5 gr" e o măsurătoare, iar unitățile vin din registrul NX-266 al tenantului:
    aceeași tabelă care spune ce e un mililitru la o constrângere de client spune și ce nu e o
    nuanță. Fără ea, ar fi trebuit o listă de unități în cod."""
    got = _derive(
        [
            _p("a", "LAKA Balsam Hidratant Intens 15 gr"),
            _p("b", "LAKA Balsam Hidratant Intens 30 gr"),
        ]
    )
    assert got == {}


def test_radacina_nedeclarata_nu_primeste_nuanta():
    """Edge 5 — un produs de îngrijire cu un cod în nume nu capătă nuanță pentru că rădăcina lui nu
    e declarată, NU pentru că am recunoscut noi că e o cremă. Distincția e tot ce face procedura
    generală: pe alt vertical, lista de rădăcini e alta și codul e neatins."""
    products = [
        _p("a", "PURITO Centella Serum 30 ml No 1 Original", root="ten"),
        _p("b", "PURITO Centella Serum 30 ml No 2 Intense", root="ten"),
    ]
    assert _derive(products, roots=frozenset({"machiaj"})) == {}
    assert _derive(products, roots=frozenset({"ten"}))  # aceeași procedură, altă rădăcină


def test_nuanta_prea_lunga_nu_e_o_nuanta():
    """Peste plafon nu mai e o nuanță, sunt două produse diferite care încep la fel."""
    tail = " ".join(f"Cuvant{i}" for i in range(MAX_SHADE_TOKENS + 3))
    got = _derive(
        [_p("a", f"LAKA Linia Comuna {tail}"), _p("b", "LAKA Linia Comuna Altceva Scurt")]
    )
    assert "a" not in got


# --- failure ------------------------------------------------------------------------------------


def test_branduri_diferite_nu_se_grupeaza():
    """Failure 6 — trunchi comun, branduri diferite: sunt două linii, nu una. O grupare peste
    branduri ar produce „nuanțe" care sunt de fapt alt produs."""
    got = _derive(
        [
            _p("a", "Pure Vitamin C Serum 116 Candid", brand="LAKA"),
            _p("b", "Pure Vitamin C Serum 117 Harmony", brand="TIRTIR"),
        ]
    )
    assert got == {}


def test_nuanta_apare_intotdeauna_literal_in_nume():
    """Failure 7 — invariantul care separă un fapt derivat de unul inventat.

    Nu e o convenție de scriere, e o consecință a construcției (nuanța e o felie din nume). Se
    testează fiindcă validatorul din aval (stagiul 8) verifică adevărul FAȚĂ DE catalog: un shade
    inventat scris în `attributes` s-ar confirma singur și ar ieși la client ca afirmație."""
    products = [
        _p("a", "LAKA Fruity Glam Tint 4.5 gr 116 Candid"),
        _p("b", "LAKA Fruity Glam Tint 4.5 gr 117 Harmony"),
        _p("c", "TIRTIR Mask Fit Cushion 22N Shell Beige 18 gr acoperire", "TIRTIR"),
        _p("d", "TIRTIR Mask Fit Cushion 21N Ivory 18 gr acoperire", "TIRTIR"),
    ]
    by_id = {p["id"]: p for p in products}
    for pid, a in _derive(products).items():
        assert shade_appears_in_name(a.shade, by_id[pid]["name"]), (pid, a.shade)


def test_invariantul_chiar_prinde_o_nuanta_inventata():
    """Garda trebuie să fie roșie când e cazul — altfel e decor."""
    assert not shade_appears_in_name("Coral Sunset", "LAKA Fruity Glam Tint 116 Candid")


# --- determinism + contract ---------------------------------------------------------------------


def test_derivarea_e_determinista():
    """A doua rulare trebuie să producă exact aceleași nuanțe și aceleași grupuri — altfel
    „idempotent" ar însemna doar că a doua scriere a avut noroc."""
    products = [
        _p("a", "LAKA Fruity Glam Tint 4.5 gr 116 Candid"),
        _p("b", "LAKA Fruity Glam Tint 4.5 gr 117 Harmony"),
    ]
    assert _derive(products) == _derive(products)


def test_tokenizarea_pastreaza_forma_originala():
    """Nuanța scrisă în catalog trebuie să fie literal cea din nume, deci tokenii nu se
    normalizează la extragere. Dacă s-ar normaliza, invariantul de mai sus ar deveni o aproximație
    și n-ar mai prinde nimic."""
    assert tokenize("116 Candid Rosé") == ["116", "Candid", "Rosé"]


def test_pachetul_declara_fatetele_de_nuanta_si_finish():
    """Fațetele trebuie să EXISTE ca fațete tipizate, altfel nu le vede nici `facet_coverage`, nici
    poarta de relevanță, nici comparația. `finish` declară `derived_from: name` (de unde s-a
    EXTRAS), nu `source: name` — `FacetSource` n-are valoarea aia, deci loader-ul ar fi respins
    fațeta fail-closed și nimeni n-ar fi observat."""
    facets = {
        f["key"]: f
        for f in json.loads(PACK.read_text(encoding="utf-8"))["facets"]
        if isinstance(f, dict) and f.get("key")
    }
    assert facets["shade"]["source"] == "attribute"
    assert facets["shade"]["binding"] == "partitioning"  # altă nuanță CONTRAZICE cererea
    assert facets["finish"]["source"] == "attribute"
    assert facets["finish"]["derived_from"] == "name"
    assert facets["finish"]["values"], "finish fără valori nu se derivă"


def test_finishul_nu_are_nicio_valoare_dominanta():
    """Regula NX-264: o valoare adevărată la tot raftul nu ajută pe nimeni să aleagă. Valorile de
    finish sunt măsurate pe rădăcina `machiaj` (681 de produse), fiecare între 2% și 10% — două
    candidate au fost respinse tocmai pentru că nu discriminau."""
    facets = {
        f["key"]: f
        for f in json.loads(PACK.read_text(encoding="utf-8"))["facets"]
        if isinstance(f, dict) and f.get("key")
    }
    values = facets["finish"]["values"]
    aliases = facets["finish"]["aliases"]
    assert len(values) >= 3, "o fațetă cu două valori nu discriminează nimic"
    assert set(aliases.values()) <= set(values), "alias către o valoare nedeclarată"


def test_nuanta_are_prag_de_promisiune_in_politica_de_precizie():
    """Nuanța e vizibilă pe card și se compară între magazine: o nuanță greșită e o eroare pe care
    clientul o vede imediat. Pragul ei trebuie să fie de PROMISIUNE, nu de semnal de rang."""
    policy = json.loads(POLICY.read_text(encoding="utf-8"))["facets"]
    assert policy["shade"]["promise"] == "claim"
    assert policy["shade"]["min_precision"] >= 0.95
