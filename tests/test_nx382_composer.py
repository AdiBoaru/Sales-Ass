"""NX-382 faza 1 — compozitorul unic scrie răspunsul pe `detail`, cu fișa întreagă și istoricul.

Contractul: codul dă SARCINA, OBLIGAȚIILE și FAPTELE; modelul scrie; poarta judecă pe fapte (nu pe
liste de cuvinte); orice eșec al modelului servește rezerva de azi (fișa), singurul text de cod."""

from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from src.agent import composer as cp
from src.agent import kernel_executors as kx
from src.config import get_settings
from src.conversation.interpretation import TurnPlan
from tests.kernel import stage_harness as sh
from tests.test_interpreted_turn_c import _ctx, _outcome, _planned

PRODUCT = {
    "id": "p1",
    "name": "COSRX The Retinol 0.1 Cream - crema de noapte cu retinol",
    "price": 142.0,
    "availability": "in_stock",
    "rating": 4.89,
    "review_count": 19,
    "attributes": {},
    "sections": [
        {"kind": "summary", "body": "Crema de noapte cu 0,1% retinol pur si pantenol."},
        {"kind": "usage", "body": "Se aplica seara, de 3 ori pe saptamana la inceput."},
    ],
    "top_pros": ["se absoarbe repede"],
}
OTHER = {**PRODUCT, "id": "p2", "name": "SOME BY MI Retinol Bakuchiol Dual Cream", "price": 140.0}
UNITS = frozenset({"%", "spf", "ml"})


def _reply(text="Da.", items=(), suggestions=(), met=(), advice=""):
    return {
        "text": text,
        "general_advice": advice,
        "items": [{"handle": h, "reason": r} for h, r in items],
        "suggestions": list(suggestions),
        "obligations_met": list(met),
    }


class ComposeLLM:
    def __init__(self, reply):
        self.reply = reply
        self.calls: list[tuple[str, str, dict]] = []

    async def complete_schema(self, system, user, schema, **kw):
        self.calls.append((system, user, schema))
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


# --- promptul și contractul ---------------------------------------------------------------------


def test_the_system_prompt_is_the_core_one_task_block_and_the_voice_rules():
    system = cp.system_prompt("detail", store="SOLE", locale="ro")
    for anchor in (
        'online store "SOLE"',
        'Write in locale "ro"',
        "HOW TO HELP",
        "Read HISTORY before you write",
        "General know-how about using products",
        "no colleague or human operator",
        "TASK detail:",
        "Voce:",
    ):
        assert anchor in system, anchor
    assert "TASK recommend:" not in system


def test_every_task_has_a_block():
    assert set(cp.TASKS) == set(cp.TaskKind.__args__)


def test_the_user_message_carries_task_question_obligations_facts_history_and_message():
    inp = cp.ComposeInput(
        task="detail",
        products=[PRODUCT],
        question="merge cu vitamina c?",
        obligations=[cp.Obligation("need_unverifiable", {"need": "fără parfum"})],
        offered_moves=["get the link"],
    )
    facts = cp.facts_block(inp, None, "ro")
    user = cp.user_message(inp, facts=facts, history="Client: salut", message="si cu vitamina c?")
    assert "TASK: detail" in user and "QUESTION: merge cu vitamina c?" in user
    assert '- need_unverifiable {"need": "fără parfum"}' in user
    assert "OFFERED NEXT STEPS\n- get the link" in user
    assert "PRODUCT P1\nname: COSRX The Retinol 0.1 Cream" in user and "0,1% retinol" in user
    assert "HISTORY (context only, not a source of facts)\nClient: salut" in user and user.endswith(
        "CUSTOMER MESSAGE\nsi cu vitamina c?"
    )


