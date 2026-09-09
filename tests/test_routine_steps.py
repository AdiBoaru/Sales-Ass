"""NX-280 — pașii de rutină: harta din pachet, promovarea mărginită, poarta de secvență.

Testele sunt PURE (zero DB, zero LLM): modulul e o funcție de la config + atribute la o valoare.
Cazurile nu sunt inventate — fiecare corespunde unei constatări măsurate pe catalogul SOLE, notate
în `tasks/stage1/NX-280.md`.
"""

from __future__ import annotations

import pytest

from src.domain.routine_steps import (
    EMPTY_ROUTINE_STEPS,
    RoutineStepConfigError,
    build_spec,
    distinct_steps,
    load_routine_steps,
    resolve,
)

RAW = {
    "families": {
        "fata": ["curatare", "tonifiere", "tratament", "hidratare", "protectie"],
        "par": ["spalare", "conditionare"],
        "corp": ["curatare", "hidratare"],
        "machiaj": ["ten", "buze"],
    },
    "by_product_type": {
        "gel de curatare": "fata:curatare",
        "toner de fata": "fata:tonifiere",
        "ser de fata": "fata:tratament",
        "crema de fata": "fata:hidratare",
        "sampon": "par:spalare",
        "balsam": "par:conditionare",
        "lotiune de corp": "corp:hidratare",
        "gel de dus": "corp:curatare",
        "fond de ten": "machiaj:ten",
        "ruj": "machiaj:buze",
    },
    "promotions": [{"when_attribute": "spf", "within_family": "fata", "to_step": "protectie"}],
    "not_a_step": ["set", "pasta de dinti"],
}


@pytest.fixture
def spec():
    return build_spec(RAW)


# --- maparea de bază ---------------------------------------------------------------------


def test_tipul_da_pasul(spec):
    assert resolve({"product_type": "gel de curatare"}, spec) == "fata:curatare"
    assert resolve({"product_type": "sampon"}, spec) == "par:spalare"


def test_cheia_e_compusa_deci_samponul_nu_e_in_rutina_fetei(spec):
    """Invariantul central: „sampon" și „gel de curatare" sunt amândouă curățare, dar familia le
    ține separate. Fără cheia compusă, un filtru pe „curatare" ar amesteca rutinele."""
    assert resolve({"product_type": "sampon"}, spec) == "par:spalare"
    assert spec.family_of(resolve({"product_type": "sampon"}, spec)) == "par"
    assert spec.family_of(resolve({"product_type": "gel de curatare"}, spec)) == "fata"


def test_ordinea_vine_din_indexul_familiei(spec):
    assert spec.order_of("fata:curatare") == 1
    assert spec.order_of("fata:protectie") == 5
    assert spec.order_of("par:spalare") == 1
    assert spec.order_of("fata:inexistent") is None
    assert spec.order_of("inexistent:curatare") is None
    assert spec.order_of("fara-separator") is None


# --- promovarea pe atribut ---------------------------------------------------------------


def test_spf_promoveaza_in_familia_fetei(spec):
    """O cremă de față cu SPF 50 e pasul de protecție, nu cel de hidratare."""
    assert resolve({"product_type": "crema de fata", "spf": "50"}, spec) == "fata:protectie"


def test_spf_NU_scoate_produsul_din_familia_lui(spec):
    """Regula naivă («spf ⇒ protecție, indiferent de tip») ar muta 48 de produse din familia lor pe
    catalogul SOLE: 35 de bază de machiaj, 9 de corp, 4 balsamuri de buze. O loțiune de corp cu SPF
    nu e un pas din rutina feței, iar un fond de ten cu SPF 30 rămâne bază de machiaj."""
    assert resolve({"product_type": "lotiune de corp", "spf": "30"}, spec) == "corp:hidratare"
    assert resolve({"product_type": "fond de ten", "spf": "30"}, spec) == "machiaj:ten"
    assert resolve({"product_type": "ruj", "spf": "15"}, spec) == "machiaj:buze"


def test_spf_gol_sau_fals_nu_promoveaza(spec):
    for value in (None, "", False):
        assert resolve({"product_type": "crema de fata", "spf": value}, spec) == "fata:hidratare"


# --- ce NU face: nu ghicește -------------------------------------------------------------


def test_fara_product_type_nu_exista_pas_nici_cu_spf(spec):
    """Cele 24 de produse SOLE cu `spf` și fără tip. Familia ar fi o ghicitoare, iar pasul ar
    ajunge în catalog ca fapt."""
    assert resolve({"spf": "50"}, spec) is None
    assert resolve({}, spec) is None
    assert resolve(None, spec) is None


