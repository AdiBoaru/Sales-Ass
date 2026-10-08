"""NX-382 faza 2c — coșul, chitchat-ul și fraza de siguranță, scrise de compozitor.

Contractul fazei:
- coșul: mutația rulează întâi (neschimbată), apoi compozitorul confirmă ce s-a adăugat (obligația
  `cart_change`) și alege dintre complementare doar ce completează, sau nimic; orice eșec lasă
  textul de azi, niciodată o a doua scriere;
- chitchat: un tur DOAR `chitchat` (`reply_only`) e răspuns de compozitor, nu de bucla v1;
- siguranța: pe un context declarat, trimiterea la medic sau farmacist e o obligație verificată
  (amprenta NX-173, după scoaterea propozițiilor medicale), iar `enforce` nu mai pune fraza codului
  peste ea; registrul indisponibil și setul golit de excludere rămân ale codului (P0).

Zero model real, zero DB."""

from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from src.agent import composer as cp
from src.agent import interpreted_turn as it
from src.agent import kernel_executors as kx
from src.agent.turn_planner import PlannedTurn
from src.config import get_settings
from src.conversation.interpretation import TurnPlan
from src.models import Author, Direction, Message, Reply
from src.safety import compose as safety_compose
from src.safety.contraindications import Block
from src.safety.policy import Decision
from src.worker.text_scrub import has_medical_claim
from tests.test_interpreted_turn_d import _ctx, _deps, _outcome, cart_tool, electronics  # noqa: F401

REFERRAL = (
    "Țin cont că ești însărcinată, așa că am lăsat deoparte opțiunile cu retinoizi, iar alegerea "
    "e bine s-o verifici cu medicul sau farmacistul."
)


def _reply(text="Da.", items=(), met=(), advice="", suggestions=()):
    return {
        "text": text,
        "general_advice": advice,
        "items": [{"handle": h, "reason": r} for h, r in items],
        "suggestions": list(suggestions),
        "obligations_met": list(met),
    }


class ComposeLLM:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls: list[tuple[str, str, dict]] = []

    async def complete_schema(self, system, user, schema, **kw):
        self.calls.append((system, user, schema))
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, Exception):
            raise reply
        return reply


def _cart(ids=("el-01",)) -> PlannedTurn:
    plan = TurnPlan(executor="cart", product_ids=list(ids), search_args=None, depends_on=None)
    return PlannedTurn(plans=(plan,), primary=0)


def _reply_only() -> PlannedTurn:
    plan = TurnPlan(executor="reply_only", product_ids=[], search_args=None, depends_on=None)
    return PlannedTurn(plans=(plan,), primary=0)


@pytest.fixture
def complements(monkeypatch, electronics):  # noqa: F811
    rows: list[dict] = []
    from tests.kernel import stage_harness as sh

    rows.extend(sh.product_rows(electronics, ["el-03"]))

    async def fetch(ctx, deps, mutation):
        return list(rows)

    monkeypatch.setattr(kx, "_cart_complements", fetch)
    return rows


# --- coșul ----------------------------------------------------------------------------------------


async def test_the_cart_change_is_confirmed_by_the_composer_with_a_chosen_complement(
    electronics,  # noqa: F811
    cart_tool,  # noqa: F811
    complements,
):
    llm = ComposeLLM(
        _reply(
            "Am pus Samsung Phone 1 128 GB în coș.",
            items=[("P1", "Se potrivește cu telefonul pe care l-ai luat.")],
            met=["cart_change"],
        )
    )
    ctx = _ctx(electronics, "il iau")
    assert await kx.execute_read_plans(ctx, _deps(llm), _cart(), _outcome()) is True
    assert [c for c in cart_tool.calls if c[0] == "cart_add"] == [
        ("cart_add", {"product_id": "el-01"})
    ]
    assert ctx.reply.rich is not None and ctx.reply.rich.intro.startswith("Am pus Samsung")
    [item] = ctx.reply.rich.items
    assert item.product_id == "el-03" and item.reason.startswith("Se potrivește")
    assert ctx.reply.cacheable is False
    system, user, _ = llm.calls[0]
    assert "TASK cart:" in system
    assert '- cart_change {"added": ["Samsung Phone 1 128 GB"], "failed": 0}' in user
    assert "PRODUCT P1" in user, "complementarele sunt candidați, cu fișa lor"
    assert cart_tool.calls.count(("cross_sell", {})) == 0, "cross-sell-ul v1 nu mai rulează"


