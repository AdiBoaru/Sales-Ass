"""NX-372 — un tur care a citit doar regulile magazinului nu răspunde „n-am găsit produse".

Rularea pe producție din 2026-10-01 (`tasks/stage1/KERNEL-LIVE-2026-10-01.md`, clasa A1): la
«livrați și în Republica Moldova?» modelul a chemat `faq_lookup` și a scris că nu are informația,
validatorul a respins fraza (afirmație despre livrare, nu citat al unei reguli), iar clientul a
primit „Momentan n-am găsit produse potrivite". La «cât fac toate în coș?» la fel, după un total
scris din istoric. Turul nu căutase niciun produs: rezerva comună era falsă pentru el.

Regula e STRUCTURALĂ (ce unelte a chemat turul: `RetrievalResult.catalog_read` și
`read_beyond_catalog`), nu citește mesajul, deci ține pe orice pachet și în orice limbă cu fraza
`kernel_sentences.store_info_unknown`. Testele rulează pe pachetul SOLE (forma din fixture-uri) și
pe două pachete de alt domeniu.
"""

from __future__ import annotations

import dataclasses

import pytest

from src.agent.finalize import render
from src.agent.planner import ResponsePlan
from src.agent.prompt_builder import PromptInputs
from src.config import get_settings
from src.domain.loader import _norm_kernel_sentences
from src.domain.pack import KERNEL_SENTENCE_CODES, kernel_sentence
from src.models import (
    BusinessConfig,
    Contact,
    ConversationState,
    InboundMessage,
    RetrievalResult,
    TurnContext,
)
from src.worker.runner import PipelineDeps
from tests.kernel import fixture_catalog

#: Regulile de livrare SOLE (`db/seed/faqs_sole_ro.json`), cum le aduce `faq_lookup`.
DELIVERY_RULES = [
    "Taxa de livrare e între 19,9 și 24,90 lei, cu TVA inclus, iar suma exactă o vezi în "
    "pagina de finalizare a comenzii.",
    "Peste 199 lei transportul e gratuit, iar dacă ai mai comandat la noi pragul e 149 lei.",
]
DELIVERY_QUESTIONS = {
    DELIVERY_RULES[0]: "Cât costă livrarea?",
    DELIVERY_RULES[1]: "Când e livrarea gratuită?",
}
#: Textele REALE ale modelului din rularea de producție (`model_io`, turele k9 T2 și k7 T3).
MOLDOVA_CLIENT = "livrati si in Republica Moldova?"
MOLDOVA_MODEL = (
    "Nu am informații confirmate despre livrarea în Republica Moldova. Pot verifica acest lucru "
    "cu un coleg."
)
CART_CLIENT = "cat fac toate in cos?"
CART_MODEL = (
    "În coș este momentan primul TIRTIR Mask Fit Red Cushion, la 135 lei. Dacă adaugi și "
    "rezerva, totalul produselor va fi 270 lei."
)
PRODUCTS_NO_RESULT = "Momentan n-am găsit produse potrivite"


class _LLM:
    async def complete(self, system, user, *, model=None):
        return ""


def _deps() -> PipelineDeps:
    return PipelineDeps(conn=object(), redis=None, llm=_LLM())


def _ctx(
    body: str, pack, *, language: str = "ro", retrieval: RetrievalResult | None
) -> TurnContext:
    ctx = TurnContext(
        turn_id="t",
        business=BusinessConfig(
            id="biz-1", slug="s", name="n", default_locale=language, domain_pack=pack
        ),
        contact=Contact(id="c", business_id="biz-1"),
        message=InboundMessage(provider_msg_id="m", body=body),
        conversation_id="conv",
        state=ConversationState(),
    )
    ctx.language = language
    ctx.retrieval = retrieval
    return ctx


#: Turul a citit doar regulile: `faq_lookup` (nu e o unealtă de catalog) și a adus surse.
STORE_TURN = RetrievalResult(
    products=[], source="tools", catalog_read=False, read_beyond_catalog=True, store_only=True
)
#: Turul a chemat o unealtă din afara catalogului care NU citește regulile (`clarify_options`, o
#: comandă, o mutație picată): recenzia NX-372, fraza de magazin ar fi falsă pentru el.
OTHER_TOOL_TURN = RetrievalResult(
    products=[], source="tools", catalog_read=False, read_beyond_catalog=True, store_only=False
)
#: Turul a căutat produse și n-a găsit.
CATALOG_TURN = RetrievalResult(products=[], source="tools", catalog_read=True)
#: Turul n-a chemat nicio unealtă.
NO_TOOL_TURN = RetrievalResult(products=[], source="tools", catalog_read=False)


def _plan(final: str, query: str, *, sources=DELIVERY_RULES) -> ResponsePlan:
    return ResponsePlan(
        handled=False,
        products=[],
        final=final,
        is_order=False,
        query=query,
        history="",
        inp=PromptInputs.build("D", "ecommerce", "ro", ["Ten"], []),
        mode="prose",
        grounded_sources=list(sources),
        grounded_questions={k: v for k, v in DELIVERY_QUESTIONS.items() if k in sources},
    )


