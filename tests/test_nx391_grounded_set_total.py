"""NX-391 — totalul unui set de produse e un fapt, nu un preț inventat.

Setul nevăzut `mixed-2026-10-09`, m2: după o rutină de șase produse, la «cât ar ieși în total?»
modelul a scris corect „Toate cele șase produse recomandate ar costa 615 lei”, iar validatorul a
respins suma (615 nu e prețul unui produs), deci clientul a primit „Nu pot să-ți confirm sigur asta
acum”. Codul nu citește cererea clientului: modelul înțelege orice formulare și scrie suma, iar
codul verifică doar că ea e totalul REAL al unui set arătat. Doar totalul întreg, nu orice
submulțime (poarta anti-injecție, NX-121). Zero model real, zero DB."""

from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from src.agent import kernel_executors as kx
from src.agent.validator import _allowed_numbers, _prices_ok, set_total
from tests.kernel import fixture_catalog as fc
from tests.kernel import stage_harness as sh
from tests.test_interpreted_turn_d import LoopLLM, _deps, _outcome, _planned
from tests.test_kernel_ambiguity_gate import screen

SIX = [
    {"id": "a", "price": 100.0},
    {"id": "b", "price": 110.0},
    {"id": "c", "price": 115.0},
    {"id": "d", "price": 50.0},
    {"id": "e", "price": 140.0},
    {"id": "f", "price": 100.0},
]


# --- validatorul ---------------------------------------------------------------------------------


def test_the_total_of_the_set_is_grounded():
    assert set_total(SIX) == 615.0
    assert _prices_ok("Toate șase ar costa 615 lei.", SIX)
    assert _prices_ok("În total plătești 615,00 lei.", SIX)


def test_the_total_without_currency_is_a_known_number():
    assert 615.0 in _allowed_numbers(SIX, set())


def test_a_partial_sum_is_not_grounded():
    """Doar totalul întreg: 100 + 50 = 150 ar fi un preț „real” pentru orice produs inventat."""
    assert not _prices_ok("Crema costă 150 lei.", SIX)


def test_an_invented_total_is_still_rejected():
    assert not _prices_ok("Toate ar costa 600 lei.", SIX)


def test_the_same_product_twice_counts_once():
    assert set_total([{"id": "a", "price": 100.0}, {"id": "a", "price": 100.0}]) is None
    assert set_total([*SIX, {"id": "a", "price": 100.0}]) == 615.0


def test_one_product_has_no_set_total():
    assert set_total([{"id": "a", "price": 100.0}]) is None
    assert set_total([{"id": "a", "price": None}, {"id": "b", "price": 10.0}]) is None


# --- bucla restrânsă: prețurile a ce vede clientul -----------------------------------------------


@pytest.fixture
def electronics(monkeypatch):
    cat = sh.catalog("electronics")
    sh.install(monkeypatch, cat, executors=True)

    async def fetch(deps, business_id, lookup, *, op="reference_facts"):
        return cat.facts(lookup)

    monkeypatch.setattr("src.catalog.reference_facts.fetch_reference_facts", fetch)
    return cat


def _ctx_on_screen(cat, *ids, text="x"):
    state = screen("electronics", *ids)
    ctx = sh.build_ctx(cat, state, text)
    ctx.kernel_view = NS(gate_state=state)
    return ctx


def _price(pid: str) -> float:
    return float(fc.products("electronics")[pid]["price"])


async def test_the_delegate_answer_with_the_screen_total_is_served(electronics):
    total = _price("el-01") + _price("el-02") + _price("el-03")
    final = f"Toate trei ar costa {total:.0f} lei."
    ctx = _ctx_on_screen(electronics, "el-01", "el-02", "el-03")
    served = await kx.execute_read_plans(
        ctx, _deps(LoopLLM(final=final)), _planned("delegate"), _outcome()
    )
    assert served is True
    assert ctx.reply.text == final
    assert {"outcome": "read", "prices": 4, "turn_id": "t0"} in [
        e.properties for e in ctx.events if e.type == "delegate_shown_prices"
    ]


async def test_the_delegate_answer_with_a_screen_price_is_served(electronics):
    final = f"Primul costă {_price('el-01'):.0f} lei."
    ctx = _ctx_on_screen(electronics, "el-01", "el-02")
    await kx.execute_read_plans(ctx, _deps(LoopLLM(final=final)), _planned("delegate"), _outcome())
    assert ctx.reply.text == final


async def test_a_wrong_total_on_the_delegate_path_is_still_rejected(electronics):
    wrong = _price("el-01") + _price("el-02") + 1000
    final = f"Toate ar costa {wrong:.0f} lei."
    ctx = _ctx_on_screen(electronics, "el-01", "el-02")
    await kx.execute_read_plans(ctx, _deps(LoopLLM(final=final)), _planned("delegate"), _outcome())
    assert ctx.reply is None or ctx.reply.text != final


async def test_a_failed_price_read_keeps_todays_validation(monkeypatch, electronics):
    async def broken(*a, **kw):
        raise RuntimeError("db down")

    monkeypatch.setattr("src.catalog.reference_facts.fetch_reference_facts", broken)
    total = _price("el-01") + _price("el-02")
    final = f"Ambele costă {total:.0f} lei."
    ctx = _ctx_on_screen(electronics, "el-01", "el-02")
    await kx.execute_read_plans(ctx, _deps(LoopLLM(final=final)), _planned("delegate"), _outcome())
    assert ctx.reply is None or ctx.reply.text != final
    assert {"outcome": "unavailable", "turn_id": "t0"} in [
        e.properties for e in ctx.events if e.type == "delegate_shown_prices"
    ]