async def test_without_a_chosen_complement_the_cards_are_the_added_products(
    electronics,  # noqa: F811
    cart_tool,  # noqa: F811
    complements,
):
    llm = ComposeLLM(_reply("Gata, telefonul e în coș.", met=["cart_change"]))
    ctx = _ctx(electronics, "il iau")
    assert await kx.execute_read_plans(ctx, _deps(llm), _cart(), _outcome()) is True
    assert ctx.reply.text == "Gata, telefonul e în coș." and ctx.reply.rich is None
    assert [p["product_id"] for p in ctx.reply.products] == ["el-01"]
    assert ctx.retrieval.catalog_read is False


@pytest.mark.parametrize(
    "reply",
    [
        _reply("Gata, e în coș."),  # obligația nedeclarată
        _reply("Am pus în coș, costă 99 lei.", met=["cart_change"]),  # preț inventat
        RuntimeError("down"),
    ],
)
async def test_a_rejected_cart_reply_keeps_todays_sentence_and_writes_once(
    electronics,  # noqa: F811
    cart_tool,  # noqa: F811
    complements,
    reply,
):
    from tests.test_interpreted_turn_d import _added

    ctx = _ctx(electronics, "il iau")
    assert await kx.execute_read_plans(ctx, _deps(ComposeLLM(reply)), _cart(), _outcome())
    assert ctx.reply.text == _added(electronics, "el-01")
    assert [c[0] for c in cart_tool.calls].count("cart_add") == 1, "o singură scriere"


async def test_a_failed_add_is_told_without_complements(
    electronics,  # noqa: F811
    cart_tool,  # noqa: F811
    monkeypatch,
):
    cart_tool.fail = {"el-01"}
    llm = ComposeLLM(_reply("N-am reușit să-l pun în coș, mai încearcă.", met=["cart_change"]))
    ctx = _ctx(electronics, "il iau")
    assert await kx.execute_read_plans(ctx, _deps(llm), _cart(), _outcome())
    assert ctx.reply.text.startswith("N-am reușit")
    assert '"added": [], "failed": 1' in llm.calls[0][1]
    assert "PRODUCT P1" not in llm.calls[0][1], "fără complementare pe o adăugare picată"


async def test_the_cart_flag_off_is_todays_path(electronics, cart_tool, monkeypatch):  # noqa: F811
    from tests.test_interpreted_turn_d import _added

    monkeypatch.setattr(get_settings(), "composer_cart_enabled", False)
    llm = ComposeLLM(_reply("x", met=["cart_change"]))
    ctx = _ctx(electronics, "il iau")
    await kx.execute_read_plans(ctx, _deps(llm), _cart(), _outcome())
    assert llm.calls == [] and ctx.reply.text == _added(electronics, "el-01")


async def test_complements_that_fail_to_load_do_not_lose_the_confirmation(
    electronics,  # noqa: F811
    cart_tool,  # noqa: F811
    monkeypatch,
):
    async def broken(ctx, deps, mutation):
        raise RuntimeError("db")

    monkeypatch.setattr(kx, "_cart_complements", broken)
    llm = ComposeLLM(_reply("E în coș.", met=["cart_change"]))
    ctx = _ctx(electronics, "il iau")
    assert await kx.execute_read_plans(ctx, _deps(llm), _cart(), _outcome())
    assert ctx.reply.text == "E în coș."
    assert any(
        e.type == "composer" and e.properties.get("outcome") == "complements_failed"
        for e in ctx.events
    )


# --- chitchat -------------------------------------------------------------------------------------


async def test_a_chitchat_only_turn_is_answered_by_the_composer(electronics):  # noqa: F811
    llm = ComposeLLM(_reply("Cu plăcere, spor la folosit!"))
    ctx = _ctx(electronics, "mersi, pa")
    verdict = await kx.execute_read_plans(
        ctx, _deps(llm), _reply_only(), _outcome(), None, False, False, social=True
    )
    assert verdict is True and ctx.reply.text == "Cu plăcere, spor la folosit!"
    assert ctx.reply.cacheable is False and ctx.retrieval.catalog_read is False
    assert "TASK chitchat:" in llm.calls[0][0]


