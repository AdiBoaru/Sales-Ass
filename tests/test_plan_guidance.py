"""NX-315 felia 1 — „cum alegi" devine obligatoriu acolo unde setul servit îl justifică.

Pe turul real «vreau o crema de hidratare» iZi a pus un paragraf „cum alegi" (tip de ten, textură,
doar hidratare sau și antirid); Nativx n-a pus nimic. Pe calea v1, care rulează în producție,
slotul există (`education`), dar promptul rich îl declară opțional („REGULA DE AUR… mai bine gol").

Acum serverul cere slotul DOAR când contractul de formă NX-299 îl cere (≥2 carduri și ≥1 axă de
decizie), numește axele din date, iar lipsa are nume și cod (`guidance_dropped`). Cardul inițial
propunea un câmp nou pe `AnswerPlanV2`, gândit pentru creierul unic; creierul e stins în producție
din 2026-09-17, deci slotul s-a ancorat pe calea care chiar răspunde clientului.
"""

from __future__ import annotations

import pytest

from src.agent import answer_shape
from src.agent import finalize as finalize_mod
from src.agent.finalize import _rich_bundle, _rich_facets, render
from src.agent.planner import ResponsePlan
from src.agent.prompt_builder import PromptInputs
from src.config import get_settings
from src.domain.pack import DomainPack, FacetSpec
from src.models import BusinessConfig, Contact, ConversationState, InboundMessage, TurnContext
from src.worker import compose
from src.worker.runner import PipelineDeps

INP = PromptInputs.build("S", "ecommerce", "ro", ["Ten"], [])
PACK = DomainPack(
    vertical="ecommerce",
    comparison_facets=(FacetSpec(key="skin_type", labels={"ro": "Tip de ten"}),),
    howto_sections=("usage",),
)


def _p(i: int, skin: str, price: float) -> dict:
    return {
        "id": f"p{i}",
        "name": f"Crema {i}",
        "price": price,
        "url": f"https://shop/p{i}",
        "availability": "in_stock",
        "ai_summary": "crema hidratanta",
        "attributes": {"skin_type": skin, "product_type": "crema de fata"},
    }


VARIED = [_p(1, "dry", 40.0), _p(2, "oily", 90.0), _p(3, "sensitive", 60.0)]
QUERY = "vreau o crema de hidratare"


class _LLM:
    def __init__(self, education: str | None = None):
        self._education = education
        self.calls: list[tuple[str, str, dict]] = []

    async def embed(self, texts, *, model=None):
        return [[0.0] * 8 for _ in texts]

    async def complete(self, system, user, *, model=None):
        return "Uite câteva creme."

    async def complete_schema(self, system, user, schema, *, model=None):
        self.calls.append((system, user, schema))
        return {
            "intro": "Am ales creme hidratante.",
            "items": [
                {"product_id": p["id"], "pro_index": 0, "fit_clause": "ușoară"} for p in VARIED
            ],
            "pick": None,
            "education": self._education,
            "suggestions": [],
        }


def _ctx(body: str = QUERY) -> TurnContext:
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


def _plan(products: list[dict]) -> ResponsePlan:
    return ResponsePlan(
        handled=False,
        products=[dict(p) for p in products],
        final="",
        is_order=False,
        query=QUERY,
        history="",
        inp=INP,
        mode="rich",
    )


async def _run(ctx: TurnContext, llm: _LLM, products: list[dict] = VARIED) -> None:
    await render(ctx, PipelineDeps(conn=object(), redis=None, llm=llm), _plan(products))


@pytest.fixture
def guidance_on(monkeypatch):
    monkeypatch.setattr(get_settings(), "guidance_required_enabled", True)


# --- toate flagurile OFF: nimic nu se schimbă ---------------------------------------------------


@pytest.fixture
def all_off(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "guidance_required_enabled", False)
    monkeypatch.setattr(s, "narrowing_question_enabled", False)
    monkeypatch.setattr(s, "howto_from_catalog_enabled", False)


