"""NX-389b (`kernel.v9.0`) — întrebarea de familie ține minte rutina, iar răspunsul o face.

Pe 389a, întrebarea închidea cererea: memoria întrebării avea doar cheia, plannerul arunca actul la
`must_ask`, iar la «pentru față» modelul citește un `find`, deci clientul ar fi primit o listă de
produse în locul rutinei. Acum întrebarea ține minte actul și opțiunile (`resume_route = "bundle"`,
`options_refs` = cheile rafturilor), iar CODUL decide că turul de răspuns continuă rutina
(`routine_family.resume_routine`). Lanțul pur pe trei ture (`kernel_step`), zero model, zero DB."""

from __future__ import annotations

import dataclasses

from src.conversation.ambiguity_gate import ROUTINE_FAMILY_KEY
from src.conversation.routine_family import RESUME_BUNDLE, resume_routine
from src.conversation.state_v2 import ConversationStateV2, PendingClarification
from tests.test_nx389_routine_family_question import (
    PETE,
    SEARA,
    change,
    family_step,
    interp,
)

ASK = "fa-mi o rutina de seara pt pete pigmentare, maxim 150 lei toata"
BUDGET = {
    "op": "set",
    "dimension": "price",
    "value": None,
    "quote": "maxim 150 lei toata",
    "relation": "lte",
    "number": 150.0,
    "unit": "lei",
    "target": None,
    "relative_to": None,
}


def _asked():
    """Turul 1: cererea, întrebarea pusă, starea de după (cu memoria întrebării)."""
    first = family_step(ASK, PETE, SEARA, BUDGET)
    assert first.outcome.decision.verdict == "must_ask"
    return first


def _primary(step):
    return step.planned.plans[step.planned.primary]


# --- memoria întrebării --------------------------------------------------------------------------


def test_the_question_remembers_the_routine_and_its_options():
    pending = _asked().state_after.pending_clarification
    assert pending is not None
    assert pending.target_key == ROUTINE_FAMILY_KEY
    assert pending.resume_route == RESUME_BUNDLE
    assert pending.options_refs == ("ten", "machiaj")


# --- răspunsul -----------------------------------------------------------------------------------


def test_answering_face_builds_the_face_routine_whatever_act_the_model_wrote():
    """«pentru față» citit de model ca `find` cu raftul `ten`: rutina pe față, cu momentul și cu
    bugetul spuse la cerere."""
    state = _asked().state_after
    answer = family_step("pentru fata", change("category", "ten", "fata"), act="find", state=state)
    plan = _primary(answer)
    assert (plan.executor, plan.family) == ("bundle", "fata")
    assert plan.search_args is not None and plan.search_args.price_max == 150.0
    assert answer.state_after.topic.category_key == "ten"
    assert answer.state_after.pending_clarification is None
    codes = [code for _, code in answer.planned.disclosures]
    assert "family_defaulted" not in codes
    assert answer.delta.counters.get("routine_resumed") == 1


def test_the_answer_sets_the_shelf_even_when_the_quote_names_a_homograph():
    """«față» numește în vocabular și subraftul de machiaj `machiaj-fata`, deci validatorul poate
    respinge citatul. Opțiunea aleasă e un răspuns la meniul NOSTRU: subiectul se scrie oricum."""
    state = _asked().state_after
    answer = family_step("fata", change("category", "ten", "fata"), act="other", state=state)
    assert answer.state_after.topic.category_key == "ten"
    assert (_primary(answer).executor, _primary(answer).family) == ("bundle", "fata")


def test_answering_makeup_builds_the_makeup_routine():
    state = _asked().state_after
    answer = family_step(
        "machiaj", change("category", "machiaj", "machiaj"), act="find", state=state
    )
    assert (_primary(answer).executor, _primary(answer).family) == ("bundle", "machiaj")


def test_i_dont_know_takes_the_majority_family_and_says_so():
    state = _asked().state_after
    answer = family_step("nu stiu, alege tu", act="bundle", state=state)
    plan = _primary(answer)
    assert (plan.executor, plan.family) == ("bundle", "fata")
    assert "family_defaulted" in [code for _, code in answer.planned.disclosures]
    assert answer.outcome.decision.question is None


def test_both_builds_the_first_and_offers_the_other():
    state = _asked().state_after
    answer = family_step(
        "ambele",
        change("category", "machiaj", "machiaj"),
        change("category", "ten", "ten"),
        act="find",
        state=state,
    )
    plan = _primary(answer)
    assert (plan.executor, plan.family) == ("bundle", "fata")
    assert plan.offer == ["Machiaj"]
    assert answer.state_after.topic.category_key == "ten"


def test_a_new_request_is_served_as_itself():
    """La întrebare, clientul cere altceva: nicio rutină, întrebarea se închide."""
    state = _asked().state_after
    answer = family_step(
        "aveti livrare gratuita?",
        act="store_info",
        state=state,
    )
    assert _primary(answer).executor != "bundle"
    assert answer.state_after.pending_clarification is None


def test_another_shelf_is_a_new_request():
    state = _asked().state_after
    answer = family_step("vreau ceva de par", change("category", "par", "par"), state=state)
    assert answer.delta.counters.get("routine_resumed") is None


# --- funcția pură --------------------------------------------------------------------------------


def _pending_state(**overrides) -> ConversationStateV2:
    pending = PendingClarification(
        question_id="q:routine_family:1:1",
        target_key=ROUTINE_FAMILY_KEY,
        reason="no_family",
        options_refs=("ten", "machiaj"),
        asked_at_revision=1,
        resume_route=RESUME_BUNDLE,
    )
    return ConversationStateV2(
        revision=1, pending_clarification=dataclasses.replace(pending, **overrides)
    )