@pytest.mark.parametrize(
    "reply",
    [_reply("Avem 20% reducere azi!"), _reply("Avem pe stoc tot ce cauți."), RuntimeError("x")],
)
async def test_an_ungrounded_chitchat_reply_goes_to_the_v1_path(electronics, reply):  # noqa: F811
    ctx = _ctx(electronics, "salut")
    verdict = await kx.execute_read_plans(
        ctx, _deps(ComposeLLM(reply)), _reply_only(), _outcome(), None, False, False, social=True
    )
    assert verdict is None and ctx.reply is None


async def test_a_reply_only_that_is_not_social_stays_on_v1(electronics):  # noqa: F811
    llm = ComposeLLM(_reply("Salut!"))
    ctx = _ctx(electronics, "x")
    assert await kx.execute_read_plans(ctx, _deps(llm), _reply_only(), _outcome()) is None
    assert llm.calls == []


async def test_the_chitchat_flag_off_stays_on_v1(electronics, monkeypatch):  # noqa: F811
    monkeypatch.setattr(get_settings(), "composer_chitchat_enabled", False)
    llm = ComposeLLM(_reply("Salut!"))
    ctx = _ctx(electronics, "salut")
    verdict = await kx.execute_read_plans(
        ctx, _deps(llm), _reply_only(), _outcome(), None, False, False, social=True
    )
    assert verdict is None and llm.calls == []


def _chain(kinds, skipped=()):
    acts = [NS(kind=k) for k in kinds]
    return NS(
        interpreted=NS(interpretation=NS(acts=acts)),
        outcome=NS(skipped_acts=tuple(skipped)),
    )


@pytest.mark.parametrize(
    ("kinds", "skipped", "social"),
    [
        (["chitchat"], (), True),
        (["chitchat", "chitchat"], (), True),
        (["chitchat", "find"], (), False),
        (["chitchat", "cart"], (1,), False),  # coșul scos de poartă: refuzul e al NX-383
        ([], (), False),
    ],
)
def test_a_social_turn_is_only_chitchat_with_nothing_dropped(kinds, skipped, social):
    assert it._social_turn(_chain(kinds, skipped)) is social


# --- fraza de siguranță ---------------------------------------------------------------------------


def _pregnant(blocked: bool = True, unavailable: bool = False) -> Decision:
    blocks = [Block("x1", "pregnancy", "pregnancy-retinoids", "retinol")] if blocked else []
    return Decision(
        kept=[{"id": "p1"}],
        blocked=blocks,
        contexts=("pregnancy",),
        rule_ids=("pregnancy-retinoids",) if blocked else (),
        must_refer=True,
        unavailable=unavailable,
    )


def _sctx(decision, history=()):
    return NS(
        safety_decision=decision,
        language="ro",
        history=list(history),
        events=[],
        safety_referral_composed=None,
    )


def test_a_declared_context_becomes_the_safety_obligation_with_labels():
    ob = cp.safety_obligation(_sctx(_pregnant()))
    assert ob.code == cp.SAFETY_REFERRAL
    assert ob.facts == {
        "situation": ["ești însărcinată"],
        "left_out": ["opțiunile cu retinoizi"],
    }
    assert "left_out" not in cp.safety_obligation(_sctx(_pregnant(blocked=False))).facts


@pytest.mark.parametrize(
    "decision",
    [None, _pregnant(unavailable=True), Decision(kept=[], must_refer=False)],
)
def test_no_obligation_without_a_context_or_on_the_fail_closed_registry(decision):
    assert cp.safety_obligation(_sctx(decision)) is None


def test_already_told_only_with_the_short_reminder_setting(monkeypatch):
    told = Message(
        direction=Direction.OUTBOUND, author=Author.BOT, body="Verifică cu farmacistul."
    )
    assert "already_told" not in cp.safety_obligation(_sctx(_pregnant(), [told])).facts
    monkeypatch.setattr(get_settings(), "safety_referral_short_after_first", True)
    assert cp.safety_obligation(_sctx(_pregnant(), [told])).facts["already_told"] is True
    # cuvintele CLIENTULUI nu contează ca trimitere deja făcută
    said = Message(direction=Direction.INBOUND, author=Author.CONTACT, body="intreb farmacistul")
    assert "already_told" not in cp.safety_obligation(_sctx(_pregnant(), [said])).facts