def test_not_a_step_e_distinct_de_nemapat(spec):
    """Un «set» e un ambalaj, nu un pas — declarat explicit, ca să se distingă de o scăpare."""
    assert resolve({"product_type": "set"}, spec) is None
    assert resolve({"product_type": "tip inexistent"}, spec) is None


# --- validarea configului: fail-closed la SCRIERE ----------------------------------------


@pytest.mark.parametrize(
    "mutate, fragment",
    [
        (lambda r: r.pop("families"), "families"),
        (lambda r: r.update(families={}), "families"),
        (lambda r: r.update(families={"fata": []}), "n-are pași"),
        (lambda r: r.update(families={"fa:ta": ["x"]}), "familie invalid"),
        (lambda r: r.update(families={"fata": ["a", "a"]}), "duplicați"),
        (lambda r: r.update(by_product_type={"x": "fata:inexistent"}), "nu e un pas declarat"),
        (lambda r: r.pop("by_product_type"), "by_product_type"),
        (
            lambda r: r.update(
                promotions=[{"when_attribute": "spf", "within_family": "zzz", "to_step": "x"}]
            ),
            "familia necunoscută",
        ),
        (
            lambda r: r.update(
                promotions=[{"when_attribute": "spf", "within_family": "par", "to_step": "zzz"}]
            ),
            "absent din",
        ),
        (lambda r: r.update(not_a_step=["ruj"]), "și mapate, și declarate"),
    ],
)
def test_config_invalid_e_respins(mutate, fragment):
    raw = {k: (v.copy() if hasattr(v, "copy") else v) for k, v in RAW.items()}
    mutate(raw)
    with pytest.raises(RoutineStepConfigError) as e:
        build_spec(raw)
    assert fragment in str(e.value)


def test_un_pas_declarat_fara_tipuri_e_legal(spec):
    """`fata:protectie` n-are niciun `product_type`: se ajunge la el DOAR prin promovare, fiindcă
    nu există `product_type = "protectie solara"` în catalog."""
    assert "fata:protectie" in spec.canonical_values()
    assert "fata:protectie" not in spec.by_product_type.values()


# --- citirea de runtime: tolerantă -------------------------------------------------------


def test_load_tolerant_nu_darama_pachetul():
    """Asimetria față de `build_spec`: la CITIRE o hartă stricată costă o capabilitate, nu tot
    pachetul. Un `raise` aici ar lăsa tenantul fără `concern_map` pentru o virgulă greșită."""
    assert load_routine_steps({"families": "gunoi"}) is EMPTY_ROUTINE_STEPS
    assert load_routine_steps(None) is EMPTY_ROUTINE_STEPS
    assert load_routine_steps({}) is EMPTY_ROUTINE_STEPS
    assert load_routine_steps(RAW).families == build_spec(RAW).families


def test_spec_gol_nu_da_niciun_pas():
    """Comportamentul de dinaintea NX-280, byte-identic."""
    assert resolve({"product_type": "gel de curatare"}, EMPTY_ROUTINE_STEPS) is None


# --- poarta de secvență (ce consumă `answer_plan`) ---------------------------------------


def test_doi_pasi_distincti_din_aceeasi_familie_sunt_o_secventa(spec):
    got = distinct_steps(["fata:curatare", "fata:hidratare"], spec)
    assert got == {"fata": {"curatare", "hidratare"}}
    assert max(len(s) for s in got.values()) >= 2


def test_doua_produse_din_ACELASI_pas_nu_sunt_o_rutina(spec):
    """Două creme nu sunt o rutină. Pragul pe cardinalitate (≥2 produse) le-ar accepta."""
    got = distinct_steps(["fata:hidratare", "fata:hidratare"], spec)
    assert max(len(s) for s in got.values()) == 1


def test_familii_amestecate_nu_sunt_o_rutina(spec):
    """Un șampon și un ser de față nu sunt pași unul după altul."""
    got = distinct_steps(["par:spalare", "fata:tratament"], spec)
    assert got == {"par": {"spalare"}, "fata": {"tratament"}}
    assert max(len(s) for s in got.values()) == 1


def test_valorile_necunoscute_sunt_ignorate_nu_numarate(spec):
    assert distinct_steps(["fata:inexistent", "gunoi", "", "fata:curatare"], spec) == {
        "fata": {"curatare"}
    }


def test_rutina_de_machiaj_cere_pasi_distincti(spec):
    """Cu harta veche (2 pași: bază + culoare), «fond de ten + ruj» trecea drept rutină. Cu 5 pași
    tot trece, dar cere pași chiar distincți — iar testul fixează intenția."""
    got = distinct_steps(["machiaj:ten", "machiaj:buze"], spec)
    assert max(len(s) for s in got.values()) == 2
