"""NX-292 felia 1 — compunerea rutinei: așezare, precedență, goluri declarate.

Pure (zero DB, zero LLM): modulul e o funcție de la (pachet, candidați) la sloturi. Cazurile nu
sunt inventate — fiecare pinuiește o decizie din `docs/NX-292-ROUTINE-PLAN.md`, iar cele mai multe
apără un eșec pe care nimic din aval nu l-ar prinde (validatorul și `grounding_guard` sunt porți de
ADEVĂR, nu de POTRIVIRE).
"""

from __future__ import annotations

import pytest

from src.catalog.routine_compose import (
    MIN_SLOTS_FOR_ROUTINE,
    ORIGINS,
    UNCOVERED_REASONS,
    UnknownFamilyError,
    compose,
)
from src.domain.routine_steps import build_spec

RAW = {
    "families": {
        "fata": ["curatare", "tonifiere", "tratament", "hidratare", "protectie"],
        "par": ["spalare", "conditionare"],
    },
    "by_product_type": {
        "gel de curatare": "fata:curatare",
        "toner de fata": "fata:tonifiere",
        "ser de fata": "fata:tratament",
        "crema de fata": "fata:hidratare",
        "sampon": "par:spalare",
    },
}


@pytest.fixture
def spec():
    return build_spec(RAW)


def _full_candidates() -> dict[str, list[str]]:
    return {
        "curatare": ["gel-1", "gel-2"],
        "tonifiere": ["toner-1"],
        "tratament": ["ser-1", "ser-2"],
        "hidratare": ["crema-1"],
        "protectie": ["spf-1"],
    }


# ── Ordinea și forma ────────────────────────────────────────────────────────────────────────────


def test_ordinea_pasilor_e_a_pachetului_nu_a_candidatilor(spec):
    """Ordinea vine din `families`, nu din ordinea cheilor primite. Un dict cu altă ordine de
    inserție nu are voie să schimbe rutina: altfel două apeluri echivalente ar da sfaturi
    diferite."""
    shuffled = dict(reversed(list(_full_candidates().items())))
    plan = compose("fata", spec, candidates=shuffled)

    assert [s.step for s in plan.slots] == RAW["families"]["fata"]
    assert [s.position for s in plan.slots] == [1, 2, 3, 4, 5]


def test_rutina_completa(spec):
    plan = compose("fata", spec, candidates=_full_candidates())

    assert plan.is_complete
    assert plan.is_routine
    assert plan.product_ids() == ("gel-1", "toner-1", "ser-1", "crema-1", "spf-1")
    assert {s.origin for s in plan.slots} == {"facet"}


def test_familia_necunoscuta_e_fail_closed(spec):
    with pytest.raises(UnknownFamilyError):
        compose("unghii", spec, candidates={})


# ── Golurile: declarate, cu motiv, niciodată sărite ─────────────────────────────────────────────


def test_pasul_fara_candidat_ramane_slot_declarat(spec):
    """Un pas fără candidat NU scurtează rutina. Dacă ar dispărea, clientul ar primi o secvență
    care pare completă, iar noi n-am mai putea spune ce lipsește."""
    candidates = _full_candidates()
    del candidates["tonifiere"]

    plan = compose("fata", spec, candidates=candidates)

    assert len(plan.slots) == 5  # tot 5 pași, nu 4
    (gap,) = plan.uncovered_slots
    assert (gap.position, gap.step) == (2, "tonifiere")
    assert gap.uncovered_reason == "no_candidate"
    assert gap.origin is None
    assert not plan.is_complete
    assert plan.is_routine  # 4 pași acoperiți: se poate prezenta onest


def test_motivul_apelantului_bate_no_candidate(spec):
    """`no_candidate` înseamnă „nu știm de ce e gol". Când apelantul ȘTIE (el a filtrat pe buget,
    stoc, siguranță), motivul lui trebuie păstrat: altfel raportul confundă „n-am găsit" cu
    „n-am căutat"."""
    candidates = _full_candidates()
    candidates["protectie"] = []

    plan = compose("fata", spec, candidates=candidates, reasons={"protectie": "budget"})

    assert plan.slots[4].uncovered_reason == "budget"


def test_motiv_din_afara_vocabularului_e_respins(spec):
    with pytest.raises(ValueError, match="motive necunoscute"):
        compose("fata", spec, candidates=_full_candidates(), reasons={"protectie": "prea scump"})


def test_toate_motivele_declarate_sunt_acceptate(spec):
    """Garda care ține vocabularul și validarea în sincron: un motiv adăugat în `UNCOVERED_REASONS`
    dar respins de `compose` ar fi o listă care minte."""
    for reason in UNCOVERED_REASONS:
        plan = compose(
            "par", spec, candidates={"spalare": ["s-1"]}, reasons={"conditionare": reason}
        )
        assert plan.slots[1].uncovered_reason == reason


# ── Precedența: pin > graf > fațetă ─────────────────────────────────────────────────────────────


def test_seedul_de_graf_bate_fateta(spec):
    plan = compose("fata", spec, candidates=_full_candidates(), seed=[("graph-toner", "tonifiere")])

    assert plan.slots[1].product_id == "graph-toner"
    assert plan.slots[1].origin == "graph"
    assert plan.slots[0].origin == "facet"  # restul rămâne pe inventar