def test_the_schema_is_strict_with_closed_handles_and_obligation_codes():
    inp = cp.ComposeInput(
        task="recommend", products=[PRODUCT, OTHER], obligations=[cp.Obligation("ask")]
    )
    s = cp.schema(inp)
    assert s["name"] == "composer_reply" and s["strict"] is True
    body = s["schema"]
    assert body["required"] == [
        "text",
        "general_advice",
        "items",
        "suggestions",
        "obligations_met",
    ]
    assert body["properties"]["items"]["items"]["properties"]["handle"]["enum"] == ["P1", "P2"]
    assert body["properties"]["obligations_met"]["items"]["enum"] == ["ask"]
    empty = cp.schema(cp.ComposeInput(task="chitchat"))["schema"]
    assert "enum" not in empty["properties"]["items"]["items"]["properties"]["handle"]


# --- parsarea și poarta --------------------------------------------------------------------------


def _check(reply, inp):
    return _checked(reply, inp)[0]


def _checked(reply, inp):
    composed = cp.parse(reply, inp)
    facts = cp.facts_block(inp, None, "ro")
    return cp.check(composed, inp, facts=facts, units=UNITS)


def test_a_grounded_reply_passes_and_handles_become_ids():
    inp = cp.ComposeInput(task="recommend", products=[PRODUCT, OTHER], offered_moves=["a"] * 9)
    reply = _reply(
        "Am două creme cu retinol, prima are 0,1% retinol.",
        items=[
            ("P1", "Se aplică seara, de 3 ori pe săptămână la început."),
            ("P2", "Costă 140 lei."),
        ],
        suggestions=["a", "b", "c", "d", "e", "f"],
    )
    composed = cp.parse(reply, inp)
    assert composed.items == (
        ("p1", "Se aplică seara, de 3 ori pe săptămână la început."),
        ("p2", "Costă 140 lei."),
    )
    assert len(composed.suggestions) == cp.MAX_SUGGESTIONS
    assert _check(reply, inp).ok


@pytest.mark.parametrize(
    ("reply", "reason"),
    [
        (_reply("Are 2% retinol."), "ungrounded_number"),
        (_reply("Costă 99 lei."), "ungrounded_price"),
        (_reply("Da.", items=[("P9", "x")]), "unknown_handle"),
        (_reply("Da.", items=[("P1", "Are SPF 50.")]), "ungrounded_number"),
        (_reply("Vezi https://evil.test"), "invented_link"),
    ],
)
def test_an_ungrounded_reply_is_rejected(reply, reason):
    inp = cp.ComposeInput(task="recommend", products=[PRODUCT, OTHER])
    assert _check(reply, inp).reason == reason


def test_a_required_obligation_must_be_declared_covered():
    inp = cp.ComposeInput(
        task="detail", products=[PRODUCT], obligations=[cp.Obligation("need_unverifiable")]
    )
    assert _check(_reply("Da."), inp).reason == "obligation_missing"
    assert _check(_reply("Da.", met=["need_unverifiable"]), inp).ok


def test_a_malformed_reply_is_not_parsed():
    assert cp.parse({"items": []}, cp.ComposeInput(task="detail")) is None
    assert cp.parse("text", cp.ComposeInput(task="detail")) is None


# --- executorul de detaliu ----------------------------------------------------------------------


@pytest.fixture
def electronics(monkeypatch):
    cat = sh.catalog("electronics")
    sh.install(monkeypatch, cat, executors=True)
    monkeypatch.setattr(get_settings(), "composer_detail_enabled", True)
    return cat


@pytest.fixture
def catalog(monkeypatch):
    async def by_ids(conn, business_id, ids, limit=6):
        return [dict(PRODUCT)] if "p1" in ids else []

    monkeypatch.setattr(kx, "get_products_by_ids", by_ids)
    sheet: list[str] = []

    async def details(ctx, deps, pid, *, lead=None, gated=None):
        sheet.append(pid)
        first = lead(gated[0]) if lead and gated else None
        text = f"{first}\n\nFISA STANDARD" if first else "FISA STANDARD"
        ctx.set_reply(text, cacheable=False)

    monkeypatch.setattr(kx.det, "serve_details", details)
    return sheet


def _detail(question: str | None) -> TurnPlan:
    return TurnPlan(
        executor="detail", product_ids=["p1"], search_args=None, depends_on=None, question=question
    )


def _deps(llm):
    return NS(db=sh.RecordingDb(), llm=llm)


