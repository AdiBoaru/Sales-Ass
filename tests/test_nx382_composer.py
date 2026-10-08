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


def _reply(text="Da.", items=(), suggestions=(), met=()):
    return {
        "text": text,
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
    assert "HISTORY\nClient: salut" in user and user.endswith("CUSTOMER MESSAGE\nsi cu vitamina c?")


def test_the_schema_is_strict_with_closed_handles_and_obligation_codes():
    inp = cp.ComposeInput(
        task="recommend", products=[PRODUCT, OTHER], obligations=[cp.Obligation("ask")]
    )
    s = cp.schema(inp)
    assert s["name"] == "composer_reply" and s["strict"] is True
    body = s["schema"]
    assert body["required"] == ["text", "items", "suggestions", "obligations_met"]
    assert body["properties"]["items"]["items"]["properties"]["handle"]["enum"] == ["P1", "P2"]
    assert body["properties"]["obligations_met"]["items"]["enum"] == ["ask"]
    empty = cp.schema(cp.ComposeInput(task="chitchat"))["schema"]
    assert "enum" not in empty["properties"]["items"]["items"]["properties"]["handle"]


# --- parsarea și poarta --------------------------------------------------------------------------


def _check(reply, inp):
    composed = cp.parse(reply, inp)
    facts = cp.facts_block(inp, None, "ro")
    return cp.check(composed, inp, facts=facts, units=UNITS)


def test_a_grounded_reply_passes_and_handles_become_ids():
    inp = cp.ComposeInput(task="recommend", products=[PRODUCT, OTHER])
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
