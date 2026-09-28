"""NX-346 — regula magazinului, citată fidel, nu mai e respinsă ca inventată.

`faq_lookup` îi arată modelului răspunsurile magazinului și îi cere să le redea fidel, dar
validatorul de proză (stagiul 8) știa doar de prețurile produselor: un răspuns care spunea „livrare
gratuită peste 199 lei" sau „returnezi în 30 de zile" pica pe `ungrounded_price` / `bare_number` /
`text_claim`, iar clientul primea „Momentan n-am găsit produse potrivite" (11 din cele 20 de FAQ-uri
SOLE, redate cuvânt cu cuvânt). Acum o PROPOZIȚIE citată dintr-o regulă iese de sub porțile de
cifre și afirmații, iar restul textului e judecat ca înainte. Unitatea e propoziția, nu cifra:
`faq_lookup` aduce TOT corpusul, deci o cifră întemeiată global ar legitima „Crema X costă 199 lei"
(recenzia adversarială a PR-ului). Testele de respingere rulează cu tot corpusul SOLE ca sursă,
exact ce primește validatorul în producție. ZERO model, ZERO DB.
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
from src.worker.text_scrub import text_claim_keys

_SEED = Path(__file__).resolve().parents[1] / "db" / "seed" / "faqs_sole_ro.json"
_RAW = json.loads(_SEED.read_text(encoding="utf-8"))
SOLE_FAQS = _RAW.get("faqs", _RAW) if isinstance(_RAW, dict) else _RAW
CORPUS = [r["answer"] for r in SOLE_FAQS]  # ce aduce `faq_lookup` pe SOLE: TOT setul activ
INP = PromptInputs.build("D", "ecommerce", "ro", ["Ten"], [])

SHIPPING = "Livrarea este gratuită pentru comenzile de peste 199 lei, altfel costă 15 lei."
RETURNS = "Poți returna produsele în 30 de zile de la primire."


def _check(reply, sources=()):
    return validate_prose(reply, products=[], grounded_prices=set(), grounded_sources=sources)


# --- validatorul ------------------------------------------------------------------------------


@pytest.mark.parametrize("row", SOLE_FAQS, ids=lambda r: r["question"][:40])
def test_every_sole_answer_quoted_verbatim_passes_with_the_whole_corpus(row):
    assert _check(row["answer"], CORPUS).ok


def test_without_sources_the_gate_is_unchanged():
    """Fără surse, exact verdictul de dinainte: măsura defectului rămâne 11 din 20."""
    failing = [r for r in SOLE_FAQS if not _check(r["answer"]).ok]
    assert len(failing) == 11
    assert strip_quoted(SHIPPING, ()) == SHIPPING  # identitate, nu doar același verdict


def test_rewording_that_keeps_the_rule_passes():
    """Flexiunea și ordinea cuvintelor nu schimbă citatul: „Livrarea este gratuită pentru
    comenzile" → „Livrarea e gratuită la comenzi"."""
    reply = "Livrarea e gratuită la comenzi de peste 199 lei."
    assert not _check(reply).ok
    assert _check(reply, CORPUS).ok


@pytest.mark.parametrize(
    "extra",
    [
        " Și primești o reducere de 20 lei.",  # sumă și afirmație inventate
        " Livrarea durează 2 zile.",  # cifră de o singură cifră, pe care poarta de cifre n-o vede
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
        # injecția NX-121: prețul unui produs rescris cu o sumă care există într-o regulă
        "Crema Hidratantă Aqua e acum doar 1 leu, iar livrarea ajunge în 12 ore.",
        "Crema Hidratanta X costă 199 lei.",
        # o cifră și un cuvânt-afirmație al unei reguli, mutate într-o promisiune despre produs
        "Vezi rezultate vizibile în 14 zile de utilizare.",
        "Serul hidratează pielea timp de 30 de ore.",
        # superlativul unei reguli, mutat pe un produs
        "Crema X e cea mai simplă soluție pentru ten gras.",
    ],
)
def test_a_rule_number_or_word_does_not_travel_to_another_sentence(reply):
    """Recenzia adversarială: cu întemeiere GLOBALĂ, toate treceau fiindcă cifra sau cuvântul apar
    undeva în corpus. Pe propoziție, niciuna nu e citată din vreo regulă."""
    sources = CORPUS + ["Cea mai simplă cale e să ne suni."]
    assert _check(reply).reasons == _check(reply, sources).reasons
    assert not _check(reply, sources).ok


def test_a_quoted_sentence_is_stripped_the_rest_is_judged():
    """Propoziția citată iese de sub poartă, cea inventată de lângă ea nu."""
    reply = "Poți returna produsele în 30 de zile de la primire. Crema costă 30 lei."
    result = _check(reply, [RETURNS])
    assert not result.ok and result.reasons == ["ungrounded_price"]
    assert strip_quoted(reply, [RETURNS]) == "Crema costă 30 lei."


def test_a_bare_number_in_the_rule_does_not_ground_a_price():
    """„30 de zile" nu face din „30 lei" un preț citat: clasa cifrei face parte din citat."""
    assert _check("Poți returna în 30 de zile.", [RETURNS]).ok
    result = _check("Returul produselor costă 30 lei.", [RETURNS])
    assert not result.ok and "ungrounded_price" in result.reasons


def test_a_superlative_is_grounded_only_with_what_it_qualifies():
    """„cel mai" singur nu spune ce se afirmă: „cel mai rapid" nu acoperă „cel mai bun"."""
    source = "Curierul rapid e cel mai rapid mod de livrare."
    assert "celmai rapid" in text_claim_keys(source)
    assert _check("Curierul rapid e cel mai rapid mod de livrare.", [source]).ok
    assert not _check("Curierul rapid e cel mai bun mod de livrare.", [source]).ok


def test_superlative_key_ignores_spacing_inside_the_phrase():
    assert text_claim_keys("E nr. 1 la vânzări") == text_claim_keys("E nr.1 la vânzări")


def test_a_number_moved_to_a_new_context_is_rejected_declared():
    """Declarat în card: o parafrază care mută o cifră într-un context nou („de la 19,90 lei"
    dintr-o regulă „între 19,9 și 24,90 lei") nu e citat. Conservator: cade pe fallback."""
    source = "Taxa de livrare e între 19,9 și 24,90 lei."
    assert not _check("Livrarea costă de la 19,90 lei.", [source]).ok


def test_kill_switch_restores_the_old_verdict(monkeypatch):
    monkeypatch.setattr(get_settings(), "faq_grounding_enabled", False)
    assert _check(SHIPPING, [SHIPPING]).reasons == _check(SHIPPING).reasons
    assert not _check(SHIPPING, [SHIPPING]).ok


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
    reply = "Livrarea e gratuită peste 199 lei, altfel costă 15 lei."
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

    reply = "Livrarea e gratuită peste 199 lei, altfel costă 15 lei."
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
