"""NX-346 — regula magazinului, citată fidel, nu mai e respinsă ca inventată.

`faq_lookup` îi arată modelului răspunsurile magazinului și îi cere să le redea fidel, dar
validatorul de proză (stagiul 8) știa doar de prețurile produselor: un răspuns care spunea „livrare
gratuită peste 199 lei" sau „returnezi în 30 de zile" pica pe `ungrounded_price` / `bare_number` /
`text_claim`, iar clientul primea „Momentan n-am găsit produse potrivite" (11 din cele 20 de FAQ-uri
SOLE, redate cuvânt cu cuvânt). Acum o propoziție CITATĂ LITERAL dintr-o regulă iese de sub porțile
de cifre și afirmații, iar restul textului e judecat ca înainte.

Literal, nu aproximativ: recenzia adversarială a spart două variante mai largi (cifrele corpusului
întemeiate global, apoi acoperirea de cuvinte cu vecin comun), iar atacurile ei sunt aici, rulate cu
TOT corpusul SOLE ca sursă, exact ce primește validatorul în producție. ZERO model, ZERO DB.
"""

import json
from pathlib import Path

import pytest

from src.agent import tool_executor
from src.agent.finalize import render
from src.agent.planner import ResponsePlan
from src.agent.prompt_builder import PromptInputs
from src.agent.tool_executor import ToolRun
from src.agent.validator import strip_quoted, validate_prose
from src.config import get_settings
from src.models import BusinessConfig, Contact, ConversationState, InboundMessage, TurnContext
from src.tools import faq_tools as ft
from src.tools.base import ToolResult
from src.worker.runner import PipelineDeps

_SEED = Path(__file__).resolve().parents[1] / "db" / "seed" / "faqs_sole_ro.json"
_RAW = json.loads(_SEED.read_text(encoding="utf-8"))
SOLE_FAQS = _RAW.get("faqs", _RAW) if isinstance(_RAW, dict) else _RAW
CORPUS = [r["answer"] for r in SOLE_FAQS]  # ce aduce `faq_lookup` pe SOLE: TOT setul activ
INP = PromptInputs.build("D", "ecommerce", "ro", ["Ten"], [])

SHIPPING = "Livrarea este gratuită pentru comenzile de peste 199 lei, altfel costă 15 lei."
RETURNS = "Poți returna produsele în 30 de zile de la primire."
CARD = {"id": "p1", "name": "Crema Aqua", "price": 82.99, "availability": "in_stock"}


def _check(reply, sources=(), products=()):
    return validate_prose(
        reply, products=list(products), grounded_prices=set(), grounded_sources=sources
    )


# --- validatorul ------------------------------------------------------------------------------


@pytest.mark.parametrize("row", SOLE_FAQS, ids=lambda r: r["question"][:40])
def test_every_sole_answer_quoted_verbatim_passes_with_the_whole_corpus(row):
    assert _check(row["answer"], CORPUS).ok


def test_without_sources_the_gate_is_unchanged():
    """Fără surse, exact verdictul de dinainte: măsura defectului rămâne 11 din 20."""
    failing = [r for r in SOLE_FAQS if not _check(r["answer"]).ok]
    assert len(failing) == 11
    assert strip_quoted(SHIPPING, ()) == SHIPPING  # identitate, nu doar același verdict
    assert strip_quoted("Crema Aqua costă 82,99 lei.", CORPUS) == "Crema Aqua costă 82,99 lei."


def test_a_verbatim_part_of_a_rule_is_a_quote():
    """Un fragment continuu al regulii, pe cuvinte întregi, e citat: diacriticele și punctuația
    nu contează, cuvintele da."""
    assert _check("Poti returna produsele in 30 de zile!", [RETURNS]).ok
    assert not _check("Poți returna în 30 de zile.", [RETURNS]).ok  # a lipsit un cuvânt


def test_a_paraphrase_is_judged_as_before_declared():
    """Declarat în card: o regulă reformulată nu e citat, deci e judecată ca pe `main` (nicio
    regresie). E prețul variantei stricte: orice criteriu aproximativ a fost spart."""
    reply = "Livrarea e gratuită la comenzi de peste 199 lei."
    assert _check(reply, CORPUS).reasons == _check(reply).reasons


@pytest.mark.parametrize(
    "extra",
    [
        " Și primești o reducere de 20 lei.",  # sumă și afirmație inventate
        " Livrarea durează 2 zile.",  # o singură cifră, pe care poarta de cifre n-o vede
        " Suntem cel mai bun magazin.",  # superlativ inventat
    ],
)
def test_anything_added_beyond_the_rule_is_still_rejected(extra):
    assert _check(SHIPPING, [SHIPPING]).ok
    assert not _check(SHIPPING + extra, [SHIPPING]).ok
    assert not _check(SHIPPING + extra, CORPUS + [SHIPPING]).ok