def _events(ctx, name):
    return [e for e in ctx.events if getattr(e, "type", None) == name]


@pytest.fixture(params=["sole-ro", "electronics", "furniture"])
def pack(request):
    """Pachetul SOLE (forma din fixture-uri) și două pachete de alt domeniu: regula nu depinde de
    cosmetice."""
    if request.param == "sole-ro":
        from tests.kernel.replay import load_pack

        doc = load_pack("sole-ro")
        from src.domain.loader import load_domain_pack

        loaded = load_domain_pack(
            BusinessConfig(
                id="b-sole",
                slug="sole-ro",
                name="SOLE",
                vertical="ecommerce",
                settings={"domain_pack": doc["domain_pack"]},
            )
        )
        assert loaded is not None
        return loaded
    return fixture_catalog.pack(request.param)


async def test_a_store_question_with_a_rejected_answer_gets_the_store_sentence(pack):
    ctx = _ctx(MOLDOVA_CLIENT, pack, retrieval=STORE_TURN)
    await render(ctx, _deps(), _plan(MOLDOVA_MODEL, MOLDOVA_CLIENT))
    assert ctx.reply.text == kernel_sentence(pack, "ro", "store_info_unknown")
    assert PRODUCTS_NO_RESULT not in ctx.reply.text
    assert not ctx.reply.suggestions  # fără chips de produse sub o întrebare de livrare
    assert ctx.reply.cacheable is False
    [event] = _events(ctx, "store_info_unanswered")
    assert event.properties["reason"] == "prose_rejected" and event.properties["sentence"] is True


async def test_a_cart_total_written_from_history_gets_the_store_sentence(pack):
    """Clasa e aceeași: turul a citit doar `faq_lookup`. Cititul coșului e cardul următor (D4)."""
    ctx = _ctx(CART_CLIENT, pack, retrieval=STORE_TURN)
    await render(ctx, _deps(), _plan(CART_MODEL, CART_CLIENT))
    assert ctx.reply.text == kernel_sentence(pack, "ro", "store_info_unknown")


async def test_a_store_turn_without_any_text_gets_the_store_sentence(pack):
    ctx = _ctx(MOLDOVA_CLIENT, pack, retrieval=STORE_TURN)
    await render(ctx, _deps(), _plan("", MOLDOVA_CLIENT))
    assert ctx.reply.text == kernel_sentence(pack, "ro", "store_info_unknown")
    [event] = _events(ctx, "store_info_unanswered")
    assert event.properties["reason"] == "no_text"


async def test_a_catalog_search_without_results_keeps_the_product_message(pack):
    """Turul a CĂUTAT produse: „n-am găsit produse" e adevărat și rămâne, cu chips de continuare."""
    ctx = _ctx("vreau ceva rar", pack, retrieval=CATALOG_TURN)
    await render(ctx, _deps(), _plan(MOLDOVA_MODEL, "vreau ceva rar"))
    assert ctx.reply.text.startswith(PRODUCTS_NO_RESULT)
    assert not _events(ctx, "store_info_unanswered")


async def test_a_turn_without_any_tool_keeps_the_product_message(pack):
    ctx = _ctx("ceva", pack, retrieval=NO_TOOL_TURN)
    await render(ctx, _deps(), _plan("", "ceva", sources=[]))
    assert ctx.reply.text.startswith(PRODUCTS_NO_RESULT)


async def test_without_retrieval_the_old_path_holds(pack):
    """Apelanții care nu setează `ctx.retrieval` (handlerele vechi, testele NX-369): neschimbați."""
    ctx = _ctx(MOLDOVA_CLIENT, pack, retrieval=None)
    await render(ctx, _deps(), _plan(MOLDOVA_MODEL, MOLDOVA_CLIENT))
    assert ctx.reply.text.startswith(PRODUCTS_NO_RESULT)


async def test_the_kill_switch_restores_the_old_message(pack, monkeypatch):
    monkeypatch.setattr(get_settings(), "store_info_fallback_enabled", False)
    ctx = _ctx(MOLDOVA_CLIENT, pack, retrieval=STORE_TURN)
    await render(ctx, _deps(), _plan(MOLDOVA_MODEL, MOLDOVA_CLIENT))
    assert ctx.reply.text.startswith(PRODUCTS_NO_RESULT)
    assert not _events(ctx, "store_info_unanswered")


async def test_a_pack_without_the_sentence_falls_back_and_is_counted():
    bare = dataclasses.replace(fixture_catalog.pack("electronics"), kernel_sentences={})
    ctx = _ctx(MOLDOVA_CLIENT, bare, retrieval=STORE_TURN)
    await render(ctx, _deps(), _plan(MOLDOVA_MODEL, MOLDOVA_CLIENT))
    assert ctx.reply.text.startswith(PRODUCTS_NO_RESULT)  # niciodată tăcere (P6)
    [event] = _events(ctx, "store_info_unanswered")
    assert event.properties["sentence"] is False


