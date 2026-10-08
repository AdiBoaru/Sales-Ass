"""NX-382 faza 3 — comparația scrisă de compozitorul unic.

Contractul fazei: tabelul rămâne date. Compozitorul scrie, într-un singur apel, răspunsul la ce a
întrebat clientul (deasupra tabelului), verdictul pe tipuri de client (dedesubt) și axele; fiecare
celulă trece prin aceeași verificare pe sursă ca narativul de azi (`assemble_axes`). Fără axe
păstrate rămân rândurile deterministe. Verdictul reținut (I12) și orice eșec al modelului rămân
pe calea de azi (`compose_comparison`).

Zero model real, zero DB."""

from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from src.agent import composer as cp
from src.agent import kernel_executors as kx
from src.config import get_settings
from src.models import Comparison, ComparisonColumn, ComparisonRow
from tests.kernel import stage_harness as sh
from tests.test_interpreted_turn_c import (  # noqa: F401
    _ctx,
    _outcome,
    _pair,
    _plan,
    _planned,
    _policy,
    _spy_compose,
    electronics,
)

P1 = {
    "id": "p1",
    "name": "COSRX The Retinol 0.1 Cream",
    "price": 142.0,
    "availability": "in_stock",
    "rating": 4.89,
    "review_count": 19,
    "attributes": {},
    "top_pros": ["se absoarbe repede"],
    "sections": [],
}
P2 = {**P1, "id": "p2", "name": "SOME BY MI Retinol Bakuchiol Dual Cream", "price": 140.0}
P2["top_pros"] = ["nu lasa urme"]


def _reply(text="Diferența e textura.", verdict="", axes=(), met=(), suggestions=(), advice=""):
    return {
        "text": text,
        "general_advice": advice,
        "items": [],
        "suggestions": list(suggestions),
        "obligations_met": list(met),
        "verdict": verdict,
        "axes": list(axes),
    }


def _axis(label, *cells):
    return {"label": label, "cells": [{"handle": h, "source": s, "text": t} for h, s, t in cells]}


class ComposeLLM:
    def __init__(self, reply):
        self.reply = reply
        self.calls: list[tuple[str, str, dict]] = []

    async def complete_schema(self, system, user, schema, **kw):
        self.calls.append((system, user, schema))
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def _table() -> Comparison:
    return Comparison(
        columns=[
            ComparisonColumn(product_id="p1", name="COSRX", price=142.0),
            ComparisonColumn(product_id="p2", name="SOME BY MI", price=140.0),
        ],
        rows=[ComparisonRow(label="Preț", values=["142 lei", "140 lei"])],
        intro="Lead determinist.",
    )


# --- schema și parsarea ---------------------------------------------------------------------------


def test_only_the_compare_schema_asks_for_a_verdict_and_axes():
    body = cp.schema(cp.ComposeInput(task="compare", products=[P1, P2]))["schema"]
    assert body["required"][-2:] == ["verdict", "axes"]
    cell = body["properties"]["axes"]["items"]["properties"]["cells"]["items"]
    assert cell["properties"]["handle"]["enum"] == ["P1", "P2"]
    other = cp.schema(cp.ComposeInput(task="detail", products=[P1]))["schema"]
    assert "verdict" not in other["properties"] and "axes" not in other["properties"]


def test_cells_are_translated_from_handles_to_ids():
    inp = cp.ComposeInput(task="compare", products=[P1, P2])
    raw = _reply(axes=[_axis("Cum se simte", ("P1", "avantaje", "se absoarbe"))])
    composed = cp.parse(raw, inp)
    assert composed.axes[0]["cells"][0]["product_id"] == "p1"
    # pe altă sarcină câmpurile comparației nu există
    assert cp.parse(raw, cp.ComposeInput(task="detail", products=[P1])).axes == ()


def test_the_verdict_is_judged_on_the_facts():
    inp = cp.ComposeInput(task="compare", products=[P1, P2])
    bad = cp.parse(_reply(verdict="Ia COSRX, costă 99 lei."), inp)
    verdict, _ = cp.check(bad, inp, facts="", units=frozenset())
    assert not verdict.ok
    good = cp.parse(_reply(verdict="Pentru ten uscat aș lua COSRX."), inp)
    verdict, cleaned = cp.check(good, inp, facts="", units=frozenset())
    assert verdict.ok and cleaned.verdict == "Pentru ten uscat aș lua COSRX."


def test_the_comparison_sources_name_each_product_under_its_handle():
    assert cp.comparison_sources([P1, P2], (), "ro") == (
        "P1: avantaje: se absoarbe repede\nP2: avantaje: nu lasa urme"
    )


# --- tabelul -------------------------------------------------------------------------------------


def _events(ctx, name):
    return [e for e in ctx.events if e.type == name]


def _composed(axes, verdict="Pentru ten uscat, COSRX.", advice=""):
    inp = cp.ComposeInput(task="compare", products=[P1, P2])
    return cp.parse(_reply("Diferența e cum se simt.", verdict, axes, advice=advice), inp)


