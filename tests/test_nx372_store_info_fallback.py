"""NX-372 — un tur care a citit doar regulile magazinului nu răspunde „n-am găsit produse".

Rularea pe producție din 2026-10-01 (`tasks/stage1/KERNEL-LIVE-2026-10-01.md`, clasa A1): la
«livrați și în Republica Moldova?» modelul a chemat `faq_lookup` și a scris că nu are informația,
validatorul a respins fraza (afirmație despre livrare, nu citat al unei reguli), iar clientul a
primit „Momentan n-am găsit produse potrivite". La «cât fac toate în coș?» la fel, după un total
scris din istoric. Turul nu căutase niciun produs: rezerva comună era falsă pentru el.

Regula e STRUCTURALĂ (ce unelte a chemat turul: `RetrievalResult.catalog_read` fals și
`RetrievalResult.store_only`, adică doar unelte din `tools.base.STORE_READ_TOOLS`), nu citește
mesajul, deci ține pe orice pachet și în orice limbă cu frazele `kernel_sentences.store_info_*`.
Fraza spune ce s-a citit de fapt (recenzia PR-ului): reguli citite ⇒ `store_info_unconfirmed`
(nu afirmă că lipsește), citire reușită fără nicio regulă ⇒ `store_info_unknown`, citire picată ⇒
`store_info_unavailable`, iar după reguli servite parțial ⇒ `store_info_rest_unconfirmed`. Testele
rulează pe pachetul SOLE (forma din fixture-uri) și pe două pachete de alt domeniu.
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


#: Turul a citit doar regulile: `faq_lookup` (nu e o unealtă de catalog), iar citirea a mers.
STORE_TURN = RetrievalResult(
    products=[],
    source="tools",
    catalog_read=False,
    read_beyond_catalog=True,
    store_only=True,
    store_read_ok=True,
)
#: Cele patru fraze ale turului de magazin (recenzia PR-ului: fraza spune ce s-a citit de fapt).
STORE_CODES = (
    "store_info_unknown",
    "store_info_unconfirmed",
    "store_info_unavailable",
    "store_info_rest_unconfirmed",
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
    # Regulile de livrare au fost citite: fraza nu afirmă că informația lipsește din ele.
    assert ctx.reply.text == kernel_sentence(pack, "ro", "store_info_unconfirmed")
    assert PRODUCTS_NO_RESULT not in ctx.reply.text
    assert not ctx.reply.suggestions  # fără chips de produse sub o întrebare de livrare
    assert ctx.reply.cacheable is False
    [event] = _events(ctx, "store_info_unanswered")
    assert event.properties["reason"] == "prose_rejected" and event.properties["sentence"] is True
    assert event.properties["code"] == "store_info_unconfirmed"


async def test_a_cart_total_written_from_history_gets_the_store_sentence(pack):
    """Clasa e aceeași: turul a citit doar `faq_lookup`. Cititul coșului e cardul următor (D4)."""
    ctx = _ctx(CART_CLIENT, pack, retrieval=STORE_TURN)
    await render(ctx, _deps(), _plan(CART_MODEL, CART_CLIENT))
    assert ctx.reply.text == kernel_sentence(pack, "ro", "store_info_unconfirmed")


async def test_a_store_turn_without_any_text_gets_the_store_sentence(pack):
    ctx = _ctx(MOLDOVA_CLIENT, pack, retrieval=STORE_TURN)
    await render(ctx, _deps(), _plan("", MOLDOVA_CLIENT))
    assert ctx.reply.text == kernel_sentence(pack, "ro", "store_info_unconfirmed")
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
    assert ctx.reply.text == kernel_sentence(pack, "en", "store_info_unconfirmed")
    assert ctx.reply.text != kernel_sentence(pack, "ro", "store_info_unconfirmed")


async def test_an_order_turn_is_not_touched(pack):
    """ORDER are ramura lui (login / numărul comenzii); regula e doar pe vânzare."""
    ctx = _ctx("unde e comanda mea?", pack, retrieval=STORE_TURN)
    plan = dataclasses.replace(_plan("", "unde e comanda mea?"), is_order=True)
    await render(ctx, _deps(), plan)
    assert not _events(ctx, "store_info_unanswered")


@pytest.mark.parametrize("code", STORE_CODES)
def test_the_code_is_in_the_closed_vocabulary_and_the_loader_keeps_it(code):
    assert code in KERNEL_SENTENCE_CODES
    kept = _norm_kernel_sentences({"ro": {code: "Nu am informația asta."}})
    assert kept == {"ro": {code: "Nu am informația asta."}}


def test_the_default_sentences_respect_the_voice_rules():
    """P13: fără liniuță de pauză și fără punct și virgulă în textul către client."""
    pack = fixture_catalog.pack("electronics")
    for locale in ("ro", "en"):
        for code in STORE_CODES:
            phrase = kernel_sentence(pack, locale, code)
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
    from src.tools.base import STORE_READ_TOOLS

    ctx = _ctx(body, pack, retrieval=None)
    deps = _deps()
    run = ToolRun(ctx, deps)
    run.called = list(called)
    run.store_reads_ok = sum(name in STORE_READ_TOOLS for name in called)
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
    assert ctx.reply.text == kernel_sentence(pack, "ro", "store_info_unconfirmed")


async def test_the_real_plan_with_faq_called_three_times_is_still_a_store_turn(pack):
    """k7 T3: modelul a chemat `faq_lookup` de trei ori."""
    ctx = await _through_build_plan(
        pack, ["faq_lookup"] * 3, sources=DELIVERY_RULES, final=CART_MODEL, body=CART_CLIENT
    )
    assert ctx.reply.text == kernel_sentence(pack, "ro", "store_info_unconfirmed")


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


# --- recenzia adversarială a PR-ului (#534) ------------------------------------------------------

#: k9 cu proza care redă și o regulă: regula de livrare + propoziția despre Moldova (parțial).
MOLDOVA_PARTIAL_MODEL = (
    "Taxa de livrare e între 19,9 și 24,90 lei în România. "
    "Pentru Republica Moldova nu am informații confirmate despre livrare."
)
#: Regula de livrare a fost citită și e chiar răspunsul, dar clientul n-a folosit cuvântul
#: întrebării FAQ-ului („livrarea"), deci `store_rules` nu o poate servi (NX-369, recenzia).
COURIER_CLIENT = "platesc ceva pentru curier?"
COURIER_MODEL = "Curierul te costă între 19,9 și 24,90 lei, cu TVA inclus."


async def test_review_partial_store_turn_has_no_product_half(pack):
    """Finding 1: pe un tur doar de magazin, ramura parțială NX-369 punea după regulă mesajul de
    produse și chips de produse. Acum: regula, apoi fraza pentru rest, fără chips."""
    ctx = _ctx(MOLDOVA_CLIENT, pack, retrieval=STORE_TURN)
    await render(ctx, _deps(), _plan(MOLDOVA_PARTIAL_MODEL, MOLDOVA_CLIENT))
    assert PRODUCTS_NO_RESULT not in ctx.reply.text
    assert not ctx.reply.suggestions
    assert ctx.reply.text.startswith(DELIVERY_RULES[0])
    assert ctx.reply.text.endswith(kernel_sentence(pack, "ro", "store_info_rest_unconfirmed"))
    assert ctx.reply.cacheable is False
    [event] = _events(ctx, "store_info_unanswered")
    assert event.properties["reason"] == "partial"


async def test_review_partial_on_a_catalog_turn_keeps_the_product_half(pack):
    """O întrebare MIXTĂ (turul a căutat și produse): jumătatea de produs rămâne, ca la NX-369."""
    ctx = _ctx(MOLDOVA_CLIENT, pack, retrieval=CATALOG_TURN)
    await render(ctx, _deps(), _plan(MOLDOVA_PARTIAL_MODEL, MOLDOVA_CLIENT))
    assert ctx.reply.text.startswith(DELIVERY_RULES[0])
    assert PRODUCTS_NO_RESULT in ctx.reply.text


async def test_review_partial_without_the_rest_sentence_serves_the_rules_alone():
    bare = fixture_catalog.pack("electronics")
    sentences = {
        lang: {k: v for k, v in table.items() if k != "store_info_rest_unconfirmed"}
        for lang, table in bare.kernel_sentences.items()
    }
    bare = dataclasses.replace(bare, kernel_sentences=sentences)
    ctx = _ctx(MOLDOVA_CLIENT, bare, retrieval=STORE_TURN)
    await render(ctx, _deps(), _plan(MOLDOVA_PARTIAL_MODEL, MOLDOVA_CLIENT))
    assert ctx.reply.text == DELIVERY_RULES[0]
    [event] = _events(ctx, "store_info_unanswered")
    assert event.properties["sentence"] is False


async def test_review_rules_were_read_so_the_reply_never_claims_they_lack_it(pack):
    """Finding 2: `faq_lookup` a adus regula potrivită, dar cuvintele clientului nu se leagă de
    întrebarea ei. „Nu am informația asta în regulile magazinului" ar fi fals: fraza neutră."""
    ctx = _ctx(COURIER_CLIENT, pack, retrieval=STORE_TURN)
    await render(ctx, _deps(), _plan(COURIER_MODEL, COURIER_CLIENT))
    assert ctx.reply.text != kernel_sentence(pack, "ro", "store_info_unknown")
    assert ctx.reply.text == kernel_sentence(pack, "ro", "store_info_unconfirmed")
    assert PRODUCTS_NO_RESULT not in ctx.reply.text