def _checked(text, met=(cp.SAFETY_REFERRAL,)):
    inp = cp.ComposeInput(
        task="chitchat", obligations=[cp.Obligation(cp.SAFETY_REFERRAL, {"situation": ["x"]})]
    )
    return cp.check(cp.parse(_reply(text, met=met), inp), inp, facts="", units=frozenset())[0]


def test_the_referral_must_be_in_the_text():
    assert _checked(REFERRAL).ok
    assert _checked("Țin cont de situația ta.").reason == "safety_referral_missing"
    assert _checked(REFERRAL, met=()).reason == "obligation_missing"


def test_a_referral_that_claims_safety_is_dropped_and_then_missing():
    claim = "Crema e sigură în sarcină, dar întreabă farmacistul."
    assert has_medical_claim(claim)
    assert _checked(f"Salut. {claim}").reason == "safety_referral_missing"


@pytest.mark.parametrize(
    "text",
    [
        "Crema e sigura in sarcina.",
        "Crema e sigură în sarcină.",
        "E potrivită pentru femeile însărcinate.",
        "Se poate folosi și când alăptezi.",
    ],
)
def test_pregnancy_safety_claims_are_caught_in_every_gender(text):
    assert has_medical_claim(text)


def test_the_guaranteed_sentences_are_not_medical_claims():
    from src.safety import messages

    for blocked in (True, False):
        sentence = messages.safety_sentence(
            ["pregnancy"], ["pregnancy-retinoids"], locale="ro", blocked=blocked
        )
        assert not has_medical_claim(sentence)
    assert not has_medical_claim(REFERRAL)


async def test_the_composer_adds_the_obligation_and_remembers_the_sentence(
    electronics,  # noqa: F811
):
    llm = ComposeLLM(_reply(f"{REFERRAL} Spune-mi ce cauți.", met=[cp.SAFETY_REFERRAL]))
    ctx = _ctx(electronics, "salut")
    ctx.safety_decision = _pregnant()
    composed, reason = await cp.compose(
        ctx, _deps(llm), cp.ComposeInput(task="chitchat"), history=""
    )
    assert reason is None and composed is not None
    assert ctx.safety_referral_composed == REFERRAL
    user = llm.calls[0][1]
    assert '- safety_referral {"situation": ["ești însărcinată"]' in user


def _enforce_ctx(text: str, composed: str | None, *, products=None, blocked=True, kept=True):
    decision = _pregnant(blocked=blocked)
    if not kept:
        decision = Decision(
            kept=[],
            blocked=decision.blocked,
            contexts=decision.contexts,
            rule_ids=decision.rule_ids,
            must_refer=True,
        )
    events: list = []
    return NS(
        safety_decision=decision,
        reply=Reply(text=text, products=products),
        language="ro",
        retrieval=None,
        safety_referral_composed=composed,
        events=events,
        emit=lambda name, **p: events.append(NS(type=name, properties=p)),
    )


def test_enforce_keeps_the_composed_referral_instead_of_the_code_sentence():
    ctx = _enforce_ctx(f"{REFERRAL} Uite ce am.", REFERRAL, products=[{"product_id": "p1"}])
    safety_compose.enforce(ctx)
    assert ctx.reply.text == f"{REFERRAL} Uite ce am."
    assert ctx.reply.cacheable is False
    [event] = ctx.events
    assert event.properties["outcome"] == "composed"
    safety_compose.enforce(ctx)  # a doua trecere a runnerului: nimic în plus
    assert len(ctx.events) == 1 and ctx.reply.text == f"{REFERRAL} Uite ce am."


def test_enforce_prepends_the_code_sentence_when_the_composed_one_is_gone():
    ctx = _enforce_ctx("Alt răspuns, pus de altă cale.", REFERRAL, products=[{"product_id": "p1"}])
    safety_compose.enforce(ctx)
    assert ctx.reply.text.startswith("Țin cont că ești însărcinată și am lăsat deoparte")
    assert ctx.events[-1].properties["outcome"] == "prepended"


def test_an_emptied_set_stays_the_codes_answer():
    ctx = _enforce_ctx(f"{REFERRAL} N-am găsit.", REFERRAL, kept=False)
    safety_compose.enforce(ctx)
    assert ctx.events[-1].properties["outcome"] == "replaced"
    assert "N-am găsit" not in ctx.reply.text