@pytest.mark.parametrize("body", [QUERY, "cum se foloseste prima"])
async def test_all_flags_off_prompt_and_schema_are_byte_identical(all_off, body):
    """DoD: toate flagurile OFF ⇒ mesajul și schema byte-identice cu forma de dinainte.

    Referința e forma VECHE, reconstruită aici din aceleași piese, nu o captură a codului nou:
    altfel testul ar compara codul cu el însuși."""
    ctx = _ctx(body)
    llm = _LLM()
    await _run(ctx, llm)
    system, user, schema = llm.calls[0]
    products = [dict(p) for p in VARIED]
    axes = compose.decision_axes(products, _rich_facets(ctx), "ro")
    axes_block = (
        "Axe pe care variază setul (folosește-le în intro și la segmentare): "
        + " | ".join(axes)
        + "\n"
        if axes and get_settings().decision_axes_enabled
        else ""
    )
    expected = (
        f"Limba clientului: ro\nNevoia clientului: {QUERY}\n{axes_block}\n"
        f"Produse disponibile (alege dintre acestea):\n"
        f"{_rich_bundle(products, _rich_facets(ctx), 'ro')}"
    )
    assert user == expected
    assert schema is finalize_mod._RICH_SCHEMA


# --- felia 1 ------------------------------------------------------------------------------------


async def test_guidance_is_required_with_the_axes_named(guidance_on):
    """Happy path: ≥2 carduri, o axă reală (tipul de ten) ⇒ linia care face slotul obligatoriu,
    cu axele din DATE, nu inventate de model."""
    llm = _LLM(education="Alege după tipul de ten. Pentru ten uscat, Crema 1.")
    ctx = _ctx()
    await _run(ctx, llm)
    user = llm.calls[0][1]
    assert "`education` NU e opțională" in user
    assert "Tip de ten" in user.split("NU e opțională", 1)[1]
    assert ctx.reply.rich.education  # a ieșit
    assert not any(e.type == "guidance_dropped" for e in ctx.events)


async def test_single_card_does_not_ask_for_guidance(guidance_on):
    """Edge case din card: un singur card ⇒ `closing` nu se cere, deci nici „cum alegi"."""
    llm = _LLM()
    await _run(_ctx(), llm, products=VARIED[:1])
    assert "NU e opțională" not in llm.calls[0][1]


async def test_flat_set_does_not_ask_for_guidance(guidance_on):
    """Fără axă de decizie nu există criteriu onest de numit: un paragraf generic e exact
    umplutura pe care regula de aur o interzice pe bună dreptate."""
    flat = [_p(1, "dry", 50.0), _p(2, "dry", 55.0)]
    llm = _LLM()
    await _run(_ctx(), llm, products=flat)
    assert "NU e opțională" not in llm.calls[0][1]


async def test_missing_guidance_is_named(guidance_on):
    ctx = _ctx()
    await _run(ctx, _LLM(education=None))
    event = next(e for e in ctx.events if e.type == "guidance_dropped")
    assert event.properties["reason"] == "missing"


async def test_guidance_with_an_invented_number_is_emptied_without_a_retry(guidance_on):
    """Failure case din card: o cifră negroundată ⇒ scrub-ul o prinde, slotul se golește, iar
    turul NU plătește o rundă în plus (un singur apel structurat)."""
    llm = _LLM(education="Crema 1 hidratează 72 de ore.")
    ctx = _ctx()
    await _run(ctx, llm)
    assert ctx.reply.rich is not None and ctx.reply.rich.education is None
    assert len(llm.calls) == 1
    event = next(e for e in ctx.events if e.type == "guidance_dropped")
    assert event.properties["reason"] == "scrubbed"


async def test_explain_turn_is_not_asked_for_guidance(guidance_on):
    """Forma e a obligației: pe „cum se folosește" nu se cer criterii de alegere."""
    llm = _LLM()
    await _run(_ctx("cum se foloseste prima"), llm)
    assert "NU e opțională" not in llm.calls[0][1]


def test_directive_without_axes_is_silent():
    assert answer_shape.guidance_directive(()) is None
    assert answer_shape.axis_names(["Tip de ten: uscat / gras", "Preț: de la 40 la 90 lei"]) == (
        "Tip de ten",
        "Preț",
    )
