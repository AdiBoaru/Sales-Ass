"""NX-277 — `finish` avea doi proprietari, iar al doilea își pierdea munca la fiecare rulare.

Cauza s-a dovedit alta decât cele trei din card, și mai gravă. Cele două joburi citeau `aliases`
în direcții OPUSE:

  • contractul (`src/domain/facets.py:100`) e `{alias normalizat: valoare canonică}`;
  • `derive_shade_finish` îl respecta;
  • `derive_product_attributes` îl citea invers, ca `{valoare: [forme]}`.

Consecința nu era „câteva aliasuri ratate". `aliases.get("matte")` întorcea STRINGUL `"matte"`
(fiindcă „matte" chiar e o cheie, mapată la ea însăși), iar iterarea peste un string dă CARACTERE.
Se compilau tipare pentru litere izolate: `(?<!\\w)m(?!\\w)`, `(?<!\\w)a(?!\\w)`.

Măsurat pe catalogul SOLE: **654 din 807** de potriviri veneau dintr-o literă singură, iar 70 de
produse aveau deja în catalog un `finish` pe care numele nu-l conține — „SKINTEGRA Solar I SPF 30"
primea `satin` din „I". Fațeta hrănește poarta de relevanță (NX-257), deci „ruj mat" întorcea
produse etichetate mat din întâmplare.

ZERO DB: ambele funcții de derivare sunt pure pe `(spec, nume)`.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
from types import SimpleNamespace

import pytest

from src.catalog.shade import tokenize
from src.domain.normalize import normalize

_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, _ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # `@dataclass` are nevoie de modul în `sys.modules`
    spec.loader.exec_module(module)
    return module


dpa = _load("nx277_dpa", "scripts/derive_product_attributes.py")
dsf = _load("nx277_dsf", "scripts/derive_shade_finish.py")

#: Fațeta REALĂ din pachetul `sole-ro`: `aliases` e `{alias: canonic}`, cu valori care sunt și
#: chei (`"matte": "matte"`) — exact forma pe care citirea inversă o transforma în litere.
FINISH_SPEC = {
    "key": "finish",
    "derived_from": "name",
    "values": ["matte", "satin", "glossy", "glow", "velvet"],
    "aliases": {
        "mat": "matte",
        "matte": "matte",
        "matifiant": "matte",
        "matifianta": "matte",
        "dewy": "glow",
        "glow": "glow",
        "gloss": "glossy",
        "glossy": "glossy",
        "lucios": "glossy",
        "lucioasa": "glossy",
        "satin": "satin",
        "satinat": "satin",
        "velvet": "velvet",
        "catifelat": "velvet",
    },
}


def _step2(name: str) -> str | None:
    patterns = dpa._value_patterns(FINISH_SPEC)
    name_norm = normalize(name)
    for value, pats in patterns.items():
        if any(p.search(name_norm) for p in pats):
            return value
    return None


def _step3(name: str) -> str | None:
    forms = dsf._finish_values(SimpleNamespace(settings={"domain_pack": {"facets": [FINISH_SPEC]}}))
    words = set(tokenize(normalize(name)))
    for value, fs in forms.items():
        if any(f in words for f in fs):
            return value
    return None


# ── Cauza: aliasurile citite invers produceau tipare de o literă ────────────


def test_niciun_tipar_nu_e_o_litera_singura():
    """Testul care ar fi prins defectul la sursă.

    Un tipar de un caracter nu e o potrivire de fațetă, e o coincidență: pe un catalog real
    potrivește cam orice nume destul de lung."""
    for value, pats in dpa._value_patterns(FINISH_SPEC).items():
        for pattern in pats:
            term = pattern.pattern.removeprefix(r"(?<!\w)").removesuffix(r"(?!\w)")
            assert len(term.replace("\\", "")) > 1, f"{value}: tipar de o literă ({term!r})"


@pytest.mark.parametrize(
    "name",
    [
        "SKINTEGRA Solar I SPF 30, 50 ml - Crema de fata",  # „I" izolat → dădea `satin`
        "B.fresh Cutie With a Booty 250 ml - unt de corp",  # „a" izolat → dădea `matte`
        "ALLIES OF SKIN Multi Acids and Retinoid Serum",
    ],
)
def test_o_litera_izolata_nu_mai_produce_finish(name):
    """Cele trei sunt nume REALE din catalog care primeau un finish inventat."""
    assert _step2(name) is None


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Ruj mat, 3 g", "matte"),
        ("Fond de ten matifiant SPF 20", "matte"),
        ("Gloss de buze lucios", "glossy"),
        ("Crema cu efect catifelat", "velvet"),
        ("Iluminator dewy 10 ml", "glow"),
    ],
)
def test_aliasurile_reale_chiar_se_potrivesc_acum(name, expected):
    """Partea a doua a aceleiași greșeli: citite invers, aliasurile REALE („mat", „lucios",
    „catifelat") nu se potriveau deloc. Ele erau cele 118 pe care doar pasul 3 le găsea."""
    assert _step2(name) == expected


# ── Consecința: un singur proprietar, fără invariant de ordine ──────────────


@pytest.mark.parametrize(
    "name",
    [
        "Ruj mat, 3 g",
        "Fond de ten matifiant SPF 20",
        "Gloss de buze lucios",
        "Crema cu efect catifelat",
        "Iluminator dewy 10 ml",
        "SKINTEGRA Solar I SPF 30, 50 ml - Crema de fata",
        "Sampon fara sulfati 250 ml",
    ],
)
def test_cei_doi_pasi_sunt_de_acord_dupa_reparatie(name):
    """De ce se poate declara UN proprietar: după fix, pasul 2 derivă exact ce derivă pasul 3.

    Măsurat pe catalogul real: 301 produse fiecare, zero găsite doar de pasul 3, zero dezacorduri.
    Fără egalitatea asta, scoaterea lui `finish` din pasul 3 ar fi PIERDUT semnale — motiv pentru
    care cardul cerea cauza înainte de reparație."""
    assert _step2(name) == _step3(name)


def test_pasul_3_nu_mai_detine_finish():
    """DoD 2: `finish` are UN singur proprietar declarat.

    `_SHADE_KEYS` e lista pe care `_PROJECT_ATTRS` o ȘTERGE când nu apare în `attrs`. Cât timp
    `finish` era acolo, fiecare rulare a pasului 3 rescria o fațetă a altui job — și invers,
    fiecare rulare a pasului 2 ștergea ce scrisese pasul 3."""
    assert "finish" not in dsf._SHADE_KEYS
    assert set(dsf._SHADE_KEYS) == {"shade", "shade_code", "shade_group"}


async def test_o_reintroducere_a_proprietarului_al_doilea_opreste_rularea():
    """DoD 5: invariantul e VERIFICAT, nu documentat. Un invariant pe care îl știe doar autorul nu
    e un invariant — ăsta a supraviețuit nedeclarat până când cineva a re-rulat pasul 2 și a citit
    cifra de semnale șterse."""

    class _Conn:
        def transaction(self):
            class _Tx:
                async def __aenter__(self_inner):
                    return None

                async def __aexit__(self_inner, *exc):
                    return False

            return _Tx()

    with pytest.raises(AssertionError, match="NX-277"):
        await dsf._write_batch(_Conn(), "biz", "ro", [("p1", {"finish": "matte"})])
