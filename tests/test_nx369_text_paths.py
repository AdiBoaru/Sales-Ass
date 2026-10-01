"""NX-369 — textul modelului nu se mai pierde: regulile parafrazate și handle-urile din proză.

Ambele vin din setul `prod-2026-10-01`, înregistrate și rejucate (`tests/replay`):
- c14 T0: răspunsul corect despre retur, parafrazat, respins ⇒ „Momentan n-am găsit produse";
- c5 T0: „P1 are SPF 50…, P2 și P6 sunt…" ⇒ scrub-ul arunca propozițiile, trei carduri fără text.
"""

from __future__ import annotations

from src.agent.finalize import _handle_names, _resolve_handles
from src.agent.store_rules import quoted_rules

# Regulile magazinului SOLE, exact cum le primește modelul (`faq_tools`, `sources`).
RULES = [
    "30 de zile calendaristice de la cumpărare. E peste minimul legal, iar dreptul tău de "
    "retragere în 14 zile rămâne neatins.",
    "Ai 30 de zile de la cumpărare. Intri pe www.service-return.com, la secțiunea „Returnează un "
    'produs", completezi datele și validezi cererea, apoi curierul vine să ridice produsul '
    "împreună cu documentele.",
    "Taxa de transport pentru retur e 49,9 lei și se reține din suma pe care ți-o rambursăm. "
    "Dacă produsul e defect, returul e gratuit.",
    "Da, cât timp nu are urme de utilizare, iar cutia e întreagă și completă. Adaugă în colet și o "
    "copie a documentului de achiziție.",
    "Taxa de livrare e între 19,9 și 24,90 lei cu TVA inclus, iar suma exactă o vezi la "
    "finalizarea comenzii. Peste 199 lei transportul e gratuit, iar dacă ai mai comandat la noi "
    "pragul scade la 149 lei.",
]

C14_MODEL_TEXT = (
    "Da, poți solicita returul în 30 de zile de la cumpărare. Pentru o cremă deschisă, aceasta nu "
    "trebuie să aibă urme de utilizare, iar cutia trebuie să fie întreagă și completă. Inițiezi "
    "cererea pe www.service-return.com și incluzi o copie a documentului de achiziție."
)


def test_c14_paraphrase_maps_to_the_store_rules_it_restates():
    assert quoted_rules(C14_MODEL_TEXT, RULES, "ro") == [RULES[1], RULES[3]]


def test_c7_delivery_paraphrase_maps_to_the_delivery_rule():
    text = (
        "Livrarea costă între 19,90 și 24,90 lei, cu TVA inclus. Suma exactă apare la finalizarea "
        "comenzii."
    )
    assert quoted_rules(text, RULES, "ro") == [RULES[4]]


def test_unrelated_text_maps_to_nothing():
    text = "Îți recomand o cremă hidratantă cu ceramide, potrivită pentru tenul uscat."
    assert quoted_rules(text, RULES, "ro") == []


def test_short_sentences_link_nothing():
    assert quoted_rules("Vrei și altceva? Spune-mi.", RULES, "ro") == []


def test_no_sources_or_no_text():
    assert quoted_rules(C14_MODEL_TEXT, [], "ro") == []
    assert quoted_rules("", RULES, "ro") == []


def test_exact_duplicate_rules_take_the_first():
    dup = [RULES[4], RULES[4]]
    text = "Peste 199 lei transportul e gratuit, iar la a doua comandă pragul scade la 149 lei."
    assert quoted_rules(text, dup, "ro") == [RULES[4]]


# --- handle-urile din proză --------------------------------------------------------------------

PRODUCTS = [
    {
        "id": "a",
        "name": "REAL BARRIER Peach Fit Tone Up Sun Cream SPF 50+ PA++++ 50 ml, crema de fata "
        "formulata cu oxid de zinc si niacinamida, care contribuie la protectia solara",
    },
    {"id": "b", "name": "BEAUTY OF JOSEON Daily Tinted Fluid Sunscreen SPF30 PA+++"},
]
HANDLES = {"P1": "a", "P2": "b"}


def test_handles_in_prose_become_the_card_names():
    j = {
        "intro": "Pentru tenul gras, P1 are SPF 50, iar P2 e nuanțatoare.",
        "education": "Dacă te preocupă porii, P1 e alegerea potrivită.",
        "items": [{"product_id": "P1", "fit_clause": "Mai lejeră decât P2."}],
        "pick": {"product_id": "P2", "justification": "P2 lasă un finish natural."},
    }
    out = _resolve_handles(j, HANDLES, _handle_names(PRODUCTS, HANDLES))
    assert "P1" not in out["intro"] and "P2" not in out["intro"]
    assert out["intro"].startswith("Pentru tenul gras, REAL BARRIER Peach Fit Tone Up Sun Cream")
    assert "crema de fata formulata" not in out["intro"]  # numele scurt de pe card, nu cel lung
    assert out["items"][0]["product_id"] == "a"
    assert "BEAUTY OF JOSEON" in out["items"][0]["fit_clause"]
    assert out["pick"]["product_id"] == "b" and out["pick"]["justification"].startswith("BEAUTY")
    assert j["intro"].startswith("Pentru tenul gras, P1")  # copie, nu mutație (diagnoza brută)