async def test_the_sentence_follows_the_turn_language():
    pack = fixture_catalog.pack("electronics")
    ctx = _ctx("do you ship abroad?", pack, language="en", retrieval=STORE_TURN)
    await render(ctx, _deps(), _plan("", "do you ship abroad?"))
    assert ctx.reply.text == kernel_sentence(pack, "en", "store_info_unknown")
    assert ctx.reply.text != kernel_sentence(pack, "ro", "store_info_unknown")


async def test_an_order_turn_is_not_touched(pack):
    """ORDER are ramura lui (login / numărul comenzii); regula e doar pe vânzare."""
    ctx = _ctx("unde e comanda mea?", pack, retrieval=STORE_TURN)
    plan = dataclasses.replace(_plan("", "unde e comanda mea?"), is_order=True)
    await render(ctx, _deps(), plan)
    assert not _events(ctx, "store_info_unanswered")


def test_the_code_is_in_the_closed_vocabulary_and_the_loader_keeps_it():
    assert "store_info_unknown" in KERNEL_SENTENCE_CODES
    kept = _norm_kernel_sentences({"ro": {"store_info_unknown": "Nu am informația asta."}})
    assert kept == {"ro": {"store_info_unknown": "Nu am informația asta."}}


def test_the_default_sentences_respect_the_voice_rules():
    """P13: fără liniuță de pauză și fără punct și virgulă în textul către client."""
    pack = fixture_catalog.pack("electronics")
    for locale in ("ro", "en"):
        phrase = kernel_sentence(pack, locale, "store_info_unknown")
        assert phrase and ";" not in phrase
        assert " — " not in phrase and " – " not in phrase and " - " not in phrase


async def test_a_non_store_tool_keeps_the_product_message_and_its_chips(pack):
    """Recenzia NX-372: modelul a chemat doar `clarify_options` și întrebarea lui a picat; clientul
    primea „nu am informația în regulile magazinului" fără chips. Acum rămâne mesajul de produse."""
    question = "Le vrei wireless sau cu fir, sub 300 lei sau peste 300 lei?"
    ctx = _ctx("vreau casti", pack, retrieval=OTHER_TOOL_TURN)
    await render(ctx, _deps(), _plan(question, "vreau casti", sources=[]))
    assert ctx.reply.text.startswith(PRODUCTS_NO_RESULT)
    assert not _events(ctx, "store_info_unanswered")


# --- pe drumul real: `ToolRun` → `build_plan` → `render` (recenzia NX-372) ---------------------


async def _through_build_plan(pack, called, *, sources, final, body):
    from src.agent.planner import build_plan
    from src.agent.tool_executor import ToolRun

    ctx = _ctx(body, pack, retrieval=None)
    deps = _deps()
    run = ToolRun(ctx, deps)
    run.called = list(called)
    run.grounded_sources = list(sources)
    run.grounded_questions = {k: v for k, v in DELIVERY_QUESTIONS.items() if k in sources}
    plan = await build_plan(
        ctx,
        deps,
        run,
        PromptInputs.build("D", "ecommerce", "ro", ["Ten"], []),
        final=final,
        retrieved=[],
        is_order=False,
        show_more=False,
        query=body,
        history="",
        tool_names=list(called),
        kernel=True,
    )
    assert not plan.handled
    await render(ctx, deps, plan)
    return ctx


async def test_the_real_plan_marks_a_faq_only_turn_as_a_store_turn(pack):
    ctx = await _through_build_plan(
        pack, ["faq_lookup"], sources=DELIVERY_RULES, final=MOLDOVA_MODEL, body=MOLDOVA_CLIENT
    )
    assert ctx.retrieval.store_only is True and ctx.retrieval.catalog_read is False
    assert ctx.reply.text == kernel_sentence(pack, "ro", "store_info_unknown")


async def test_the_real_plan_with_faq_called_three_times_is_still_a_store_turn(pack):
    """k7 T3: modelul a chemat `faq_lookup` de trei ori."""
    ctx = await _through_build_plan(
        pack, ["faq_lookup"] * 3, sources=DELIVERY_RULES, final=CART_MODEL, body=CART_CLIENT
    )
    assert ctx.reply.text == kernel_sentence(pack, "ro", "store_info_unknown")


@pytest.mark.parametrize(
    "called",
    [["clarify_options"], ["check_order"], ["cart_add"], ["faq_lookup", "check_order"]],
)
async def test_the_real_plan_with_another_tool_keeps_the_product_message(pack, called):
    ctx = await _through_build_plan(
        pack, called, sources=[], final="Îți recomand Sony la 1499 lei.", body="vreau casti"
    )
    assert ctx.retrieval.store_only is False
    assert ctx.reply.text.startswith(PRODUCTS_NO_RESULT)


def test_store_read_tools_are_registered_tools_and_not_catalog_reads():
    from src.agent.tool_definitions import TOOL_NAMES
    from src.tools.base import CATALOG_READ_TOOLS, STORE_READ_TOOLS

    assert STORE_READ_TOOLS <= set(TOOL_NAMES)
    assert not STORE_READ_TOOLS & CATALOG_READ_TOOLS
