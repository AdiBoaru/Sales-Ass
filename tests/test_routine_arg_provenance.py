# ruff: noqa: F811 — `_catalog` e fixture-ul importat din `test_routine_tool`, cerut ca parametru.
"""NX-321 — `routine_plan`: bugetul și nevoile modelului au nevoie de SURSĂ în ce a scris clientul.

Conversația reală `bc7a356e` (`sole-ro`, 2026-09-24), turul «fa mi o rutina»: rutina a ieșit exact
100 lei (30+10+30+30), cu tonifierea și esența tăiate la buget, și filtrată pe roșeață. Clientul
nu spusese nicio sumă, iar roșeața o scrisese BOTUL. Testele verifică ARGUMENTELE și FILTRELE cu
care ajunge unealta la catalog, nu un rezultat de ranking.

Fără DB și fără model: catalogul e cel scriptat din `test_routine_tool.py`.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from src.config import get_settings
from src.domain.constraints import build_units
from src.models import Message
from tests.test_routine_tool import _catalog, _ctx, _run  # noqa: F401 (`_catalog`: fixture autouse)

UNITS = build_units(
    {
        "price": {"factors": {"lei": 1, "ron": 1, "bani": 0.01}, "canonical": "lei"},
        "volume": {"factors": {"ml": 1, "l": 1000}, "canonical": "ml"},
        "spf": {"factors": {"spf": 1, "ip": 1}, "canonical": "spf"},
    }
)


def _with_units(ctx):
    ctx.business.domain_pack = replace(ctx.business.domain_pack, units=UNITS)
    return ctx


def _conversation(current: str, *earlier_client: str, bot: tuple[str, ...] = ()):
    ctx = _with_units(_ctx())
    ctx.message.body = current
    ctx.history = [Message(direction="inbound", author="contact", body=t) for t in earlier_client]
    ctx.history += [Message(direction="outbound", author="bot", body=t) for t in bot]
    return ctx


def _events(ctx, kind):
    return [e.properties for e in ctx.events if e.type == kind]


@pytest.fixture(autouse=True)
def _gate_on(monkeypatch):
    monkeypatch.setattr(get_settings(), "routine_arg_provenance_enabled", True)
    monkeypatch.setattr(get_settings(), "price_bound_unit_aware_enabled", True)


# ── Conversația reală ─────────────────────────────────────────────────────────────────────────


async def test_conversatia_bc7a356e_fara_buget_si_fara_rosteata(_catalog):
    """Argumentele reconstituite ale turului real. Bugetul și «calmare» nu au sursă: ies ÎNAINTE de
    `routine_candidates` și de `_fit_budget`. «se usucă după duș» e rostită, deci rămâne."""
    ctx = _conversation(
        "fa mi o rutina",
        "vreau o crema de fata",
        "pai mi se usuca pielea dupa dus",
        bot=("Am inclus și variante axate pe hidratare și calmarea roșeții.",),
    )

    result = await _run(ctx, concerns=["se usucă după duș", "calmare"], budget_max=100)

    assert _catalog["include_cheapest"] is False  # bugetul n-a ajuns la catalog
    assert all("concerns" not in (f or {}) for f in _catalog["seen_filters"])
    assert "LIPSĂ (budget)" not in result.llm_view
    assert "NU a cerut un plafon de preț" in result.llm_view
    (ev,) = _events(ctx, "routine_arg_provenance")
    assert ev["budget_source"] == "unsupported"
    assert ev["budget_kept"] is False
    assert ev["needs_kept"] == 1 and ev["needs_dropped"] == 1


# ── Bugetul ───────────────────────────────────────────────────────────────────────────────────


async def test_buget_rostit_acum_se_aplica(_catalog):
    ctx = _conversation("fa-mi o rutina de fata sub 150 lei")
    await _run(ctx, budget_max=150)
    assert _catalog["include_cheapest"] is True
    (ev,) = _events(ctx, "routine_arg_provenance")
    assert ev["budget_source"] == "spoken_now" and ev["budget_kept"] is True


async def test_buget_rostit_anterior_se_aplica(_catalog):
    ctx = _conversation("fa-mi o rutina", "am 200 de lei de cheltuit")
    await _run(ctx, budget_max=200)
    assert _catalog["include_cheapest"] is True
    assert _events(ctx, "routine_arg_provenance")[0]["budget_source"] == "spoken_earlier"


async def test_numar_fara_unitate_e_buget_posibil(_catalog):
    ctx = _conversation("ceva sub 100, fa-mi o rutina")
    await _run(ctx, budget_max=100)
    assert _catalog["include_cheapest"] is True


async def test_cerere_relativa_pastreaza_bugetul(_catalog):
    ctx = _conversation("ceva mai ieftin", "fa-mi o rutina")
    await _run(ctx, budget_max=90)
    assert _events(ctx, "routine_arg_provenance")[0]["budget_source"] == "relative_request"


async def test_100_ml_nu_e_buget(_catalog):
    """Obiecția Codex: «100 ml» coroborea literal un buget de 100 și tăia pași din rutină."""
    ctx = _conversation("vreau o rutina cu o crema de 100 ml")
    result = await _run(ctx, budget_max=100)
    assert _catalog["include_cheapest"] is False
    assert "LIPSĂ (budget)" not in result.llm_view
    (ev,) = _events(ctx, "routine_arg_provenance")
    assert ev["budget_kept"] is False and ev["unit_rejected"] is True


async def test_spf_50_nu_e_buget(_catalog):
    ctx = _conversation("rutina de dimineata cu spf 50")
    await _run(ctx, budget_max=50)
    assert _catalog["include_cheapest"] is False


async def test_suma_si_volum_in_acelasi_mesaj(_catalog):
    ctx = _conversation("100 lei sau 100 ml, oricare, fa-mi o rutina")
    await _run(ctx, budget_max=100)
    assert _catalog["include_cheapest"] is True


async def test_fara_nicio_cifra_bugetul_iese(_catalog):
    ctx = _conversation("fa mi o rutina")
    result = await _run(ctx, budget_max=100)
    assert _catalog["include_cheapest"] is False
    assert "NU a cerut un plafon de preț" in result.llm_view


# ── Nevoile ───────────────────────────────────────────────────────────────────────────────────


async def test_nevoie_rostita_filtreaza(_catalog):
    ctx = _conversation("fa-mi o rutina", "am tenul uscat")
    await _run(ctx, concerns=["ten uscat"])
    assert any(f and f.get("skin_type") == ["dry"] for f in _catalog["seen_filters"])
    assert _events(ctx, "routine_arg_provenance")[0]["keys"] == ["dry"]


async def test_nevoie_spusa_doar_de_bot_iese(_catalog):
    ctx = _conversation("fa-mi o rutina", bot=("Pentru hidratare îți recomand…",))
    result = await _run(ctx, concerns=["hidratare"])
    assert all(not f for f in _catalog["seen_filters"])
    assert "LIPSĂ (filtered)" not in result.llm_view
    assert "Nevoi ignorate" in result.llm_view


# ── Kill-switch ───────────────────────────────────────────────────────────────────────────────


async def test_flag_off_argumentele_trec_ca_inainte(_catalog, monkeypatch):
    monkeypatch.setattr(get_settings(), "routine_arg_provenance_enabled", False)
    ctx = _conversation("fa mi o rutina")
    result = await _run(ctx, concerns=["hidratare"], budget_max=100)
    assert _catalog["include_cheapest"] is True
    assert any(f and f.get("concerns") == ["hydration"] for f in _catalog["seen_filters"])
    assert not _events(ctx, "routine_arg_provenance")
    assert "plafon de preț" not in result.llm_view