def test_unknown_handles_and_lookalikes_stay():
    j = {"intro": "P9 nu există, iar SPF 50 și PA++++ nu sunt handle-uri.", "items": []}
    out = _resolve_handles(j, HANDLES, _handle_names(PRODUCTS, HANDLES))
    assert out["intro"] == j["intro"]


def test_without_names_the_old_behaviour_holds():
    j = {"intro": "P1 e bună.", "items": [{"product_id": "P1"}]}
    out = _resolve_handles(j, HANDLES)
    assert out["intro"] == "P1 e bună." and out["items"][0]["product_id"] == "a"


# --- recenzia adversarială -----------------------------------------------------------------------

from types import SimpleNamespace  # noqa: E402

from src.agent import planner  # noqa: E402
from src.agent.finalize import render  # noqa: E402
from src.agent.planner import ResponsePlan  # noqa: E402
from src.agent.prompt_builder import PromptInputs  # noqa: E402
from src.agent.store_rules import match_rules, names_any  # noqa: E402
from src.config import get_settings  # noqa: E402
from src.models import (  # noqa: E402
    BusinessConfig,
    Contact,
    ConversationState,
    InboundMessage,
    ProductRef,
    TurnContext,
)
from src.worker.runner import PipelineDeps  # noqa: E402

#: Întrebările reale ale regulilor de mai sus (`db/seed/faqs_sole_ro.json`).
QUESTIONS = {
    RULES[0]: "Cât timp am la dispoziție pentru retur?",
    RULES[1]: "Cum returnez un produs?",
    RULES[2]: "Cât costă returul?",
    RULES[3]: "Pot returna un produs desfăcut?",
    RULES[4]: "Cât costă livrarea?",
}
C14_CLIENT = "daca nu-mi place o crema o pot returna?"
MIXED_CLIENT = "aveti crema cerave si cat costa livrarea?"
MIXED_TEXT = (
    "Nu am găsit crema CeraVe în catalog, dar îți pot arăta alte creme hidratante pentru tenul "
    "uscat. Livrarea costă între 19,90 și 24,90 lei, cu TVA inclus."
)


def test_m3_a_rule_the_client_did_not_ask_about_is_not_served():
    text = C14_MODEL_TEXT + " Livrarea costă între 19,90 și 24,90 lei, cu TVA inclus."
    assert quoted_rules(text, RULES, "ro", client=C14_CLIENT, questions=QUESTIONS) == [
        RULES[1],
        RULES[3],
    ]
    assert quoted_rules(C14_MODEL_TEXT, RULES, "ro", client="buna ziua", questions=QUESTIONS) == []


def test_m1_a_mixed_answer_is_not_complete():
    m = match_rules(MIXED_TEXT, RULES, "ro", client=MIXED_CLIENT, questions=QUESTIONS)
    assert m.rules == (RULES[4],) and not m.complete and m.unmapped == 1
    full = match_rules(C14_MODEL_TEXT, RULES, "ro", client=C14_CLIENT, questions=QUESTIONS)
    assert full.complete


def _ctx(body: str) -> TurnContext:
    ctx = TurnContext(
        turn_id="t",
        business=BusinessConfig(id="biz-1", slug="s", name="n", default_locale="ro"),
        contact=Contact(id="c", business_id="biz-1"),
        message=InboundMessage(provider_msg_id="m", body=body),
        conversation_id="conv",
        state=ConversationState(),
    )
    ctx.language = "ro"
    return ctx


class _LLM:
    async def complete(self, system, user, *, model=None):
        return ""


def _plan(final: str, query: str) -> ResponsePlan:
    return ResponsePlan(
        handled=False,
        products=[],
        final=final,
        is_order=False,
        query=query,
        history="",
        inp=PromptInputs.build("D", "ecommerce", "ro", ["Ten"], []),
        mode="prose",
        grounded_sources=list(RULES),
        grounded_questions=dict(QUESTIONS),
    )


async def test_m1_mixed_question_keeps_the_no_result_after_the_rules():
    ctx = _ctx(MIXED_CLIENT)
    await render(ctx, PipelineDeps(conn=object(), redis=None, llm=_LLM()), _plan(MIXED_TEXT, ""))
    text = ctx.reply.text
    assert text.startswith(RULES[4])
    assert "n-am găsit produse" in text  # jumătatea de produs nu dispare tăcut
    assert ctx.reply.cacheable is False


