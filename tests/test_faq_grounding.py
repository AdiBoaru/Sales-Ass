"""NX-346 — regula magazinului, citată fidel, nu mai e respinsă ca inventată.

`faq_lookup` îi arată modelului răspunsurile magazinului și îi cere să le redea fidel, dar
validatorul de proză (stagiul 8) știa doar de prețurile produselor: un răspuns care spunea „livrare
gratuită peste 199 lei" sau „returnezi în 30 de zile" pica pe `ungrounded_price` / `bare_number` /
`text_claim`, iar clientul primea „Momentan n-am găsit produse potrivite" (11 din cele 20 de FAQ-uri
SOLE, redate cuvânt cu cuvânt). Acum textele servite în tur sunt SURSE: ce citează proza din ele e
întemeiat, iar orice altă cifră sau afirmație rămâne respinsă. ZERO model, ZERO DB.
"""

import json
from pathlib import Path

import pytest

from src.agent import tool_executor
from src.agent.finalize import render
from src.agent.planner import ResponsePlan
from src.agent.prompt_builder import PromptInputs
from src.agent.tool_executor import ToolRun
from src.agent.validator import source_numbers, source_prices, validate_prose
from src.config import get_settings
from src.models import BusinessConfig, Contact, ConversationState, InboundMessage, TurnContext
from src.tools import faq_tools as ft
from src.tools.base import ToolResult
from src.worker.runner import PipelineDeps
from src.worker.text_scrub import text_claim_keys

_SEED = Path(__file__).resolve().parents[1] / "db" / "seed" / "faqs_sole_ro.json"
_RAW = json.loads(_SEED.read_text(encoding="utf-8"))
SOLE_FAQS = _RAW.get("faqs", _RAW) if isinstance(_RAW, dict) else _RAW
INP = PromptInputs.build("D", "ecommerce", "ro", ["Ten"], [])

SHIPPING = "Livrarea este gratuită pentru comenzile de peste 199 lei, altfel costă 15 lei."
RETURNS = "Poți returna produsele în 30 de zile de la primire."


def _check(reply, sources=()):
    return validate_prose(reply, products=[], grounded_prices=set(), grounded_sources=sources)


# --- validatorul ------------------------------------------------------------------------------


@pytest.mark.parametrize("row", SOLE_FAQS, ids=lambda r: r["question"][:40])
def test_every_sole_answer_quoted_verbatim_passes_with_its_source(row):
    assert _check(row["answer"], [row["answer"]]).ok


def test_without_sources_the_gate_is_unchanged():
    """Fără surse, exact verdictul de dinainte: măsura defectului rămâne 11 din 20."""
    failing = [r for r in SOLE_FAQS if not _check(r["answer"]).ok]
    assert len(failing) == 11


def test_paraphrase_with_inflection_passes():
    """Flexiunea nu schimbă afirmația: sursa zice „Livrarea", proza zice „livrare"."""
    reply = "Ai livrare gratuită de la 199 lei, altfel plătești 15 lei."
    assert not _check(reply).ok
    assert _check(reply, [SHIPPING]).ok


@pytest.mark.parametrize(
    "extra",
    [
        " Și primești o reducere de 20 lei.",  # sumă inventată + afirmație inventată
        " Livrarea durează 2 zile.",  # cifră fără valută inventată
        " Suntem cel mai bun magazin.",  # superlativ inventat
    ],
)
def test_anything_added_beyond_the_source_is_still_rejected(extra):
    assert _check(SHIPPING, [SHIPPING]).ok
    assert not _check(SHIPPING + extra, [SHIPPING]).ok


def test_a_bare_number_in_the_source_does_not_ground_a_price():
    """„30 de zile" nu face din „30 lei" un preț citat: doar sumele cu valută întemeiază prețuri."""
    assert source_prices((RETURNS,)) == set()
    assert 30.0 in source_numbers((RETURNS,))
    assert _check("Poți returna în 30 de zile.", [RETURNS]).ok
    result = _check("Returul costă 30 lei.", [RETURNS])
    assert not result.ok and "ungrounded_price" in result.reasons


def test_a_superlative_is_grounded_only_with_what_it_qualifies():
    """„cel mai" singur nu spune ce se afirmă: „cel mai rapid" din sursă nu acoperă „cel mai bun"."""
    source = "Curierul rapid e cel mai rapid mod de livrare."
    assert "cel mai rapid" in text_claim_keys(source)
    assert _check("Curierul rapid e cel mai rapid mod de livrare.", [source]).ok
    assert not _check("Curierul rapid e cel mai bun mod de livrare.", [source]).ok


