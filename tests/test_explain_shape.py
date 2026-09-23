"""NX-315 felia 3 — „cum se folosește": răspunsul întâi, din fișa MAGAZINULUI.

Turul real «cum se foloseste prima» (2026-09-23): iZi a răspuns din prima frază („se folosește ca o
cremă de zi obișnuită, pe ten curat"), apoi pașii și un sfat concret. Nativx a revândut produsul
într-un paragraf, a îngropat instrucțiunile la mijloc și a spus „dimineața și seara" de două ori.

Cauza pe v1 nu era doar sufixul buclei (NX-307): apelul rich RESCRIE răspunsul, iar regula lui de
DEEP-DIVE cere „`intro` = ce ESTE produsul". Deci instrucțiunile magazinului și forma „răspunsul
întâi" trebuie să ajungă și în compunerea rich. Aprinderea se decide pe golden (D15); măsurătoarea
(`explain_shape`) rulează și cu flagul stins, ca să existe cifra de azi cu care comparăm.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.agent import answer_shape, response_quality, turn_profile
from src.agent.finalize import render
from src.agent.planner import ResponsePlan
from src.agent.prompt_builder import PromptInputs
from src.agent.voice import naturalize
from src.config import get_settings
from src.domain.pack import DomainPack, SectionSpec
from src.models import BusinessConfig, Contact, ConversationState, InboundMessage, TurnContext
from src.worker.runner import PipelineDeps

INP = PromptInputs.build("S", "ecommerce", "ro", ["Ten"], [])
USAGE = (
    "Aplica o cantitate mica pe tenul curat, dimineata si seara. Maseaza usor pana la absorbtie. "
    "Evita zona ochilor."
)
PACK = DomainPack(
    vertical="ecommerce",
    detail_sections=(SectionSpec(kind="usage", max_chars=400),),
    howto_sections=("usage", "dosage"),
)


def _product(sections: list | None = None) -> dict:
    p = {
        "id": "p1",
        "name": "Crema Yuja",
        "price": 80.0,
        "url": "https://shop/p1",
        "availability": "in_stock",
        "ai_summary": "Crema hidratanta cu vitamina C pentru luminozitate si fermitate.",
    }
    if sections is not None:
        p["sections"] = sections
    return p


WITH_USAGE = _product([{"kind": "usage", "body": USAGE}, {"kind": "summary", "body": "x"}])
NO_USAGE = _product([{"kind": "summary", "body": "x"}])
NO_SHEET = _product(None)


# --- instrucțiunile magazinului (PUR) ----------------------------------------------------------


def test_instructions_come_from_the_declared_sections():
    text, reason = answer_shape.howto_instructions(WITH_USAGE, PACK)
    assert reason == "instructions"
    assert text == USAGE


def test_a_loaded_sheet_without_usage_is_an_honest_no():
    """Edge case din card: fișa fără `usage` ⇒ răspunsul spune că magazinul nu are instrucțiuni
    (regula NX-307)."""
    assert answer_shape.howto_instructions(NO_USAGE, PACK) == (None, "no_instructions")


def test_a_product_without_its_sheet_is_not_a_no():
    """Produsul a venit fără fișă (dintr-o căutare): NU ȘTIM. A spune „nu am instrucțiuni" ar fi
    o minciună despre un magazin care le are pe 99,6% din produse."""
    assert answer_shape.howto_instructions(NO_SHEET, PACK) == (None, "no_sheet")


def test_pack_without_howto_sections_changes_nothing():
    assert answer_shape.howto_instructions(WITH_USAGE, DomainPack(vertical="x")) == (
        None,
        "not_declared",
    )


def test_instructions_respect_the_detail_cap():
    pack = DomainPack(
        vertical="x",
        detail_sections=(SectionSpec(kind="usage", max_chars=60),),
        howto_sections=("usage",),
    )
    text, _ = answer_shape.howto_instructions(WITH_USAGE, pack)
    assert text is not None and len(text) <= 60


# --- măsurătoarea (PUR) ------------------------------------------------------------------------


def test_answer_from_the_sheet_is_grounded():
    """Happy path din card: prima frază e instrucțiune, `explain_grounded` peste prag."""
    answer = (
        "Aplici o cantitate mica pe tenul curat, dimineata si seara. Maseaza usor pana la "
        "absorbtie si evita zona ochilor."
    )
    m = answer_shape.explain_measure(
        answer, answer.split(". ")[0], USAGE, WITH_USAGE["ai_summary"], "ro"
    )
    assert m["grounded"] >= answer_shape.EXPLAIN_GROUNDED_MIN
    assert m["grounded_below"] is False
    assert m["resold"] is False


def test_answer_from_memory_is_flagged():
    answer = "Pune doua picaturi de ser inainte de somn, apoi crema preferata."
    m = answer_shape.explain_measure(answer, answer, USAGE, WITH_USAGE["ai_summary"], "ro")
    assert m["grounded_below"] is True


def test_first_sentence_that_resells_the_product_is_flagged():
    """Defectul turului 3: prima frază redescrie produsul pe care clientul tocmai l-a văzut."""
    first = "Crema hidratanta cu vitamina C pentru luminozitate si fermitate."
    m = answer_shape.explain_measure(
        f"{first} {USAGE}", first, USAGE, WITH_USAGE["ai_summary"], "ro"
    )
    assert m["resold"] is True


# --- sufixul buclei (profilul `howto`) ----------------------------------------------------------


def test_howto_suffix_puts_the_answer_first_and_keeps_the_voice():
    suffix = turn_profile.PROFILES["howto"].suffix
    assert "Prima frază răspunde direct" in suffix
    assert naturalize(suffix) == suffix  # P13, verificat oricum la import


# --- cablajul în compunerea rich ----------------------------------------------------------------


class _LLM:
    def __init__(self, intro: str = "Se aplica pe tenul curat.", education: str | None = None):
        self._j = {
            "intro": intro,
            "items": [{"product_id": "p1", "pro_index": 0, "fit_clause": "ușoară"}],
            "pick": None,
            "education": education,
            "suggestions": [],
        }
        self.calls: list[tuple[str, str, dict]] = []

    async def embed(self, texts, *, model=None):
        return [[0.0] * 8 for _ in texts]

    async def complete(self, system, user, *, model=None):
        return "Se aplica pe tenul curat."

    async def complete_schema(self, system, user, schema, *, model=None):
        self.calls.append((system, user, schema))
        return self._j


def _ctx(body: str = "cum se foloseste prima") -> TurnContext:
    ctx = TurnContext(
        turn_id="t",
        business=BusinessConfig(id="b", slug="s", name="S"),
        contact=Contact(id="c", business_id="b"),
        message=InboundMessage(provider_msg_id="m", body=body),
        conversation_id="conv",
        state=ConversationState(),
    )
    ctx.language = "ro"
    ctx.business.domain_pack = PACK
    return ctx


async def _run(ctx: TurnContext, llm: _LLM, product: dict) -> None:
    plan = ResponsePlan(
        handled=False,
        products=[dict(product)],
        final="",
        is_order=False,
        query=ctx.message.body,
        history="",
        inp=INP,
        mode="rich",
    )
    await render(ctx, PipelineDeps(conn=object(), redis=None, llm=llm), plan)


@pytest.fixture
def howto_on(monkeypatch):
    monkeypatch.setattr(get_settings(), "howto_from_catalog_enabled", True)


async def test_store_instructions_reach_the_rich_call(howto_on):
    llm = _LLM()
    ctx = _ctx()
    await _run(ctx, llm, WITH_USAGE)
    user = llm.calls[0][1]
    assert USAGE in user
    assert "prima frază răspunde direct" in user
    event = next(e for e in ctx.events if e.type == "howto_instructions")
    assert event.properties["reason"] == "instructions"


async def test_numbers_from_the_store_survive_the_scrub(howto_on):
    """Instrucțiunile au cifre („de 2 ori pe zi"). Sunt FAPTE ale magazinului, deci pașii care le
    rostesc nu au voie să fie tăiați ca prețuri inventate."""
    product = _product([{"kind": "usage", "body": "Aplica de 2 ori pe zi pe tenul curat."}])
    llm = _LLM(education="Aplici de 2 ori pe zi, pe tenul curat.")
    ctx = _ctx()
    await _run(ctx, llm, product)
    assert ctx.reply.rich.education == "Aplici de 2 ori pe zi, pe tenul curat."


async def test_invented_numbers_are_still_scrubbed(howto_on):
    llm = _LLM(education="Aplici de 5 ori pe zi.")
    ctx = _ctx()
    await _run(ctx, llm, WITH_USAGE)
    assert ctx.reply.rich.education is None


async def test_sheet_without_usage_asks_for_an_honest_no(howto_on):
    llm = _LLM()
    ctx = _ctx()
    await _run(ctx, llm, NO_USAGE)
    assert "magazinul nu are instrucțiuni" in llm.calls[0][1]


async def test_product_without_sheet_gets_no_directive(howto_on):
    llm = _LLM()
    ctx = _ctx()
    await _run(ctx, llm, NO_SHEET)
    assert "instrucțiuni de folosire" not in llm.calls[0][1]
    event = next(e for e in ctx.events if e.type == "howto_instructions")
    assert event.properties["reason"] == "no_sheet"


async def test_flag_off_leaves_the_rich_call_untouched(monkeypatch):
    monkeypatch.setattr(get_settings(), "howto_from_catalog_enabled", False)
    llm = _LLM()
    await _run(_ctx(), llm, WITH_USAGE)
    assert USAGE not in llm.calls[0][1]


async def test_recommend_turn_never_gets_the_howto_directive(howto_on):
    llm = _LLM()
    await _run(_ctx("vreau o crema de hidratare"), llm, WITH_USAGE)
    assert USAGE not in llm.calls[0][1]


# --- raportul de runner ------------------------------------------------------------------------


def _reported(ctx: TurnContext, product: dict, intro: str, education: str | None = None):
    ctx.retrieval = SimpleNamespace(products=[product], relevance=None)
    rich = SimpleNamespace(intro=intro, education=education, items=[])
    ctx.reply = SimpleNamespace(rich=rich, text="", products=None, comparison=None)
    return response_quality.explain_shape_report(ctx)


def test_report_measures_a_howto_turn():
    report = _reported(_ctx(), WITH_USAGE, "Aplici o cantitate mica pe tenul curat.", USAGE)
    assert report is not None
    assert report["has_instructions"] is True and report["path"] == "rich"
    assert report["grounded_below"] is False


def test_report_is_silent_on_other_turns():
    assert _reported(_ctx("vreau o crema"), WITH_USAGE, "Am ales creme.") is None


def test_report_does_not_measure_without_instructions():
    report = _reported(_ctx(), NO_USAGE, "Nu am instructiuni pentru ea.")
    assert report == {"has_instructions": False, "reason": "no_instructions", "path": "rich"}