async def test_review_no_rules_at_all_is_the_only_case_that_claims_absence(pack):
    """`faq_lookup` a mers și n-a adus nicio regulă (tenant fără FAQ pe limba turului)."""
    ctx = _ctx(MOLDOVA_CLIENT, pack, retrieval=STORE_TURN)
    await render(ctx, _deps(), _plan(MOLDOVA_MODEL, MOLDOVA_CLIENT, sources=[]))
    assert ctx.reply.text == kernel_sentence(pack, "ro", "store_info_unknown")


async def _through_real_tool_run(pack, monkeypatch, results, *, final, body, sources=()):
    """`ToolRun.execute` pe unealta reală a turului (rezultatul ei e dat), apoi `build_plan` +
    `render`: `store_only`/`store_read_ok` vin din ce a întors fiecare apel."""
    from src.agent import tool_executor
    from src.agent.planner import build_plan
    from src.agent.tool_executor import ToolRun
    from src.tools.base import ToolResult

    queue = list(results)

    async def fake_run_tool(ctx, deps, name, args):
        if not queue.pop(0):
            return ToolResult(ok=False, error="ConnectionError")
        return ToolResult(
            ok=True,
            llm_view="reguli",
            sources=list(sources),
            source_questions={k: v for k, v in DELIVERY_QUESTIONS.items() if k in sources},
        )

    monkeypatch.setattr(tool_executor, "run_tool", fake_run_tool)
    ctx = _ctx(body, pack, retrieval=None)
    deps = _deps()
    run = ToolRun(ctx, deps)
    for _ in results:
        await run.execute("faq_lookup", {"query": "livrare"})
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
        tool_names=["faq_lookup"],
        kernel=True,
    )
    await render(ctx, deps, plan)
    return ctx, run


