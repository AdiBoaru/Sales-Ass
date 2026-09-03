"""Regresii pe vocabularul de domeniu al lui `sole-ro` (`db/seed/domain_pack_sole_ro.json`).

Toate PURE: încarcă seed-ul prin loader-ul REAL, fără DB și fără rețea, deci rulează în CI.
Confruntarea cu catalogul (câte produse servesc fiecare nevoie) e treaba lui
`scripts/set_domain_pack.py --check`, care are nevoie de baza live — aici verificăm invarianții
care nu depind de date și care, dacă se rup, se rup TĂCUT în producție.
"""

from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

from src.agent.voice import naturalize
from src.domain.loader import load_domain_pack

PACK_PATH = Path(__file__).resolve().parent.parent / "db" / "seed" / "domain_pack_sole_ro.json"

# Verticalul REAL al tenantului. Pachetul se deep-merge-uiește peste `defaults/ecommerce.json`,
# deci un test care l-ar încărca pe „beauty_salon" ar testa alt merge decât cel din producție.
VERTICAL = "ecommerce"


@pytest.fixture(scope="module")
def raw() -> dict:
    return json.loads(PACK_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def pack(raw: dict):
    business = types.SimpleNamespace(vertical=VERTICAL, settings={"domain_pack": raw})
    return load_domain_pack(business)


def test_loaderul_nu_arunca_nimic_din_pachet(raw: dict, pack) -> None:
    """Ce am DECLARAT e și ce s-a ÎNCĂRCAT.

    Loader-ul respinge fail-closed o fațetă sau un tip de relație invalid și doar LOGHEAZĂ —
    comportament corect (un pachet stricat nu trebuie să crape boot-ul), dar înseamnă că
    diferența dintre „am scris" și „rulează" e invizibilă în producție. Testul o face vizibilă.
    Exact așa a fost prinsă prima versiune a acestui pachet: `substitute` declara
    `purpose: "recovery"`, o valoare care nu există în `RelationPurpose`, deci muchia de
    substitut — singura care poate salva un produs epuizat — cădea tăcut.
    """
    declared_facets = {f["key"] for f in raw["facets"]}
    assert {f.key for f in pack.facets} == declared_facets

    # Cheile care încep cu `_` sunt NOTE, convenția întregului pachet („de ce e goală lista asta",
    # „de unde vin cifrele"). Loader-ul le sare deliberat; a le cere aici ar transforma o notă
    # într-un tip de muchie fantomă.
    declared_kinds = {k for k in raw["relation_kinds"] if not k.startswith("_")}
    assert set(pack.relation_kinds.specs) == declared_kinds


def test_fiecare_nevoie_are_o_tinta_care_exista_in_fatete(raw: dict, pack) -> None:
    """O cheie canonică din `concern_map` trebuie să fie o valoare DECLARATĂ a unei fațete.

    Altfel maparea e nerezolvabilă pentru totdeauna: fraza clientului se traduce într-o cheie pe
    care nicio fațetă n-o cunoaște, iar rezultatul e un `UNKNOWN` care arată la fel ca un cuvânt
    nemaiauzit. Tipul de ten stă în `skin_type` (fațetă `partitioning` — cumpărătorul are exact
    unul), obiectivele în `concerns` (`additive` — un produs care nu le țintește nu contrazice
    nimic), și tocmai de-aia sunt două fațete, nu o listă amestecată.
    """
    allowed: set[str] = set()
    for facet in raw["facets"]:
        if facet["key"] in ("skin_type", "concerns"):
            allowed |= set(facet.get("values") or [])

    orphans = sorted(set(pack.concern_map.values()) - allowed)
    assert not orphans, f"chei canonice fără fațetă care să le poarte: {orphans}"


def test_dovada_de_acoperire_nu_poate_drifta_de_vocabular(raw: dict, pack) -> None:
    """`_coverage` e proveniența pachetului: câte produse servesc fiecare nevoie. Dacă cineva
    adaugă o nevoie și uită dovada, pachetul începe să afirme lucruri nemăsurate."""
    measured = set(raw["_coverage"]) - {"_note"}
    assert measured == set(pack.concern_map.values())


def test_expandarile_raman_goale_pana_cand_scara_lexicala_se_repara(raw: dict) -> None:
    """Pin pe o DECIZIE măsurată, nu pe o preferință.

    Un set de 27 de expandări a fost construit și rulat pe catalogul live: niciuna n-a
    îmbunătățit vreo interogare. `query_rewrite` le concatenează în `search_text`, iar
    `search_products_lexical` le trece prin `content_terms` și leagă TOT cu ȘI pe treapta
    `strict`, deci o expandare poate doar să îngusteze: «am cearcane» 18 → 1, «pete pigmentare pe
    fata» 41 → 3, «bariera afectata» 29 → 1. Iar ce părea câștig («crema pentru riduri» 20 → 50)
    era o CĂDERE de pe `strict` pe `relaxed`, adică 50 de rezultate vagi.

    Repararea e în scara lexicală (expandarea largește ramura SAU și ranking-ul, niciodată
    conjuncția strictă), cu măsurătoarea ei. Până atunci orice intrare aici degradează
    retrievalul, așa că testul cere și explicația — o valoare goală fără motiv se reumple.
    """
    assert raw["query_expansions"] == {}
    note = raw["_query_expansions_note"]
    assert "strict" in note and "relaxed" in note


def test_textul_vizibil_clientului_respecta_vocea(raw: dict) -> None:
    """Principiul 13: în textul către client nu există liniuță de pauză și nici punct și virgulă.

    `response_style` intră în prompturile de compunere, iar etichetele de fațetă ajung pe
    ecranul clientului în tabelul de comparație. Un exemplu cu semnul interzis ÎN prompt îl
    învață pe model exact ce îi interzici — așa a picat prima încercare de a impune regula.
    """
    visible: list[str] = list(raw["response_style"].values())
    for facet in raw["facets"]:
        visible += list((facet.get("labels") or {}).values())
    for spec in raw["comparison_facets"]:
        visible += list((spec.get("labels") or {}).values())
        for per_locale in (spec.get("value_labels") or {}).values():
            visible += list(per_locale.values())

    for text in visible:
        assert ";" not in text, f"punct și virgulă în text vizibil: {text!r}"
        assert naturalize(text) == text, f"liniuță de pauză în text vizibil: {text!r}"


def test_memoria_tenantului_nu_poate_tine_pii(pack) -> None:
    """P12: PII-ul trăiește DOAR în `channel_identities`. Nicio cheie de profil sau tip de fapt
    nu are voie să deschidă o a doua casă pentru el."""
    forbidden = {"telefon", "phone", "email", "nume", "name", "adresa", "address", "cnp", "iban"}
    assert not (forbidden & set(pack.profile_whitelist))
    assert not (forbidden & set(pack.fact_type_whitelist))


def test_doar_tipul_de_ten_poate_exclude(raw: dict) -> None:
    """NX-257: `partitioning` e singurul binding care poate EXCLUDE produse. Pe fațetele de
    ATRIBUT ale acestui pachet, singura care îl merită e tipul de ten: cumpărătorul are exact
    unul, deci un produs cu altă valoare îl contrazice. Un obiectiv (riduri, luminozitate) nu
    contrazice nimic — un produs care nu-l țintește doar nu-l țintește, deci `additive`.

    `fragrance_free` e tot partiționantă, dar din alt motiv: e o cerință binară a clientului.
    `shade` (NX-269) e a treia, și își merită locul dintr-un al treilea motiv: cumpărătorul cere o
    nuanță ANUME („116 Candid"), iar alta nu e o potrivire mai slabă, e produsul greșit — la fel de
    greșit ca un ten uscat servit cuiva cu ten gras.

    Testul le ENUMERĂ ca să nu apară a patra prin distragere: fiecare intrare aici e o fațetă care
    capătă dreptul de a șterge produse din rezultate, iar dreptul ăla se dă cu motivul scris.
    """
    partitioning = {
        f["key"]
        for f in raw["facets"]
        if f.get("binding") == "partitioning" and f["source"] == "attribute"
    }
    assert partitioning == {"skin_type", "fragrance_free", "shade"}