@pytest.mark.parametrize("question", ["merge cu vitamina c?", None])
async def test_a_detail_is_written_by_the_composer_with_or_without_a_question(
    electronics, catalog, question
):
    llm = ComposeLLM(
        _reply(
            "Da, doar că nu în același moment: retinolul seara, vitamina C dimineața.",
            suggestions=["Ce spun clienții despre ea?"],
        )
    )
    ctx = _ctx(electronics, "si cu vitamina c?")
    served = await kx.execute_read_plans(ctx, _deps(llm), _planned(_detail(question)), _outcome())
    assert served is True and catalog == []
    assert ctx.reply.text.startswith("Da, doar că nu în același moment")
    assert [p["product_id"] for p in ctx.reply.products] == ["p1"]
    assert ctx.reply.suggestions == ["Ce spun clienții despre ea?"]
    system, user, schema = llm.calls[0]
    assert "TASK detail:" in system and schema["name"] == "composer_reply"
    assert ("QUESTION: merge cu vitamina c?" in user) == (question is not None)
    assert "PRODUCT P1" in user and "CUSTOMER MESSAGE\nsi cu vitamina c?" in user
    [event] = [e.properties for e in ctx.events if e.type == "composer"]
    assert event["outcome"] == "composed" and event["task"] == "detail"


@pytest.mark.parametrize(
    ("reply", "reason"),
    [(_reply("Are 2% retinol."), "ungrounded_number"), (RuntimeError("down"), "call_failed")],
)
async def test_a_failed_composition_serves_todays_sheet(electronics, catalog, reply, reason):
    ctx = _ctx(electronics, "ce concentratie are?")
    plan = _planned(_detail("ce concentratie are?"))
    await kx.execute_read_plans(ctx, _deps(ComposeLLM(reply)), plan, _outcome())
    assert catalog == ["p1"] and ctx.reply.text == "FISA STANDARD"
    [event] = [e.properties for e in ctx.events if e.type == "composer"]
    assert (
        event.get("reason") == reason if event["outcome"] == "rejected" else reason == "call_failed"
    )


async def test_a_medical_rejection_serves_the_referral_before_the_sheet(electronics, catalog):
    from src.safety.messages import refer_sentence

    llm = ComposeLLM(_reply("Fișa nu spune dacă e fără alergeni."))
    ctx = _ctx(electronics, "are alergeni?")
    await kx.execute_read_plans(ctx, _deps(llm), _planned(_detail("are alergeni?")), _outcome())
    assert ctx.reply.text.startswith(refer_sentence("ro")) and "FISA STANDARD" in ctx.reply.text


async def test_with_the_flag_off_the_composer_is_not_called(electronics, catalog, monkeypatch):
    monkeypatch.setattr(get_settings(), "composer_detail_enabled", False)
    llm = ComposeLLM(_reply("Da."))
    ctx = _ctx(electronics, "detalii")
    await kx.execute_read_plans(ctx, _deps(llm), _planned(_detail(None)), _outcome())
    assert llm.calls == [] and catalog == ["p1"]


async def test_the_history_reaches_the_composer(electronics, catalog):
    llm = ComposeLLM(_reply("Da."))
    ctx = _ctx(electronics, "si cu vitamina c?")
    from src.models import Author, Direction, Message

    ctx.history = [
        Message(direction=Direction.INBOUND, author=Author.CONTACT, body="ce concentratie are?"),
        Message(direction=Direction.OUTBOUND, author=Author.BOT, body="Are 0,1% retinol pur."),
        *ctx.history,
    ]
    await kx.execute_read_plans(ctx, _deps(llm), _planned(_detail("si cu vit c?")), _outcome())
    user = llm.calls[0][1]
    assert "Client: ce concentratie are?" in user and "Asistent: Are 0,1% retinol pur." in user