async def test_review_a_failed_faq_lookup_does_not_claim_the_rules_lack_it(pack, monkeypatch):
    """Finding 3: `faq_lookup` a picat, deci nimic n-a fost citit. Nici „n-am găsit produse"
    (nu s-a căutat nimic), nici „nu am informația în regulile magazinului": fraza indisponibilă."""
    ctx, run = await _through_real_tool_run(
        pack, monkeypatch, [False], final=MOLDOVA_MODEL, body=MOLDOVA_CLIENT
    )
    assert run.read_store_only is True
    assert ctx.reply.text != kernel_sentence(pack, "ro", "store_info_unknown")
    assert ctx.reply.text == kernel_sentence(pack, "ro", "store_info_unavailable")
    assert PRODUCTS_NO_RESULT not in ctx.reply.text
    assert run.store_read_ok is False and ctx.retrieval.store_read_ok is False


async def test_review_a_retry_that_succeeds_counts_as_a_read(pack, monkeypatch):
    ctx, run = await _through_real_tool_run(
        pack,
        monkeypatch,
        [False, True],
        final=MOLDOVA_MODEL,
        body=MOLDOVA_CLIENT,
        sources=DELIVERY_RULES,
    )
    assert ctx.reply.text == kernel_sentence(pack, "ro", "store_info_unconfirmed")
    assert run.store_read_ok is True


async def test_review_a_read_without_rules_on_the_real_path_claims_absence(pack, monkeypatch):
    ctx, _ = await _through_real_tool_run(
        pack, monkeypatch, [True], final=MOLDOVA_MODEL, body=MOLDOVA_CLIENT
    )
    assert ctx.reply.text == kernel_sentence(pack, "ro", "store_info_unknown")


async def test_review_a_valid_answer_on_a_store_turn_is_untouched(pack):
    client = "cat costa livrarea?"
    ctx = _ctx(client, pack, retrieval=STORE_TURN)
    await render(ctx, _deps(), _plan(DELIVERY_RULES[0], client))
    assert ctx.reply.text == DELIVERY_RULES[0]
    assert not _events(ctx, "store_info_unanswered")
