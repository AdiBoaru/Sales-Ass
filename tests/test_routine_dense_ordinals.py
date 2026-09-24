"""NX-323 — rutina are O numerotare, cea pe care o vede clientul: 1..N pașii arătați.

Conversația reală `bc7a356e`, turul «fa mi o rutina»: pașii acoperiți erau pe pozițiile de șablon
{1, 4, 5, 6} (curățare, tratament, hidratare, protecție), modelul a numerotat 1-2-3, iar poarta de
cifre accepta doar {1, 4, 5, 6}. «2.» și «3.» au căzut, intro-ul a ieșit gol, iar clientul a primit
șablonul „Ți-am ales ulei de curatare, masca de fata și crema de fata."

Zero DB, zero model.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from src.agent import reference_resolver as rr
from src.catalog.routine_compose import compose as compose_plan
from src.config import get_settings
from src.domain.routine_steps import build_spec
from src.tools import routine_tools as rt
from src.worker import compose
from tests.test_compose import _ctx, _settings

#: Pașii acoperiți ai turului real, cu pozițiile lor din șablonul `fata` al SOLE.
_REAL_STEPS = (
    ("A", 1, "curatare"),
    ("I", 4, "tratament"),
    ("S", 5, "hidratare"),
    ("U", 6, "protectie"),
)
_RETRIEVED = [
    {"id": pid, "name": f"Produs {pid}", "price": 30.0, "rating": 4.9, "availability": "in_stock"}
    for pid, _, _ in _REAL_STEPS
]
#: Intro-ul REAL scris de model (`conversation_traces.diagnostics.rich_raw`), fără propoziția cu
#: bugetul inventat (aceea e a NX-321).
_REAL_INTRO = (
    "1. ANUA curăță sebumul și machiajul, fără să usuce.\n"
    "2. SOME BY MI hidratează și susține vitalitatea pielii.\n"
    "3. IUNIK încheie rutina de dimineață cu protecție solară și calmare."
)


def _view(steps=_REAL_STEPS):
    return rt.RoutineView(
        family="fata",
        steps=tuple(
            rt.RoutineStepRef(position=pos, step=step, label=step.capitalize(), product_id=pid)
            for pid, pos, step in steps
        ),
    )


def _j(intro: str):
    return {
        "intro": intro,
        "items": [{"product_id": pid, "fit_clause": "potrivit"} for pid, _, _ in _REAL_STEPS],
        "education": "",
        "suggestions": [],
    }


@pytest.fixture(autouse=True)
def _dense_on(monkeypatch):
    monkeypatch.setattr(get_settings(), "routine_dense_ordinals_enabled", True)


# ── Ordinalele ────────────────────────────────────────────────────────────────────────────────


def test_ordinalele_sunt_dense_nu_pozitiile_din_sablon():
    assert _view().ordinals() == {"1", "2", "3", "4"}


def test_flag_off_pozitiile_din_sablon(monkeypatch):
    monkeypatch.setattr(get_settings(), "routine_dense_ordinals_enabled", False)
    assert _view().ordinals() == {"1", "4", "5", "6"}


def test_intro_real_supravietuieste(monkeypatch):
    monkeypatch.setattr(compose, "get_settings", lambda: _settings())
    ctx = _ctx()
    ctx.routine = _view()

    rich = compose.assemble(ctx, _j(_REAL_INTRO), _RETRIEVED)

    assert rich.intro is not None
    assert "1. ANUA" in rich.intro and "2. SOME BY MI" in rich.intro and "3. IUNIK" in rich.intro


def test_intro_real_cadea_cu_pozitiile_de_sablon(monkeypatch):
    """Defectul, pinuit: cu pozițiile de șablon, «2.» și «3.» nu sunt permise."""
    monkeypatch.setattr(get_settings(), "routine_dense_ordinals_enabled", False)
    monkeypatch.setattr(compose, "get_settings", lambda: _settings())
    ctx = _ctx()
    ctx.routine = _view()

    rich = compose.assemble(ctx, _j(_REAL_INTRO), _RETRIEVED)

    assert rich.intro is None or "2. SOME BY MI" not in rich.intro


def test_peste_n_cifra_cade(monkeypatch):
    """Patru pași: «4» trece, «5» și «6» (pozițiile de șablon) nu mai trec."""
    monkeypatch.setattr(compose, "get_settings", lambda: _settings())
    ctx = _ctx()
    ctx.routine = _view()

    rich = compose.assemble(ctx, _j("4. IUNIK la final. 5. Un pas inventat."), _RETRIEVED)

    assert rich.intro == "4. IUNIK la final."


# ── Vederea pentru model ──────────────────────────────────────────────────────────────────────


def _plan():
    spec = build_spec(
        {
            "families": {
                "fata": ["curatare", "tonifiere", "esenta", "tratament", "hidratare", "protectie"]
            },
            "by_product_type": {"gel de curatare": "fata:curatare"},
        }
    )
    return compose_plan(
        "fata",
        spec,
        candidates={"curatare": ["A"], "tratament": ["I"], "hidratare": ["S"], "protectie": ["U"]},
        reasons={"tonifiere": "budget", "esenta": "budget"},
    )


def test_vederea_numeroteaza_dens_si_declara_golurile():
    view = rt._view(
        _plan(),
        {pid: {"name": f"Produs {pid}", "price": 30} for pid in "AISU"},
        "ro",
        floor=None,
        budget=None,
    )

    assert "1. curatare — [A]" in view
    assert "2. tratament — [I]" in view
    assert "4. protectie — [U]" in view
    assert "Lipsesc: tonifiere (budget), esenta (budget)." in view
    assert "LIPSĂ" not in view


def test_vederea_flag_off_e_forma_veche(monkeypatch):
    monkeypatch.setattr(get_settings(), "routine_dense_ordinals_enabled", False)
    view = rt._view(
        _plan(),
        {pid: {"name": f"Produs {pid}", "price": 30} for pid in "AISU"},
        "ro",
        floor=None,
        budget=None,
    )

    assert "2. tonifiere — LIPSĂ (budget)" in view
    assert "4. tratament — [I]" in view


# ── Referința ulterioară: «a doua» e al doilea card de pe ecran ───────────────────────────────


@dataclass
class _Ref:
    product_id: str
    name: str


def test_a_doua_dupa_rutina_e_al_doilea_card():
    """Starea turului se scrie din cardurile răspunsului, în ordinea sloturilor (`compose.py`,
    NX-292), iar rezolvatorul nu cunoaște pozițiile de șablon. «a doua» = produsul de pe slotul 4
    (tratamentul), nu tonifierea care lipsește."""
    refs = tuple(_Ref(pid, f"Produs {pid}") for pid, _, _ in _REAL_STEPS)

    r = rr.resolve_reference(rr.ReferenceRequest(query="spune-mi de a doua", refs=refs))

    assert r.product_id == "I" and r.source == "ordinal"
