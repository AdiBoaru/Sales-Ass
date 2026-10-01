"""NX-367 — excluderea de siguranță se SPUNE clientului, iar modelul nu o mai poate nega.

Conversația c6 din 2026-10-01 („sunt însărcinată…", apoi „dar ceva cu retinol ai?"): căutarea a
găsit 13 seruri cu retinoizi, regula de sarcină le-a exclus pe toate, modelul a văzut un set gol și
a scris „Nu am găsit seruri cu retinol în catalog. … discută cu medicul sau farmacistul". Fraza
garantată („Țin cont că ești însărcinată și am lăsat deoparte opțiunile cu retinoizi…") s-a retras,
fiindcă textul conținea deja „farmacist", iar răspunsul a plecat și `cacheable: true`.
"""

from __future__ import annotations

import pytest

from src.models import Reply, RichItem, RichReply
from src.safety import contraindications as ci
from src.safety import messages
from src.safety.compose import enforce, model_hint, safety_sentence_for
from src.safety.contraindications import Block
from src.safety.policy import Decision, _merge_decision


class _Ctx:
    def __init__(self, reply, decision, language="ro"):
        self.reply = reply
        self.safety_decision = decision
        self.language = language
        self.events = []

    def emit(self, kind, **props):
        self.events.append((kind, props))


def _decision(kept=(), n_blocked=13):
    return Decision(
        kept=list(kept),
        blocked=[
            Block(f"r{i}", "pregnancy", "pregnancy-retinoids", "retinol") for i in range(n_blocked)
        ],
        contexts=("pregnancy",),
        rule_ids=("pregnancy-retinoids",),
        must_refer=True,
        message_key="safety.blocked",
    )


C6_MODEL_TEXT = (
    "Nu am găsit seruri cu retinol în catalog. Fiind însărcinată, discută cu medicul sau "
    "farmacistul înainte de a folosi produse cu retinol."
)


def _sentence():
    return safety_sentence_for(_decision(), "ro")


def test_c6_emptied_set_is_answered_by_code_not_by_the_model():
    ctx = _Ctx(Reply(text=C6_MODEL_TEXT), _decision())
    enforce(ctx)
    assert ctx.reply.text == f"{_sentence()} {messages.alternatives_offer('ro')}"
    assert "Nu am găsit" not in ctx.reply.text
    assert ctx.reply.cacheable is False
    assert ctx.events[-1][1]["outcome"] == "replaced"


def test_model_referral_does_not_suppress_the_guaranteed_sentence():
    """Produse păstrate pe ecran: proza comercială rămâne, trimiterea modelului se scoate, iar
    fraza garantată (cu ce s-a exclus) intră o dată."""
    kept = [{"id": "p1", "name": "SOME BY MI Yuja Niacin"}]
    text = (
        "Pentru pete, SOME BY MI Yuja Niacin e o variantă blândă. "
        "Pentru alegerea unui produs în sarcină, cere sfatul medicului sau farmacistului."
    )
    reply = Reply(text=text, products=kept)
    ctx = _Ctx(reply, _decision(kept=kept))
    enforce(ctx)
    out = ctx.reply.text
    assert out.startswith(_sentence())
    assert "SOME BY MI Yuja Niacin e o variantă blândă." in out
    assert "cere sfatul medicului" not in out  # trimiterea modelului, scoasă
    assert out.count("farmacist") == 1  # avertismentul apare o singură dată
    assert ctx.reply.cacheable is False


def test_a_referral_that_names_a_card_is_kept():
    kept = [{"id": "p1", "name": "SOME BY MI Yuja Niacin"}]
    text = "SOME BY MI e bun, dar discută întâi cu medicul sau farmacistul."
    ctx = _Ctx(Reply(text=text, products=kept), _decision(kept=kept))
    enforce(ctx)
    assert "SOME BY MI e bun" in ctx.reply.text  # recomandarea nu se pierde


def test_rich_intro_and_education_follow_the_same_contract():
    kept = [{"id": "p1", "name": "SOME BY MI Yuja Niacin"}]
    rich = RichReply(
        intro="Pentru pete, uite o variantă.",
        items=[RichItem(product_id="p1", name="SOME BY MI Yuja Niacin", price=110.0, reason="x")],
        pick=None,
        education="Discută cu medicul sau farmacistul. Aplică seara.",
        chips=[],
        disclaimer=None,
    )
    ctx = _Ctx(Reply(text="x", products=kept, rich=rich), _decision(kept=kept))
    enforce(ctx)
    assert ctx.reply.rich.intro.startswith(_sentence())
    assert ctx.reply.rich.education == "Aplică seara."


def test_emptied_rich_reply_gets_the_code_answer_in_the_intro():
    rich = RichReply(
        intro="Nu am găsit nimic potrivit.",
        items=[],
        pick=None,
        education="Ceva.",
        chips=[],
        disclaimer=None,
    )
    ctx = _Ctx(Reply(text="Nu am găsit nimic potrivit.", rich=rich), _decision())
    enforce(ctx)
    assert ctx.reply.rich.intro == ctx.reply.text
    assert ctx.reply.rich.education is None


def test_a_pending_question_is_not_replaced():
    reply = Reply(text="Vrei să caut altceva?", pending_question={"key": "x"})
    ctx = _Ctx(reply, _decision())
    enforce(ctx)
    assert ctx.reply.text.startswith(_sentence())
    assert "Vrei să caut altceva?" in ctx.reply.text


def test_idempotent_on_our_sentence():
    ctx = _Ctx(Reply(text="ceva", products=[{"id": "p1", "name": "X"}]), _decision(kept=[{}]))
    enforce(ctx)
    first = ctx.reply.text
    enforce(ctx)
    assert ctx.reply.text == first


def test_cacheable_is_false_even_without_anything_to_add():
    reply = Reply(text=f"{_sentence()}\n\nAltceva.", products=[{"id": "p1", "name": "X"}])
    ctx = _Ctx(reply, _decision(kept=[{"id": "p1"}]))
    enforce(ctx)
    assert ctx.reply.cacheable is False


def test_model_hint_states_how_many_were_excluded():
    hint = model_hint(_decision())
    assert "13 produse" in hint and "Nu spune că nu există în catalog" in hint
    assert "produse găsite" not in model_hint(_decision(n_blocked=0))


def test_turn_keeps_everything_any_evaluation_kept():
    """O căutare care păstrează A, apoi un detaliu pe un produs blocat: turul nu e „golit"."""
    ctx = _Ctx(None, None)
    _merge_decision(ctx, _decision(kept=[{"id": "a"}], n_blocked=1))
    _merge_decision(ctx, _decision(kept=[], n_blocked=1))
    assert [p["id"] for p in ctx.safety_decision.kept] == ["a"]


@pytest.mark.parametrize(
    "name, ingredients, blocked",
    [
        (
            "DR.REJU-ALL Advanced Retino-Mela Serum",
            ["hydroxypinacolone retinoate hpr", "niacinamida"],
            True,
        ),
        ("VT COSMETICS Cica Reti-A Mask", ["hydroxypinacolone retinoate", "bakuchiol"], True),
        ("Ser cu bakuchiol", ["bakuchiol", "niacinamida"], False),
        ("ROUND LAB Soybean Nourishing Cream", ["ceramide np", "retinere apa"], False),
    ],
)
def test_registry_blocks_retinoates_not_lookalikes(name, ingredients, blocked):
    p = {"id": "x", "name": name, "attributes": {"key_ingredients": ingredients}}
    assert (ci.check_product(p, frozenset({"pregnancy"})) is not None) is blocked