def test_model_axes_with_their_sources_become_the_table_plus_the_code_rows(electronics):  # noqa: F811
    ctx = _ctx(electronics)
    axes = [
        _axis(
            "Cum se simte",
            ("P1", "avantaje", "se absoarbe repede"),
            ("P2", "avantaje", "fără urme"),
        )
    ]
    built = cp.comparison_reply(
        ctx, _composed(axes, advice="Aplic-o seara."), _table(), [P1, P2], ()
    )
    assert built.rows[0].label == "Cum se simte"
    assert built.rows[0].values == ["se absoarbe repede", "fără urme"]
    assert any("Preț" in r.label or "Pret" in r.label for r in built.rows[1:])
    assert built.intro == "Diferența e cum se simt."
    assert built.closing == ["Pentru ten uscat, COSRX.", "Aplic-o seara."]
    [event] = _events(ctx, "composer_axes")
    assert event.properties["kept"] == 1


def test_an_axis_citing_a_source_the_product_lacks_keeps_the_deterministic_rows(
    electronics,  # noqa: F811
):
    ctx = _ctx(electronics)
    axes = [_axis("Rezistență", ("P1", "rezistenta", "ține 12 ore"), ("P2", "rezistenta", "slab"))]
    built = cp.comparison_reply(ctx, _composed(axes), _table(), [P1, P2], ())
    assert [r.label for r in built.rows] == ["Preț"]
    assert built.intro == "Diferența e cum se simt."
    [event] = _events(ctx, "composer_axes")
    assert event.properties["kept"] == 0 and event.properties["rejected"]


# --- executorul ---------------------------------------------------------------------------------


def _deps(llm):
    return NS(db=sh.RecordingDb(), llm=llm)


async def test_the_compare_plan_is_written_by_the_composer_with_the_question(
    monkeypatch,
    electronics,  # noqa: F811
):
    spy = _spy_compose(monkeypatch)
    llm = ComposeLLM(
        _reply(
            "Diferența principală e memoria.",
            verdict="Pentru poze multe, al doilea.",
            suggestions=["Adaugă-l pe primul în coș"],
        )
    )
    ctx = _ctx(electronics, "care e mai bun pentru poze?")
    plan = _plan(executor="compare", product_ids=_pair(electronics))
    plan = plan.model_copy(update={"question": "care e mai bun pentru poze?"})
    policy_for = lambda partners, rows: _policy(True)  # noqa: E731
    assert await kx.execute_read_plans(ctx, _deps(llm), _planned(plan), _outcome(), policy_for)
    assert spy == [], "narativul de azi nu mai rulează"
    cmp = ctx.reply.comparison
    assert cmp.intro == "Diferența principală e memoria."
    assert cmp.closing == ["Pentru poze multe, al doilea."]
    assert ctx.reply.suggestions == ["Adaugă-l pe primul în coș"]
    system, user, schema = llm.calls[0]
    assert "TASK compare:" in system and "QUESTION: care e mai bun pentru poze?" in user
    assert "COMPARISON SOURCES" in user
    assert schema["schema"]["required"][-2:] == ["verdict", "axes"]


@pytest.mark.parametrize("reply", [RuntimeError("down"), _reply("Primul costă 99 lei.")])
async def test_a_failed_composer_falls_back_to_todays_narrative(
    monkeypatch,
    electronics,  # noqa: F811
    reply,
):
    spy = _spy_compose(monkeypatch)
    ctx = _ctx(electronics, "compara-le")
    planned = _planned(_plan(executor="compare", product_ids=_pair(electronics)))
    policy_for = lambda partners, rows: _policy(True)  # noqa: E731
    assert await kx.execute_read_plans(
        ctx, _deps(ComposeLLM(reply)), planned, _outcome(), policy_for
    )
    assert len(spy) == 1 and "verdict" not in spy[0]


async def test_a_withheld_verdict_stays_on_todays_path(monkeypatch, electronics):  # noqa: F811
    spy = _spy_compose(monkeypatch)
    llm = ComposeLLM(_reply("x", verdict="Ia-l pe primul."))
    ctx = _ctx(electronics, "compara-le")
    planned = _planned(_plan(executor="compare", product_ids=_pair(electronics)))
    policy_for = lambda partners, rows: _policy(False)  # noqa: E731
    assert await kx.execute_read_plans(ctx, _deps(llm), planned, _outcome(), policy_for)
    assert llm.calls == [] and spy[0]["verdict"] is False


async def test_the_compare_flag_off_is_todays_narrative(monkeypatch, electronics):  # noqa: F811
    monkeypatch.setattr(get_settings(), "composer_compare_enabled", False)
    spy = _spy_compose(monkeypatch)
    llm = ComposeLLM(_reply("x"))
    ctx = _ctx(electronics, "compara-le")
    planned = _planned(_plan(executor="compare", product_ids=_pair(electronics)))
    assert await kx.execute_read_plans(ctx, _deps(llm), planned, _outcome())
    assert llm.calls == [] and len(spy) == 1