@pytest.mark.parametrize(
    "reply",
    [
        # recenzia 1: întemeierea GLOBALĂ a cifrelor și cuvintelor corpusului
        "Crema Hidratantă Aqua e acum doar 1 leu, iar livrarea ajunge în 12 ore.",
        "Crema Hidratanta X costă 199 lei.",
        "Vezi rezultate vizibile în 14 zile de utilizare.",
        "Serul hidratează pielea timp de 30 de ore.",
        "Crema X e cea mai simplă soluție pentru ten gras.",
        # recenzia 2: acoperirea de cuvinte cu un vecin comun
        "Crema Aqua costă 49,9 lei și se scade din rambursare.",
        "Crema Aqua costă 199 lei dacă ai mai comandat la noi cel puțin o dată.",
        "Taxa de transport pentru Crema Aqua e 49,9 lei și se reține din suma plătită.",
        "Peste 199 lei transportul e gratuit, iar Crema Aqua scade la 149 lei.",
        "Un punct valorează 1 leu și îl folosești ca plată, iar Crema Aqua valorează 1 leu.",
        "Crema Aqua ajunge în 24 de ore oriunde în România, din stocul nostru.",
        # o sumă a regulii, singură într-o propoziție, lipită de un produs
        "Crema Aqua costă. 199 lei.",
        # recenzia 2: prețul rupt pe două rânduri
        "Star Card de la Banca Transilvania în 3, 6, 9 sau 12\nlei.",
    ],
)
def test_a_rule_number_or_word_does_not_travel_to_a_product(reply):
    sources = CORPUS + ["Cea mai simplă cale e să ne suni."]
    for products in ((), (CARD,)):
        assert not _check(reply, sources, products).ok, reply


def test_a_quoted_sentence_is_stripped_the_rest_is_judged():
    """Propoziția citată iese de sub poartă, cea inventată de lângă ea nu."""
    reply = "Poți returna produsele în 30 de zile de la primire. Crema costă 30 lei."
    result = _check(reply, [RETURNS])
    assert not result.ok and result.reasons == ["ungrounded_price"]
    assert strip_quoted(reply, [RETURNS]) == "Crema costă 30 lei."


def test_kill_switch_restores_the_old_verdict(monkeypatch):
    monkeypatch.setattr(get_settings(), "faq_grounding_enabled", False)
    assert _check(SHIPPING, [SHIPPING]).reasons == _check(SHIPPING).reasons
    assert not _check(SHIPPING, [SHIPPING]).ok


def test_medical_claim_is_never_grounded_by_a_source():
    """Poarta P0 nu se relaxează: nici un text al magazinului nu legitimează un claim medical."""
    source = "Crema tratează acneea și calmează pielea în câteva zile."
    result = _check(source, [source])
    assert not result.ok and "medical_claim" in result.reasons


def test_many_sentences_stay_cheap():
    """Recenzia 2 măsurase 0,6-1 s pe sute de propoziții scurte cu 40 de surse; acum sursele se
    pliază o dată pe apel, iar o propoziție fără cifră sau afirmație nu se caută."""
    import time

    reply = " ".join(["Da! Sigur."] * 350 + ["Ok 12 lei."] * 50)
    started = time.perf_counter()
    strip_quoted(reply, CORPUS * 2)
    assert time.perf_counter() - started < 0.25


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
    reply = SHIPPING
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
    reply = SHIPPING
    ctx = _ctx()
    result = await render(ctx, _deps(), _plan(reply, [SHIPPING], is_order=True))
    assert ctx.reply is not None and ctx.reply.text == reply
    assert result is not None and result.ok

    ctx = _ctx()
    result = await render(ctx, _deps(), _plan(reply, [], is_order=True))
    assert ctx.reply is not None and ctx.reply.text != reply
    assert result is not None and "ungrounded_price" in result.reasons


def test_plan_mode_sees_the_sources():
    """`_plan_mode` decide proză vs fallback pe același `_valid`: fără surse ar anunța fallback."""
    from src.agent.planner import _plan_mode

    reply = SHIPPING
    kw = dict(compared=[], products=[], final=reply, is_order=False, generated_links=set())
    assert _plan_mode(_ctx(), grounded_prices=set(), grounded_sources=[SHIPPING], **kw) == "prose"
    assert _plan_mode(_ctx(), grounded_prices=set(), **kw) == "fallback"


class _CaptureLLM:
    def __init__(self):
        self.users = []

    async def complete(self, system, user, *, model=None):
        self.users.append(user)
        return ""


async def test_retry_message_carries_the_rules_only_when_there_are_sources():
    from src.agent.finalize import _finalize

    args = ("sys", "cat e livrarea", "Costă 999 lei.", [], "ro", "")
    plain, with_rules = _CaptureLLM(), _CaptureLLM()
    await _finalize(plain, *args)
    await _finalize(with_rules, *args, allowed_sources=[SHIPPING])
    assert "Regulile magazinului" not in plain.users[0]
    assert with_rules.users[0].startswith(plain.users[0])  # fără surse: mesajul de dinainte
    assert SHIPPING in with_rules.users[0]