async def test_end_to_end_the_composer_answers_through_the_real_stage(monkeypatch):
    """Lanțul întreg: interpretarea (transport fals) cu `question`, resolverul, plannerul,
    executorul, compozitorul (un apel `composer_reply`, scopul `compose` în `per_call`)."""
    from src.conversation.interpretation import TurnInterpretation
    from src.conversation.state_v2 import ConversationStateV2, DisplayedRef, References

    cat = sh.catalog("electronics")
    sh.install(monkeypatch, cat, executors=True)
    monkeypatch.setattr(get_settings(), "composer_detail_enabled", True)
    ids = list(cat.items)[:3]
    shown = tuple(DisplayedRef(p, cat.items[p]["name"], float(cat.items[p]["price"])) for p in ids)
    interp = TurnInterpretation.model_validate(
        {
            "thread": "continue",
            "acts": [
                {"kind": "detail", "targets": ["r1"], "query": None, "question": "e rezistent?"}
            ],
            "changes": [],
            "references": [
                {
                    "id": "r1",
                    "text": "primul",
                    "kind": "ordinal",
                    "ordinal": 1,
                    "name": None,
                    "dimension": None,
                    "value": None,
                    "direction": None,
                }
            ],
            "ambiguities": [],
            "corrects_previous_turn": False,
        }
    )

    class LLM(sh.StageLLM):
        composed: list[str] = []

        async def complete_schema(self, system, user, schema, **kw):
            self.composed.append(user)
            return _reply("Fișa nu spune dacă e rezistent la apă.", suggestions=["Vezi linkul"])

    llm = LLM(interp)
    state = ConversationStateV2(references=References(displayed_products=shown))
    ctx = sh.build_ctx(cat, state, "primul e rezistent?")
    run = await sh.run_turn(monkeypatch, cat, ctx, llm)
    assert run.branch_result is True
    assert ctx.reply.text == "Fișa nu spune dacă e rezistent la apă."
    assert ctx.reply.suggestions == ["Vezi linkul"]
    assert len(llm.composed) == 1 and "QUESTION: e rezistent?" in llm.composed[0]
    [turn] = [e.properties for e in ctx.events if e.type == "kernel_turn"]
    assert turn["served"] is True and turn["executor"] == "detail"


# --- recenzia adversarială a fazei 1 -------------------------------------------------------------


def test_the_voucher_and_list_prices_from_the_facts_are_grounded():
    product = {**PRODUCT, "coupon_code": "WELCOME15", "coupon_price": 120.7, "list_price": 165.0}
    inp = cp.ComposeInput(task="detail", products=[product])
    reply = _reply("Costă 142 lei, iar cu voucherul WELCOME15 ajunge la 120,70 lei, de la 165 lei.")
    assert _check(reply, inp).ok


def test_general_advice_may_carry_numbers_but_never_a_product_a_price_or_a_link():
    inp = cp.ComposeInput(task="detail", products=[PRODUCT])
    verdict, cleaned = _checked(
        _reply("Se aplică seara.", advice="Dimineața pune un SPF 30 și așteaptă 20 de minute."),
        inp,
    )
    assert verdict.ok and cleaned.advice.startswith("Dimineața pune un SPF 30")
    assert cleaned.served.endswith("așteaptă 20 de minute.")
    for bad in ("Costă 99 lei.", "Vezi https://x.test", "COSRX The Retinol 0.1 Cream e ideală."):
        verdict, cleaned = _checked(_reply("Se aplică seara.", advice=bad), inp)
        assert verdict.ok and cleaned.advice == "", bad


def test_a_number_from_general_knowledge_in_the_text_is_still_rejected():
    """Faptele produsului rămân stricte: sfatul cu cifre are locul lui, `general_advice`."""
    inp = cp.ComposeInput(task="detail", products=[PRODUCT])
    assert _check(_reply("Pune un SPF 30 dimineața."), inp).reason == "ungrounded_number"


def test_a_medical_sentence_is_dropped_alone_and_the_rest_is_served():
    """Recenzia: fișa unui produs pentru acnee nu pierde tot răspunsul (și nici nu primește o
    trimitere la medic) pentru o propoziție care sună a tratament."""
    inp = cp.ComposeInput(task="detail", products=[PRODUCT])
    verdict, cleaned = _checked(
        _reply("Se aplică seara. Tratează acneea în 7 zile. Se absoarbe repede."), inp
    )
    assert verdict.ok and cleaned.reply == "Se aplică seara. Se absoarbe repede."
    only = _check(_reply("Tratează acneea."), inp)
    assert only.reason == "medical_claim"