def test_resume_needs_a_live_routine_question():
    answer = interp(change("category", "ten", "fata"), act="find")
    assert resume_routine(answer, ConversationStateV2(revision=1))[1] is None
    assert resume_routine(answer, _pending_state(resume_route=None))[1] is None
    assert resume_routine(answer, _pending_state(asked_at_revision=-5))[1] is None


def test_a_sub_shelf_is_not_one_of_the_options():
    """«față» citit de model pe `machiaj-fata` (rădăcina `machiaj`): nu e opțiunea `ten`, deci nu
    reia o rutină de machiaj."""
    answer = interp(change("category", "machiaj-fata", "fata"), act="find")
    effective, resumed = resume_routine(answer, _pending_state())
    assert resumed is None and effective is answer


def test_an_item_type_is_a_new_request():
    answer = interp(change("product_type", "ser", "un ser"), act="find")
    assert resume_routine(answer, _pending_state())[1] is None


def test_an_aside_does_not_resume():
    answer = interp(act="chitchat").model_copy(update={"thread": "aside"})
    assert resume_routine(answer, _pending_state())[1] is None


def test_resume_keeps_the_needs_said_with_the_answer():
    answer = interp(
        change("category", "ten", "fata"), change("concerns", "pores", "pori"), act="find"
    )
    effective, resumed = resume_routine(answer, _pending_state())
    assert resumed is not None and resumed.chosen == "ten"
    assert [a.kind for a in effective.acts] == ["bundle"]
    assert [c.dimension for c in effective.changes] == ["category", "concerns"]


# --- vederea modelului ---------------------------------------------------------------------------


def test_the_model_sees_the_act_and_the_options_of_the_routine_question():
    from src.conversation.turn_interpreter import _INSTRUCTIONS, _pending

    pending = _asked().state_after.pending_clarification
    assert _pending(pending) == "routine_family (act: bundle; options: ten, machiaj)"
    text = " ".join(_INSTRUCTIONS.split())
    assert "with the act it was asked for and its options when it has them" in text


def test_any_other_pending_question_is_shown_as_before():
    from src.conversation.turn_interpreter import _pending

    other = dataclasses.replace(
        _pending_state().pending_clarification, resume_route=None, options_refs=()
    )
    assert _pending(other) == ROUTINE_FAMILY_KEY
    assert _pending(None) == "none"


# --- «ambele»: rutina celeilalte familii, oferită ca chip ----------------------------------------


def _ctx(reply):
    from types import SimpleNamespace

    from tests.test_nx389_routine_family_question import sole_pack

    return SimpleNamespace(
        reply=reply, language="ro", business=SimpleNamespace(domain_pack=sole_pack())
    )


def test_the_other_routine_is_offered_first_among_the_chips():
    from src.agent.kernel_executors import _offer_routines
    from src.models import Chip, Reply, RichReply

    rich = RichReply(
        intro="",
        items=[],
        chips=[Chip(label="Arata-mi mai multe", payload="x")],
        pick=None,
        education="",
        disclaimer="",
    )
    reply = Reply(text="", rich=rich)
    _offer_routines(_ctx(reply), ["Machiaj"])
    assert [c.label for c in rich.chips] == ["Fa-mi si rutina pentru machiaj", "Arata-mi mai multe"]


def test_a_plain_reply_gets_the_offer_in_its_suggestions():
    from src.agent.kernel_executors import _offer_routines
    from src.models import Reply

    reply = Reply(text="rutina", suggestions=["Altceva"])
    _offer_routines(_ctx(reply), ["Corp"])
    assert reply.suggestions == ["Fa-mi si rutina pentru corp", "Altceva"]


def test_a_refusal_does_not_start_a_routine():
    """Recenzia 389b: «nu mai vreau» (citit `chitchat` sau `other`, fără alegere) nu e «alege tu»:
    nicio rutină pornită pe care clientul n-o mai cere."""
    state = _asked().state_after
    for act in ("chitchat", "other", "find"):
        answer = family_step("nu mai vreau", act=act, state=state)
        assert answer.delta.counters.get("routine_resumed") is None
        assert _primary(answer).executor != "bundle"


# --- 389c: textul întrebării și chips-urile de răspuns -------------------------------------------


class _FakeCtx:
    def __init__(self):
        from types import SimpleNamespace

        from tests.test_nx389_routine_family_question import sole_pack

        self.language = "ro"
        self.history = []
        self.business = SimpleNamespace(id="b", domain_pack=sole_pack())
        self.reply = None
        self.clarify = None

    def set_clarify(self, text, *, field, resume_route, suggestions=None):
        from src.models import Reply

        self.clarify = (text, field, resume_route, suggestions)
        self.reply = Reply(text=text, suggestions=list(suggestions or []))


def test_the_routine_question_tells_the_composer_what_it_is_about_and_offers_answer_chips(
    monkeypatch,
):
    import asyncio

    from src.agent import composer, kernel_executors
    from src.config import get_settings

    seen = {}

    async def fake_compose(ctx, deps, inp, history=None):
        seen["inp"] = inp
        return None, "test"

    monkeypatch.setattr(composer, "compose", fake_compose)
    on = get_settings().model_copy(update={"composer_ask_enabled": True})
    monkeypatch.setattr(kernel_executors, "get_settings", lambda: on)
    first = _asked()
    ctx = _FakeCtx()
    served = asyncio.run(kernel_executors._ask(ctx, None, _primary(first), first.outcome))
    assert served
    facts = seen["inp"].obligations[0].facts
    assert facts["options"] == ["Ten", "Machiaj"]
    assert "routine" in facts["about"]
    text, _field, _route, chips = ctx.clarify
    assert text == first.outcome.decision.question  # compozitorul a picat: șablonul porții
    assert chips == ["Pentru ten", "Pentru machiaj"]