async def test_m1_a_pure_rule_answer_is_the_rules_alone():
    ctx = _ctx(C14_CLIENT)
    await render(
        ctx, PipelineDeps(conn=object(), redis=None, llm=_LLM()), _plan(C14_MODEL_TEXT, "")
    )
    assert ctx.reply.text == f"{RULES[1]} {RULES[3]}"


def _r3_ctx() -> TurnContext:
    ctx = _ctx("care e mai ieftina dintre ele si cat costa livrarea?")
    ctx.state.displayed_products = [
        ProductRef(product_id="a", name=PRODUCTS[0]["name"], price=80.0),
        ProductRef(product_id="b", name=PRODUCTS[1]["name"], price=60.0),
    ]
    return ctx


def test_m2_r3_still_runs_when_the_prose_names_a_displayed_product():
    run = SimpleNamespace(grounded_sources=[RULES[4]])
    named = "Cea mai ieftină e BEAUTY OF JOSEON Daily Tinted, la 60 lei. Livrarea costă 19,90 lei."
    assert planner._store_rules_turn(_r3_ctx(), run, named) is False
    store = "Livrarea costă între 19,90 și 24,90 lei, cu TVA inclus."
    assert planner._store_rules_turn(_r3_ctx(), run, store) is True


def test_m4_r3_follows_the_faq_grounding_kill_switch(monkeypatch):
    run = SimpleNamespace(grounded_sources=[RULES[4]])
    store = "Livrarea costă între 19,90 și 24,90 lei, cu TVA inclus."
    monkeypatch.setattr(get_settings(), "faq_grounding_enabled", False)
    assert planner._store_rules_turn(_r3_ctx(), run, store) is False


def test_names_any_needs_a_unique_prefix():
    names = [p["name"] for p in PRODUCTS]
    assert names_any("Îți recomand REAL BARRIER, e lejeră.", names, "ro")
    assert not names_any("Livrarea costă 19,90 lei.", names, "ro")


_W = ["alfaa", "betaa", "gamaa", "deltaa", "epsil", "zetaa", "etaaa", "theta", "iotaa", "kappa"]


def test_low_a_margin_of_exactly_one_tenth_is_enough():
    rule_a = " ".join(_W[:6]) + "."
    rule_b = " ".join(_W[5:]) + "."
    assert quoted_rules(" ".join(_W) + ".", [rule_a, rule_b], None) == [rule_a]


def test_low_a_rule_contained_in_a_chosen_one_is_not_repeated():
    big = "alfaa betaa gamaa deltaa epsil zetaa."
    small = "alfaa betaa gamaa deltaa omega."
    text = "alfaa betaa gamaa deltaa epsil zetaa. alfaa betaa omega deltaa."
    assert quoted_rules(text, [big, small], None) == [big]


def test_low_lines_and_bullets_split_sentences():
    text = (
        "Livrarea costă între 19,90 și 24,90 lei cu TVA inclus\n"
        "- Returul îl soliciți în 30 de zile de la cumpărare pe www.service-return.com"
    )
    assert quoted_rules(text, RULES, "ro") == [RULES[4], RULES[1]]


def test_low_handles_in_question_and_suggestions():
    j = {
        "intro": "Uite.",
        "question": "Preferi P1 sau P2?",
        "suggestions": ["Compară P1 cu P2", "Arată-mi P3", "Altceva"],
        "items": [],
    }
    handles = {**HANDLES, "P3": "zzz"}  # P3 n-are produs cunoscut, deci nici nume
    out = _resolve_handles(j, handles, _handle_names(PRODUCTS, handles))
    assert "P1" not in out["question"] and "REAL BARRIER" in out["question"]
    assert out["suggestions"][0].startswith("Compară REAL BARRIER")
    assert len(out["suggestions"]) == 2 and out["suggestions"][1] == "Altceva"


async def test_m3_faq_tool_carries_each_answer_question(monkeypatch):
    from src.agent import tool_executor
    from src.agent.tool_executor import ToolRun
    from src.tools import faq_tools as ft

    rows = [{"id": "f", "question": " Cât costă livrarea? ", "answer": RULES[4]}]

    async def fake(conn, bid, locale, *, limit):
        return list(rows)

    monkeypatch.setattr(ft, "list_active", fake)
    deps = PipelineDeps(conn=object(), redis=None, llm=_LLM())
    res = await ft.faq_lookup_tool(_ctx("x"), deps, {"query": "livrare"})
    assert res.sources == [RULES[4]]
    assert res.source_questions == {RULES[4]: "Cât costă livrarea?"}

    async def fake_run_tool(ctx, deps, name, args):
        return res

    monkeypatch.setattr(tool_executor, "run_tool", fake_run_tool)
    run = ToolRun(_ctx("x"), deps)
    await run._execute_serialized("faq_lookup", {"query": "livrare"})
    assert run.grounded_questions == {RULES[4]: "Cât costă livrarea?"}