def test_sentences_do_not_split_after_a_short_name_abbreviation():
    assert cp.sentences("Crema DR. Jart+ e ușoară. Se aplică seara.") == [
        "Crema DR. Jart+ e ușoară.",
        "Se aplică seara.",
    ]


def test_chips_are_capped_to_the_offered_steps_and_naturalized():
    inp = cp.ComposeInput(task="detail", products=[PRODUCT], offered_moves=["link", "reviews"])
    composed = cp.parse(_reply(suggestions=["Vezi linkul — acum", "b", "c", "d"]), inp)
    assert len(composed.suggestions) == 2 and "—" not in composed.suggestions[0]
    assert cp.parse(_reply(suggestions=["x"]), cp.ComposeInput(task="detail")).suggestions == ()


def test_duplicate_obligation_codes_give_one_enum_value():
    inp = cp.ComposeInput(task="ask", obligations=[cp.Obligation("ask"), cp.Obligation("ask")])
    assert cp.schema(inp)["schema"]["properties"]["obligations_met"]["items"]["enum"] == ["ask"]


async def test_no_referral_lead_without_a_question(electronics, catalog):
    """Recenzia: un detaliu fără întrebare, respins pe medical, primește fișa, fără trimitere."""
    llm = ComposeLLM(_reply("Tratează acneea."))
    ctx = _ctx(electronics, "detalii")
    await kx.execute_read_plans(ctx, _deps(llm), _planned(_detail(None)), _outcome())
    assert ctx.reply.text == "FISA STANDARD"


# --- faza 2: regulile magazinului ----------------------------------------------------------------

RULES = [
    {"question": "Cat costa livrarea?", "answer": "Livrarea costa 15 lei, gratuita peste 149 lei."},
    {"question": "Pot returna?", "answer": "Poti returna in 30 de zile de la primire."},
]


@pytest.fixture
def store(monkeypatch, electronics):
    from src.tools import faq_tools

    monkeypatch.setattr(get_settings(), "composer_store_info_enabled", True)

    async def rules(ctx, deps):
        return [dict(r) for r in RULES]

    monkeypatch.setattr(faq_tools, "load_rules", rules)
    loops: list[frozenset[str]] = []

    async def delegate(ctx, deps, allowed):
        loops.append(allowed)
        ctx.set_reply("BUCLA DE AZI", cacheable=False)
        return True

    monkeypatch.setattr(kx, "_delegate", delegate)
    return loops


def _faq_plan() -> TurnPlan:
    return TurnPlan(executor="faq", product_ids=[], search_args=None, depends_on=None)


async def test_a_store_question_is_answered_from_the_rules_in_the_models_words(store, electronics):
    llm = ComposeLLM(_reply("Peste 149 lei livrarea e gratuită, altfel costă 15 lei."))
    ctx = _ctx(electronics, "cat e transportul?")
    assert await kx.execute_read_plans(ctx, _deps(llm), _planned(_faq_plan()), _outcome())
    assert store == [] and ctx.reply.text.startswith("Peste 149 lei")
    assert ctx.reply.cacheable is False
    assert ctx.retrieval.store_only and not ctx.retrieval.catalog_read
    system, user, _ = llm.calls[0]
    assert "TASK store_info:" in system
    assert "STORE RULES\n- Cat costa livrarea? -> Livrarea costa 15 lei" in user


@pytest.mark.parametrize(
    "reply",
    [
        _reply("Livrarea e gratuită peste 99 lei."),  # sumă care nu e în reguli
        _reply("Poți returna în 60 de zile."),  # cifră care nu e în reguli
        RuntimeError("down"),
    ],
)
async def test_an_ungrounded_store_answer_falls_back_to_todays_loop(store, electronics, reply):
    ctx = _ctx(electronics, "cat e transportul?")
    await kx.execute_read_plans(ctx, _deps(ComposeLLM(reply)), _planned(_faq_plan()), _outcome())
    assert store and ctx.reply.text == "BUCLA DE AZI"