def test_kill_switch_restores_the_old_verdict(monkeypatch):
    monkeypatch.setattr(get_settings(), "faq_grounding_enabled", False)
    reply = "Ai livrare gratuită de la 199 lei."
    assert _check(reply, [SHIPPING]).reasons == _check(reply).reasons
    assert not _check(reply, [SHIPPING]).ok


def test_medical_claim_is_never_grounded_by_a_source():
    """Poarta P0 nu se relaxează: nici un text al magazinului nu legitimează un claim medical."""
    source = "Crema tratează acneea."
    result = _check(source, [source])
    assert not result.ok and "medical_claim" in result.reasons


# --- unealta și acumularea pe tur ---------------------------------------------------------------


def _ctx():
    ctx = TurnContext(
        turn_id="t",
        business=BusinessConfig(id="biz-1", slug="s", name="n", default_locale="ro"),
        contact=Contact(id="c", business_id="biz-1"),
        message=InboundMessage(provider_msg_id="m", body="cat e livrarea"),
        conversation_id="conv",
        state=ConversationState(),
    )
    ctx.language = "ro"
    return ctx


class _LLM:
    async def complete(self, system, user, *, model=None):  # retry-ul de proză
        return ""


def _deps():
    return PipelineDeps(conn=object(), redis=None, llm=_LLM())


async def test_faq_tool_returns_the_answers_it_showed_as_sources(monkeypatch):
    rows = [
        {"id": "f1", "question": "Cât costă livrarea?", "answer": SHIPPING},
        {"id": "f2", "question": "Pot returna?", "answer": RETURNS},
    ]

    async def fake(conn, bid, locale, *, limit):
        return list(rows)

    monkeypatch.setattr(ft, "list_active", fake)
    res = await ft.faq_lookup_tool(_ctx(), _deps(), {"query": "livrare"})
    assert res.sources == [SHIPPING, RETURNS]


def test_sources_are_exactly_the_entries_the_view_shows(monkeypatch):
    """Ce taie plafonul vederii nu e sursă: modelul nu l-a văzut, deci nu-l poate cita."""
    monkeypatch.setattr(ft, "MAX_VIEW_CHARS", 500)
    rows = [{"id": str(i), "question": f"Q{i}?", "answer": f"R{i} " + "x" * 60} for i in range(10)]
    view = ft.render_view(rows)
    kept = [r["answer"] for r in ft._fitting(rows)]
    assert 0 < len(kept) < len(rows)
    assert all(a in view for a in kept)
    assert not any(r["answer"] in view for r in rows[len(kept) :])


async def test_tool_run_accumulates_sources(monkeypatch):
    async def fake_run_tool(ctx, deps, name, args):
        return ToolResult(ok=True, llm_view="faq", sources=[SHIPPING])

    monkeypatch.setattr(tool_executor, "run_tool", fake_run_tool)
    run = ToolRun(_ctx(), _deps())
    await run._execute_serialized("faq_lookup", {"query": "livrare"})
    assert run.grounded_sources == [SHIPPING]


# --- servirea -----------------------------------------------------------------------------------


def _plan(final, sources, *, is_order=False):
    return ResponsePlan(
        handled=False,
        products=[],
        final=final,
        is_order=is_order,
        query="cat e livrarea",
        history="",
        inp=INP,
        mode="prose",
        grounded_sources=sources,
    )


async def test_quoted_rule_reaches_the_client_instead_of_no_result():
    reply = "Livrarea e gratuită peste 199 lei, altfel costă 15 lei."
    ctx = _ctx()
    result = await render(ctx, _deps(), _plan(reply, [SHIPPING]))
    assert ctx.reply is not None and ctx.reply.text == reply
    assert result is not None and result.ok

    ctx = _ctx()  # fără sursă: vechiul „n-am găsit", necacheabil
    result = await render(ctx, _deps(), _plan(reply, []))
    assert ctx.reply is not None and ctx.reply.text != reply and ctx.reply.cacheable is False
    assert result is not None and not result.ok


async def test_quoted_rule_on_the_order_route_is_served():
    """Setul de comandă are `faq_lookup` întâi: «cât costă livrarea comenzii?» citează regula, iar
    suma ei trecea doar prin sumele comenzii, deci pica pe `ungrounded_price`."""
    reply = "Livrarea costă 15 lei sub 199 lei."
    ctx = _ctx()
    result = await render(ctx, _deps(), _plan(reply, [SHIPPING], is_order=True))
    assert ctx.reply is not None and ctx.reply.text == reply
    assert result is not None and result.ok

    ctx = _ctx()
    result = await render(ctx, _deps(), _plan(reply, [], is_order=True))
    assert ctx.reply is not None and ctx.reply.text != reply
    assert result is not None and "ungrounded_price" in result.reasons
