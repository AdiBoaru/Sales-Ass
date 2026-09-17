"""Felia NX-296 pe calea creierului unic: mutările intră în prompt, textul iese pe poartă.

Testele din `test_chip_moves.py` verifică modulul PUR. Aici se verifică lipiturile, adică exact
locurile unde felia se poate rupe tăcut: mutările oferite modelului și cele judecate de poartă
trebuie să fie ACELEAȘI, chips-urile trebuie să ajungă pe reply pe fiecare ramură, iar starea
trebuie să rețină ce s-a oferit.

ZERO OpenAI / ZERO DB.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.agent import brain as brain_mod
from src.agent.answer_plan import (
    AnswerPlanV2,
    ChipLabel,
    PlanFacts,
    PlanObligation,
    PlanRecommendation,
    SelectedProduct,
    StyleSignals,
)
from src.agent.tool_executor import ToolRun
from src.catalog.clarify_menu import ClarifyMenu, MenuOption
from src.config import get_settings
from src.domain.loader import load_domain_pack
from src.models import (
    BusinessConfig,
    Contact,
    ConversationState,
    InboundMessage,
    Route,
    RouteDecision,
    TurnContext,
)
from src.worker.runner import PipelineDeps

RETRIEVED = [
    {
        "id": "p1",
        "business_id": "b",
        "name": "Serum Hidratant LumaDerm - cu acid hialuronic",
        "price": 89.0,
        "availability": "in_stock",
        "product_url": "https://shop/p1",
    },
    {
        "id": "p2",
        "business_id": "b",
        "name": "Crema Bogata NordSkin",
        "price": 149.0,
        "availability": "in_stock",
        "product_url": "https://shop/p2",
    },
    {
        "id": "p3",
        "business_id": "b",
        "name": "Tonic Calmant Petala",
        "price": 45.0,
        "availability": "in_stock",
        "product_url": "https://shop/p3",
    },
]

MENU = ClarifyMenu(
    options=(
        MenuOption(phrase="ten uscat", dimension="skin_type", key="dry", count=420),
        MenuOption(phrase="riduri", dimension="concerns", key="anti_aging", count=310),
        MenuOption(phrase="Machiaj", dimension="category", key="machiaj", count=680),
    ),
    reason="topic",
)


def _business() -> BusinessConfig:
    business = BusinessConfig(id="b", slug="demo", name="Demo", vertical="ecommerce")
    business.domain_pack = load_domain_pack(business)
    return business


def _ctx(body: str = "vreau un ser bun", offered=()) -> TurnContext:
    ctx = TurnContext(
        turn_id="t1",
        business=_business(),
        contact=Contact(id="c", business_id="b"),
        message=InboundMessage(provider_msg_id="m", body=body),
        conversation_id="conv",
        state=ConversationState(offered_chips=list(offered)),
        language="ro",
    )
    ctx.route = RouteDecision(route=Route.SALES)
    return ctx


def _plan(*, obligation="recommend", labels=(), n_products=3) -> AnswerPlanV2:
    ids = [p["id"] for p in RETRIEVED][:n_products]
    return AnswerPlanV2(
        schema_version=2,
        business_id="b",
        locale="ro",
        intent_summary="recomandare",
        obligations=(PlanObligation(kind=obligation, key=f"{obligation}_0"),),
        direct_answer="Uite ce se potrivește.",
        selected_products=tuple(
            SelectedProduct(product_id=i, variant_id=None, evidence_ids=(f"product:{i}:identity",))
            for i in ids
        ),
        claims=(),
        facts=PlanFacts(prices=(), stocks=(), urls=()),
        recommendations=(
            PlanRecommendation(
                product_id=ids[0],
                variant_id=None,
                reason="are acid hialuronic",
                evidence_ids=(f"product:{ids[0]}:identity",),
                need_ids=(),
            ),
        ),
        comparison=None,
        constraints_applied=(),
        unknowns=(),
        relaxations=(),
        clarification=None,
        no_results=None,
        state_update_proposals=(),
        action_intents=(),
        disclosures=(),
        confirmed_actions=(),
        style_signals=StyleSignals(tone="neutral", verbosity="short"),
        chip_labels=tuple(ChipLabel(move_id=m, text=t) for m, t in labels),
    )


def _deps() -> PipelineDeps:
    return PipelineDeps(conn=object(), redis=None, llm=SimpleNamespace())


def _run(ctx: TurnContext) -> ToolRun:
    run = ToolRun(ctx, _deps())
    run.retrieved = [dict(p) for p in RETRIEVED]
    return run


@pytest.fixture(autouse=True)
def _flags_on(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "chip_moves_enabled", True)
    monkeypatch.setattr(settings, "brain_chips_enabled", True)
    monkeypatch.setattr(settings, "brain_rich_reply_enabled", True)


@pytest.fixture
def _menu(monkeypatch):
    async def fake_menu(ctx, deps):
        return MENU

    monkeypatch.setattr("src.catalog.clarify_menu.menu_for_turn", fake_menu)


async def _chip_ctx(ctx: TurnContext) -> brain_mod.ChipContext:
    return await brain_mod._chip_context(ctx, _deps())


# --- mutările ajung la model ------------------------------------------------------------------


async def test_offer_block_reaches_the_prompt_with_ids_and_anchors(_menu) -> None:
    from src.conversation import chip_moves

    ctx = _ctx()
    chip_ctx = await _chip_ctx(ctx)
    block = chip_moves.offer_block(chip_ctx.moves, ctx.business.domain_pack, "ro", "Sugestii:")
    assert "ten uscat" in block and "Machiaj" in block
    assert "refine_facet:skin_type:dry" in block


async def test_unusable_menu_still_produces_card_chips(monkeypatch) -> None:
    """Meniu indisponibil (DB jos) nu are voie să însemne «zero sugestii»: cardurile turului
    produc mutări care nu depind de vocabular."""

    async def empty_menu(ctx, deps):
        return ClarifyMenu(reason="vocabulary_unavailable")

    monkeypatch.setattr("src.catalog.clarify_menu.menu_for_turn", empty_menu)
    ctx = _ctx()
    chip_ctx = await _chip_ctx(ctx)
    assert chip_ctx.moves == ()
    chips = brain_mod._turn_chips(ctx, _plan(), _run(ctx), chip_ctx)
    assert chips, "cardurile turului rămân o sursă de continuări"


# --- textul modelului trece prin poartă ---------------------------------------------------------


async def test_model_rephrasing_survives_when_it_keeps_the_anchor(_menu) -> None:
    ctx = _ctx()
    chip_ctx = await _chip_ctx(ctx)
    plan = _plan(
        labels=[("refine_facet:skin_type:dry", "Am ten uscat, ce imi recomanzi")],
    )
    chips = brain_mod._turn_chips(ctx, plan, _run(ctx), chip_ctx)
    assert "Am ten uscat, ce imi recomanzi" in chips


async def test_invented_suggestion_cannot_reach_the_client(_menu) -> None:
    """Defectul NX-295, exprimat pe contractul nou: modelul nu POATE emite o sugestie proprie,
    fiindcă nu există câmp în care s-o pună — doar reformulări cheiate pe mutări oferite."""
    ctx = _ctx()
    chip_ctx = await _chip_ctx(ctx)
    plan = _plan(labels=[("refine_facet:inventat:usb", "Pentru consola, cablu USB de date")])
    chips = brain_mod._turn_chips(ctx, plan, _run(ctx), chip_ctx)
    assert all("USB" not in c for c in chips)
    assert chips, "restul sugestiilor rămân, pe șablon"


async def test_broken_rephrasing_falls_back_to_template(_menu) -> None:
    ctx = _ctx()
    chip_ctx = await _chip_ctx(ctx)
    plan = _plan(labels=[("refine_facet:skin_type:dry", "Vreau ceva foarte bun")])
    chips = brain_mod._turn_chips(ctx, plan, _run(ctx), chip_ctx)
    assert "Caut ceva pentru ten uscat" in chips
    assert "Vreau ceva foarte bun" not in chips


# --- chips-urile ajung pe reply, pe fiecare ramură ----------------------------------------------


async def test_rich_reply_carries_chips(_menu) -> None:
    ctx = _ctx()
    chip_ctx = await _chip_ctx(ctx)
    await brain_mod._set_brain_reply(ctx, _deps(), _plan(), _run(ctx), "Uite.", chip_ctx)
    assert ctx.reply is not None
    labels = [c.label for c in (ctx.reply.rich.chips if ctx.reply.rich else [])]
    assert labels, "ramura bogată emite chips"
    assert len(labels) <= get_settings().chip_slots


async def test_prose_branch_carries_chips(_menu) -> None:
    """Un răspuns fără carduri nu e o fundătură (P6, ca `_attach_no_result_alternatives`)."""
    ctx = _ctx()
    chip_ctx = await _chip_ctx(ctx)
    run = ToolRun(ctx, _deps())  # fără retrieval ⇒ fără carduri ⇒ ramura de proză
    plan = _plan(n_products=1)
    await brain_mod._set_brain_reply(ctx, _deps(), plan, run, "Nu am gasit.", chip_ctx)
    assert ctx.reply is not None and ctx.reply.suggestions


# --- turul factual nu primește filtre -----------------------------------------------------------


async def test_factual_turn_gets_product_chips_not_refinements(_menu) -> None:
    ctx = _ctx("care e pretul la LumaDerm?")
    chip_ctx = await _chip_ctx(ctx)
    chips = brain_mod._turn_chips(ctx, _plan(obligation="answer"), _run(ctx), chip_ctx)
    assert chips
    assert "Caut ceva pentru ten uscat" not in chips
    assert any("LumaDerm" in c or "Serum Hidratant" in c for c in chips)


# --- anti-repetiție ------------------------------------------------------------------------------


async def test_offered_moves_are_remembered_and_not_repeated(_menu) -> None:
    ctx = _ctx()
    chip_ctx = await _chip_ctx(ctx)
    first = brain_mod._turn_chips(ctx, _plan(), _run(ctx), chip_ctx)
    remembered = ctx.state_patch["offered_chips"]
    assert remembered, "ce s-a oferit se persistă, ca ref-uri scurte"
    assert all(":" in move_id for move_id in remembered)

    # Turul următor pornește cu memoria turului trecut.
    ctx2 = _ctx(offered=remembered)
    chip_ctx2 = await _chip_ctx(ctx2)
    second = brain_mod._turn_chips(ctx2, _plan(), _run(ctx2), chip_ctx2)
    assert set(second).isdisjoint(set(first)) or not second


async def test_memory_is_bounded(_menu) -> None:
    from src.models import MAX_OFFERED_CHIPS

    ctx = _ctx(offered=[f"refine_facet:x:{i}" for i in range(MAX_OFFERED_CHIPS + 10)])
    chip_ctx = await _chip_ctx(ctx)
    brain_mod._turn_chips(ctx, _plan(), _run(ctx), chip_ctx)
    assert len(ctx.state_patch["offered_chips"]) <= MAX_OFFERED_CHIPS


# --- kill-switch ----------------------------------------------------------------------------------


async def test_kill_switch_restores_the_old_chips(monkeypatch, _menu) -> None:
    """OFF = chips-urile de azi: etichetele seci ale meniului, prin `_clarify_chips`."""
    monkeypatch.setattr(get_settings(), "chip_moves_enabled", False)
    ctx = _ctx()
    await brain_mod._set_brain_reply(ctx, _deps(), _plan(), _run(ctx), "Uite.", None)
    labels = [c.label for c in (ctx.reply.rich.chips if ctx.reply and ctx.reply.rich else [])]
    assert "ten uscat" in labels
    assert "offered_chips" not in ctx.state_patch