async def test_without_rules_or_with_the_flag_off_todays_loop_answers(
    store, electronics, monkeypatch
):
    from src.tools import faq_tools

    async def none(ctx, deps):
        return []

    monkeypatch.setattr(faq_tools, "load_rules", none)
    llm = ComposeLLM(_reply("Da."))
    await kx.execute_read_plans(
        _ctx(electronics, "x"), _deps(llm), _planned(_faq_plan()), _outcome()
    )
    monkeypatch.setattr(get_settings(), "composer_store_info_enabled", False)
    await kx.execute_read_plans(
        _ctx(electronics, "x"), _deps(llm), _planned(_faq_plan()), _outcome()
    )
    assert llm.calls == [] and len(store) == 2


def test_rule_prices_are_grounded_for_the_store_answer():
    assert cp.rule_prices(["Livrarea costa 15 lei, gratuita peste 149 lei."]) == {15.0, 149.0}


# --- faza 2: întrebarea porții și „n-am găsit" ----------------------------------------------------


def _gate_ask(question="La care te referi dintre A și B?"):
    from src.conversation.ambiguity_gate import GateOutcome
    from src.conversation.interpretation import AmbiguityDecision

    return GateOutcome(
        decision=AmbiguityDecision(verdict="must_ask", reason="x", question=question),
        asked_key="ref:abc",
        asked_kind="pending",
    )


async def test_the_gate_question_is_phrased_by_the_composer_and_stays_pending(
    electronics, catalog, monkeypatch
):
    monkeypatch.setattr(get_settings(), "composer_ask_enabled", True)
    llm = ComposeLLM(_reply("Pe care o vrei, crema COSRX sau cea SOME BY MI?", met=["ask"]))
    plan = TurnPlan(executor="ask", product_ids=["p1"], search_args=None, depends_on=None)
    ctx = _ctx(electronics, "o iau")
    assert await kx.execute_read_plans(ctx, _deps(llm), _planned(plan), _gate_ask())
    assert ctx.reply.text.startswith("Pe care o vrei")
    assert ctx.reply.pending_question is not None  # memoria întrebării (I11) rămâne
    assert [p["product_id"] for p in ctx.reply.products] == ["p1"]
    user = llm.calls[0][1]
    assert '- ask {"question_to_ask": "La care te referi dintre A și B?"}' in user


async def test_the_gate_question_falls_back_to_the_template(electronics, catalog, monkeypatch):
    monkeypatch.setattr(get_settings(), "composer_ask_enabled", True)
    llm = ComposeLLM(_reply("Pe care o vrei?"))  # obligația `ask` nedeclarată ⇒ respins
    plan = TurnPlan(executor="ask", product_ids=["p1"], search_args=None, depends_on=None)
    ctx = _ctx(electronics, "o iau")
    await kx.execute_read_plans(ctx, _deps(llm), _planned(plan), _gate_ask())
    assert ctx.reply.text == "La care te referi dintre A și B?"


async def test_nothing_found_is_said_concretely_by_the_composer(electronics, monkeypatch):
    from src.tools import catalog_tools
    from src.tools.base import ToolResult
    from src.tools.catalog_tools import SearchArgs

    monkeypatch.setattr(get_settings(), "composer_no_results_enabled", True)

    async def empty(ctx, deps, args, **kw):
        return ToolResult(ok=True, products=[])

    monkeypatch.setattr(catalog_tools, "run_planned_search", empty)
    llm = ComposeLLM(
        _reply("N-am găsit un ser sub 100 lei pentru pete, dar am peste.", met=["nothing_found"])
    )
    plan = TurnPlan(
        executor="search",
        product_ids=[],
        search_args=SearchArgs(query="ser pete", price_max=100),
        depends_on=None,
    )
    ctx = _ctx(electronics, "un ser pentru pete sub 100")
    assert await kx.execute_read_plans(ctx, _deps(llm), _planned(plan), _outcome())
    assert ctx.reply.text.startswith("N-am găsit un ser sub 100 lei")
    user = llm.calls[0][1]
    assert '"words": "ser pete"' in user and '"price_max": 100' in user