def test_slotul_pinuit_nu_se_misca_nici_pentru_graf(spec):
    """Alegerea clientului bate forma dedusă din conținut. Fără regula asta, un al doilea «ceva mai
    ieftin» pe alt pas ar putea re-alege tăcut primul."""
    plan = compose(
        "fata",
        spec,
        candidates=_full_candidates(),
        seed=[("graph-toner", "tonifiere")],
        pinned={2: "alesul-clientului"},
    )

    assert plan.slots[1].product_id == "alesul-clientului"
    assert plan.slots[1].origin == "pinned"
    assert "graph-toner" in plan.dropped_seeds


def test_pozitie_pinuita_in_afara_familiei_e_respinsa(spec):
    with pytest.raises(ValueError, match="poziții pinuite"):
        compose("par", spec, candidates={}, pinned={7: "x"})


def test_primul_seed_pe_un_pas_castiga(spec):
    """Ordinea lanțului de graf se respectă fără ca modulul să știe ce e un lanț: primul seed
    pentru un pas ocupă slotul, al doilea e RAPORTAT, nu înghițit."""
    plan = compose(
        "fata",
        spec,
        candidates={},
        seed=[("primul", "curatare"), ("al-doilea", "curatare")],
    )

    assert plan.slots[0].product_id == "primul"
    assert plan.dropped_seeds == ("al-doilea",)


def test_seed_dintr_o_alta_familie_e_raportat(spec):
    """Un lanț care nu se potrivește familiei cerute e o informație despre datele noastre, nu un
    detaliu de implementare."""
    plan = compose("fata", spec, candidates={}, seed=[("sampon-1", "spalare")])

    assert plan.dropped_seeds == ("sampon-1",)
    assert all(not s.covered for s in plan.slots)


# ── Un produs, un slot ──────────────────────────────────────────────────────────────────────────


def test_acelasi_produs_nu_ocupa_doua_sloturi(spec):
    """«Pune crema, apoi crema» e un sfat absurd pe care validatorul l-ar lăsa să treacă: produsul
    e real, prețul e real. Garda e aici fiindcă în aval nu există niciuna."""
    plan = compose(
        "fata",
        spec,
        candidates={"curatare": ["dublu"], "tonifiere": ["dublu", "toner-2"]},
    )

    assert plan.slots[0].product_id == "dublu"
    assert plan.slots[1].product_id == "toner-2"


def test_candidat_deja_ocupat_da_all_taken_nu_no_candidate(spec):
    """`all_taken` spune ceva despre COMPUNERE (candidatul exista, dar ocupa alt pas), nu despre
    catalog. Confundarea lor ar trimite pe cineva să repare catalogul degeaba."""
    plan = compose("fata", spec, candidates={"curatare": ["unic"], "tonifiere": ["unic"]})

    assert plan.slots[1].uncovered_reason == "all_taken"
    assert plan.slots[0].uncovered_reason is None


def test_seedul_nu_se_repeta_din_fateta(spec):
    candidates = _full_candidates()
    candidates["tratament"] = ["graph-toner", "ser-2"]

    plan = compose("fata", spec, candidates=candidates, seed=[("graph-toner", "tonifiere")])

    assert plan.slots[1].product_id == "graph-toner"
    assert plan.slots[2].product_id == "ser-2"


# ── Pragul de secvență ──────────────────────────────────────────────────────────────────────────


def test_un_singur_pas_acoperit_nu_e_rutina(spec):
    """Pragul e ACELAȘI pe care îl impune `_routine_sequence_ok` (NX-280) în aval. E declarat aici
    ca apelantul să degradeze ONEST înainte de a promite o rutină, nu ca să se dubleze poarta."""
    plan = compose("fata", spec, candidates={"curatare": ["gel-1"]})

    assert len(plan.covered_slots) == 1
    assert not plan.is_routine
    assert not plan.is_complete


def test_pragul_e_exact_min_slots(spec):
    plan = compose("fata", spec, candidates={"curatare": ["a"], "hidratare": ["b"]})

    assert len(plan.covered_slots) == MIN_SLOTS_FOR_ROUTINE
    assert plan.is_routine


# ── Puritate ────────────────────────────────────────────────────────────────────────────────────


def test_doua_apeluri_dau_acelasi_rezultat(spec):
    """Puritatea nu e un detaliu estetic: pe ea se sprijină faptul că un raport de acoperire rulat
    de două ori pe același catalog nu poate să difere."""
    args = {"candidates": _full_candidates(), "seed": [("g", "tonifiere")], "pinned": {1: "p"}}

    assert compose("fata", spec, **args) == compose("fata", spec, **args)


def test_originile_sunt_din_vocabular(spec):
    plan = compose(
        "fata", spec, candidates=_full_candidates(), seed=[("g", "tonifiere")], pinned={1: "p"}
    )

    assert {s.origin for s in plan.covered_slots} <= ORIGINS


def test_intrarile_nu_se_modifica(spec):
    """Apelantul își păstrează listele. Un modul pur care consumă distructiv ar face ca a doua
    compunere din același tur să dea alt rezultat."""
    candidates = _full_candidates()
    snapshot = {k: list(v) for k, v in candidates.items()}
    pinned = {1: "p"}

    compose("fata", spec, candidates=candidates, pinned=pinned)

    assert {k: list(v) for k, v in candidates.items()} == snapshot
    assert pinned == {1: "p"}
