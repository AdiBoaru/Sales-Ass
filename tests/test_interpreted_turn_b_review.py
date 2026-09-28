"""NX-336 PR B — corecturile după recenzia independentă. Fiecare test a fost scris ÎNTÂI și a picat
pe arborele livrat, înaintea reparației (vezi cardul, „Corecturi după recenzie (PR B)").

1. Prune-ul de siguranță pe calea kernelului e o ELIMINARE a produselor blocate (din ecranul de după
   prima trecere, din slotul parcat și din seturile de mai devreme), nu o înlocuire a listei.
2. Vederea de citire e scrisă ÎNAINTEA executorilor (compunerea rulează în executor); un subiect
   reluat nu e „nou".
3. Memoria întrebării se scrie doar când întrebarea a fost chiar pusă.
4. Propunerile adăugate de executori în `ctx.state_proposals` trec prin I20, cu înregistrare.
5. A doua trecere nu pierde turul: o propunere în afara mulțimii se numără și se aruncă.
6. Un răspuns existent dinaintea ramurii nu face turul „servit".
7. `search_session_aside` spune rezultatul kernelului pe un tur servit.
8. Contractul trece pe `kernel.v2.0` (I5, decizia lui Adi).

Zero model, zero DB."""

from __future__ import annotations

import dataclasses

import pytest

from src.agent import interpreted_turn as it
from src.config import get_settings
from src.conversation.delta import TurnDelta
from src.conversation.interpretation import KERNEL_CONTRACT_VERSION
from src.conversation.needs import NeedVocabulary
from src.conversation.state_reducer import ReducerPolicy, StateUpdateProposal
from src.conversation.state_v2 import (
    ConversationStateV2,
    DisplayedRef,
    ParkedTopic,
    References,
    Topic,
    active_needs,
    hydrate_state_v2,
    serialize,
)
from src.models import Reply, RetrievalResult
from src.worker import processor
from src.worker.runner import PipelineDeps
from tests.kernel import gates, replay
from tests.kernel import stage_harness as sh

JOURNEYS = {j.journey_id: j for j in replay.load_journeys()}


def _kc():
    from src.worker import kernel_commit

    return kernel_commit


def _events(ctx, name):
    return [e.properties for e in ctx.events if e.type == name]


def _doc(state):
    return serialize(state)[0]


def _ids(refs):
    return [d.product_id for d in refs]


@pytest.fixture
def electronics(monkeypatch):
    cat = sh.catalog("electronics")
    sh.install(monkeypatch, cat)
    return cat


# --- 1. prune-ul = eliminarea produselor blocate ----------------------------------------------


def _refs(*ids):
    return tuple(DisplayedRef(i, i, 10.0) for i in ids)


#: Subiectul curent: telefoanele b1, b2 pe ecran; parcat: laptopurile a1, a2. Contextul de siguranță
#: apare în tur și blochează b2: `_prune_displayed` scrie în `state_patch` ecranul minus b2.
BEFORE = ConversationStateV2(
    revision=5,
    topic=Topic(category_key="telefoane", changed_at_revision=4),
    references=References(
        displayed_products=_refs("b1", "b2"),
        displayed_revision=4,
        recent_sets=(_refs("b2", "z1"),),
    ),
    parked=ParkedTopic(topic=Topic(category_key="laptopuri"), shown=_refs("a1", "a2")),
)
PRUNED = [{"product_id": "b1", "name": "b1", "price": 10.0}]
PARK = StateUpdateProposal(
    "set_topic", category_key="casti", source="user_explicit", turn_id="t", origin="interpretation"
)


def _commit(cat, thread, *, shown=(), proposals=(), fresh=None, patch=PRUNED):
    ctx = sh.build_ctx(cat, BEFORE, "x")
    ctx.state_patch["displayed_products"] = list(patch)
    rows = [{"id": i, "name": i, "price": 1.0} for i in shown]
    ctx.reply = Reply(text="ok", products=rows)
    ctx.retrieval = RetrievalResult(products=rows, catalog_read=False)
    ctx.kernel_turn = _kc().KernelTurn(
        TurnDelta(thread=thread, proposals=proposals), (), None, False
    )
    doc = processor._build_new_state(
        _doc(fresh or BEFORE), ctx, is_rich=False, has_products=bool(shown)
    )
    return hydrate_state_v2(doc, NeedVocabulary.from_pack(cat.pack))


def _nowhere(state, pid):
    sets = [
        state.references.displayed_products,
        *state.references.recent_sets,
        state.parked.shown if state.parked else (),
    ]
    return all(pid not in _ids(s) for s in sets)


def test_r1_continue_with_new_cards_keeps_the_cards_and_removes_the_blocked(electronics):
    got = _commit(electronics, "continue", shown=("c1",))
    assert _ids(got.references.displayed_products) == ["c1"]
    assert _nowhere(got, "b2")
    assert [_ids(s) for s in got.references.recent_sets] == [["b1"], ["z1"]]
    assert got.revision == BEFORE.revision + 1


def test_r1_a_park_keeps_the_new_cards_and_parks_the_pruned_set(electronics):
    got = _commit(electronics, "continue", shown=("c1",), proposals=(PARK,))
    assert got.topic.category_key == "casti"
    assert _ids(got.references.displayed_products) == ["c1"]
    assert got.parked.topic.category_key == "telefoane"
    assert _ids(got.parked.shown) == ["b1"]
    assert _nowhere(got, "b2")


def test_r1_a_resume_without_cards_brings_the_parked_set_back(electronics):
    got = _commit(electronics, "resume")
    assert got.topic.category_key == "laptopuri"
    assert _ids(got.references.displayed_products) == ["a1", "a2"]
    assert _ids(got.parked.shown) == ["b1"]
    assert _nowhere(got, "b2")


def test_r1_a_resume_with_new_cards_keeps_them(electronics):
    got = _commit(electronics, "resume", shown=("a3",))
    assert _ids(got.references.displayed_products) == ["a3"]
    assert _nowhere(got, "b2")


def test_r1_an_aside_removes_the_blocked_and_bumps_once(electronics):
    got = _commit(electronics, "aside")
    assert _ids(got.references.displayed_products) == ["b1"]
    assert got.revision == BEFORE.revision + 1
    assert got.references.displayed_revision == BEFORE.revision + 1
    assert (got.topic, got.needs, got.active_search) == (
        BEFORE.topic,
        BEFORE.needs,
        BEFORE.active_search,
    )
    assert _nowhere(got, "b2")


def test_r1_an_aside_without_a_blocked_product_keeps_the_revision(electronics):
    whole = [{"product_id": p, "name": p, "price": 10.0} for p in ("b1", "b2")]
    got = _commit(electronics, "aside", patch=whole)
    assert got.revision == BEFORE.revision
    assert _ids(got.references.displayed_products) == ["b1", "b2"]


def test_r1_the_conflict_rebuild_removes_from_the_fresh_screen(electronics):
    """La conflictul de versiune ecranul PROASPĂT poate avea alt set: se scot doar produsele blocate
    în turul ăsta (b2), nu tot ce lipsește din lista curățată (x1 rămâne)."""
    fresh = dataclasses.replace(
        BEFORE,
        revision=9,
        references=References(displayed_products=_refs("b1", "b2", "x1"), displayed_revision=9),
    )
    got = _commit(electronics, "continue", fresh=fresh)
    assert _ids(got.references.displayed_products) == ["b1", "x1"]
    assert got.revision == 10


# --- 2. vederea e scrisă înaintea executorilor ---------------------------------------------------


async def test_r2_the_executor_composes_with_the_view_and_without_nx315(monkeypatch, electronics):
    from src.agent import finalize, turn_profile

    seen: dict = {}
    offers: list = []

    async def offer(ctx, deps, pack, products):
        offers.append(ctx.turn_id)
        return None, (), "no_partitioning_facet"

    monkeypatch.setattr(get_settings(), "narrowing_question_enabled", True)
    monkeypatch.setattr(finalize, "_narrowing_offer", offer)
    monkeypatch.setattr(turn_profile, "name_for_turn", lambda ctx: "recommend")

    async def execute(ctx, deps, planned, *rest):
        seen["view"] = ctx.kernel_view
        seen["needs"] = active_needs(ctx)
        seen["sc"] = dict(ctx.state.search_constraints)
        await finalize._turn_shape(ctx, deps, sh.product_rows(electronics, ["el-01", "el-07"]))
        ctx.set_reply("ok", cacheable=False)
        return True

    turn = JOURNEYS["k01-electronics-park-and-resume"].turns[0]
    monkeypatch.setattr(it, "execute_plans", execute)
    ctx = sh.build_ctx(electronics, ConversationStateV2(), turn.user_input)
    run = await sh.run_turn(
        monkeypatch, electronics, ctx, sh.StageLLM(turn.expect["interpretation"])
    )
    assert run.branch_result is True
    assert seen["view"] is not None and seen["needs"] != ()
    assert seen["needs"] == seen["view"].gate_state.active_needs()
    assert seen["sc"], "compunerea vede nevoile turului"
    assert offers == [], "I11: întrebarea NX-315 nu rulează pe calea kernelului"


async def test_r2_a_resumed_subject_is_not_new(monkeypatch):
    journey = JOURNEYS["k01-electronics-park-and-resume"]
    cat = sh.catalog(journey.pack)
    sh.install(monkeypatch, cat)
    item = list(sh.chain(journey, cat))[2]
    assert item.turn.expect["interpretation"].thread == "resume"
    seen: dict = {}

    async def execute(ctx, deps, planned, *rest):
        seen["new"] = ctx.kernel_view.subject_is_new if ctx.kernel_view else None
        ctx.set_reply("ok", cacheable=False)
        return True

    monkeypatch.setattr(it, "execute_plans", execute)
    ctx = sh.build_ctx(cat, item.state_before, item.turn.user_input, item.previous, turn_id="t2")
    run = await sh.run_turn(monkeypatch, cat, ctx, sh.StageLLM(item.turn.expect["interpretation"]))
    assert run.branch_result is True
    assert seen["new"] is False


# --- 3. memoria întrebării doar când întrebarea a fost pusă ---------------------------------------


async def _serve_chain_turn(monkeypatch, journey_id, index, execute):
    journey = JOURNEYS[journey_id]
    cat = sh.catalog(journey.pack)
    sh.install(monkeypatch, cat)
    item = list(sh.chain(journey, cat))[index]
    monkeypatch.setattr(it, "execute_plans", execute)
    ctx = sh.build_ctx(
        cat, item.state_before, item.turn.user_input, item.previous, turn_id=f"t{index}"
    )
    run = await sh.run_turn(monkeypatch, cat, ctx, sh.StageLLM(item.turn.expect["interpretation"]))
    assert run.branch_result is True
    return ctx, item


@pytest.mark.parametrize("asked", [True, False])
async def test_r3_a_pending_question_is_remembered_only_when_asked(monkeypatch, asked):
    async def execute(ctx, deps, planned, *rest):
        if asked:
            ctx.set_clarify("?", field="kernel", resume_route="sales")
        else:
            ctx.set_reply("ok", cacheable=False)
        return True

    ctx, item = await _serve_chain_turn(
        monkeypatch, "g02-electronics-ambiguous-cart-asks-once", 1, execute
    )
    assert item.step.outcome.asked_kind == "pending"
    ops = [p.op for p in ctx.kernel_turn.memory]
    assert ops == (["set_pending_question"] if asked else [])


@pytest.mark.parametrize("asked", [True, False])
async def test_r3_a_confirmation_is_noted_only_when_it_is_in_the_reply(monkeypatch, asked):
    question: dict = {}

    async def execute(ctx, deps, planned, *rest):
        text = "ok"
        if asked:
            text = f"ok. {question['q']}"
        ctx.set_reply(text, cacheable=False)
        return True

    journey = JOURNEYS["g10-gifts-confirm-an-implicit-recipient"]
    cat = sh.catalog(journey.pack)
    question["q"] = list(sh.chain(journey, cat))[1].step.outcome.decision.question
    ctx, item = await _serve_chain_turn(monkeypatch, journey.journey_id, 1, execute)
    assert item.step.outcome.asked_kind == "noted"
    ops = [p.op for p in ctx.kernel_turn.memory]
    assert ops == (["note_asked"] if asked else [])


# --- 4. propunerile executorilor trec prin I20, cu înregistrare -----------------------------------


@pytest.mark.parametrize("journey", ["k01-electronics-park-and-resume", "c08-aside-keeps-state"])
async def test_r4_an_executor_need_is_rejected_with_a_record(monkeypatch, electronics, journey):
    need = StateUpdateProposal("set_need", key="brand", value="Apple", source="catalog")

    async def execute(ctx, deps, planned, *rest):
        ctx.state_proposals.append(need)
        ctx.set_reply("ok", cacheable=False)
        return True

    turn = JOURNEYS[journey].turns[0]
    monkeypatch.setattr(it, "execute_plans", execute)
    ctx = sh.build_ctx(electronics, ConversationStateV2(), turn.user_input)
    run = await sh.run_turn(
        monkeypatch, electronics, ctx, sh.StageLLM(turn.expect["interpretation"])
    )
    assert run.branch_result is True
    committed = sh.commit(electronics, ctx, ConversationStateV2())
    assert all(n.normalized_value != "apple" for n in committed.active_needs())
    rejected = _events(ctx, "need_update_rejected")
    assert {"reason": "executor_state_scope", "operation": "set_need"} in [
        {k: v for k, v in e.items() if k != "turn_id"} for e in rejected
    ]


async def test_r4_an_executor_reference_is_applied(monkeypatch, electronics):
    select = StateUpdateProposal(
        "set_references", source="catalog", payload={"selected_product": "el-07"}
    )

    async def execute(ctx, deps, planned, *rest):
        ctx.state_proposals.append(select)
        ctx.set_reply("ok", cacheable=False)
        return True

    turn = JOURNEYS["k01-electronics-park-and-resume"].turns[0]
    monkeypatch.setattr(it, "execute_plans", execute)
    state = ConversationStateV2(revision=2)
    ctx = sh.build_ctx(electronics, state, turn.user_input)
    await sh.run_turn(monkeypatch, electronics, ctx, sh.StageLLM(turn.expect["interpretation"]))
    assert select in ctx.kernel_turn.executor_proposals
    committed = sh.commit(electronics, ctx, state)
    assert committed.references.selected_product == "el-07"


# --- 5. a doua trecere: numărat și aruncat, nu excepție ------------------------------------------


def test_r5_a_foreign_second_pass_proposal_is_dropped_and_counted(electronics):
    sneaky = StateUpdateProposal("set_need", key="brand", value="Samsung", source="policy")
    out = _kc().commit_kernel_turn(
        ConversationStateV2(),
        _kc().KernelTurn(TurnDelta(thread="continue"), (), None, False),
        [sneaky],
        ReducerPolicy(vocabulary=NeedVocabulary.from_pack(electronics.pack)),
    )
    assert out.state.active_needs() == ()
    assert ("set_need", "second_pass_scope") in [(r.op, r.reason) for r in out.rejected]


# --- 6. un răspuns dinaintea ramurii nu face turul servit -----------------------------------------


async def test_r6_a_reply_set_before_the_branch_does_not_count_as_served(monkeypatch, electronics):
    async def untouched(ctx, deps, planned, *rest):
        return True

    turn = JOURNEYS["k01-electronics-park-and-resume"].turns[0]
    monkeypatch.setattr(it, "execute_plans", untouched)
    ctx = sh.build_ctx(electronics, ConversationStateV2(), turn.user_input)
    ctx.set_reply("before", cacheable=False)
    deps = PipelineDeps(db=sh.RecordingDb(), llm=sh.StageLLM(turn.expect["interpretation"]))
    assert await it.run_interpreted_turn(ctx, deps) is False
    assert ctx.trace["kernel_fallback"]["reason"] == "executor_refused"
    assert ctx.kernel_turn is None and ctx.reply.text == "before"


# --- 7. telemetria parantezei pe un tur servit --------------------------------------------------


def test_r7_the_aside_session_outcome_is_the_kernels(electronics):
    ctx = sh.build_ctx(electronics, ConversationStateV2(), "x")
    ctx.reply = Reply(text="ok")
    ctx.retrieval = RetrievalResult(products=[], catalog_read=True)
    assert processor._search_session_aside_outcome(ctx) == "cleared"
    ctx.kernel_turn = _kc().KernelTurn(TurnDelta(thread="aside"), (), None, False)
    assert processor._search_session_aside_outcome(ctx) == "kept_aside"
    ctx.kernel_turn = _kc().KernelTurn(TurnDelta(thread="continue"), (), None, False)
    assert processor._search_session_aside_outcome(ctx) == "cleared"


# --- 8. contractul pe `kernel.v2.0` -----------------------------------------------------------


def _chain_states(pack):
    cat = sh.catalog(pack)
    out = []
    for journey in (j for j in JOURNEYS.values() if j.pack == pack):
        out += [item.step.state_after for item in sh.chain(journey, cat)]
    return cat, out


@pytest.mark.parametrize("pack", sh.PACKS)
def test_r8_i5_v2_an_aside_commit_moves_only_the_prune_and_the_memory(pack):
    """I5 (`kernel.v2.0`), pe commit, pe TOATE stările lanțului pur al pachetului: un `aside` cu
    executori care propun referințe și sesiune, cu un prune (orice submulțime a ecranului) și cu
    memoria întrebării lasă neatinse nevoile, subiectul, sesiunea, focusul și comparația; scoate
    doar blocatele (din ecran, seturile de mai devreme, parcat) și scrie doar memoria."""
    cat, states = _chain_states(pack)
    policy = ReducerPolicy(vocabulary=NeedVocabulary.from_pack(cat.pack))
    memory = StateUpdateProposal("note_asked", key="kernel_probe", source="policy", turn_id="t")
    checked = 0
    for state in states:
        screen = _ids(state.references.displayed_products)
        for cut in range(len(screen) + 1):
            blocked = set(screen[:cut])
            kept = [
                {"product_id": d.product_id, "name": d.name, "price": d.price}
                for d in state.references.displayed_products
                if d.product_id not in blocked
            ]
            tail = [
                StateUpdateProposal(
                    "set_references", source="catalog", payload={"displayed_products": []}
                ),
                StateUpdateProposal("set_active_search", source="catalog", payload=None),
                StateUpdateProposal(
                    "set_references", source="policy", payload={"displayed_products": kept}
                ),
            ]
            out = (
                _kc()
                .commit_kernel_turn(
                    state,
                    _kc().KernelTurn(TurnDelta(thread="aside"), (), None, False, memory=(memory,)),
                    tail,
                    policy,
                    screen_before=screen,
                )
                .state
            )
            assert (out.needs, out.topic, out.active_search, out.revocations) == (
                state.needs,
                state.topic,
                state.active_search,
                state.revocations,
            )
            refs = out.references
            assert refs.selected_product == state.references.selected_product
            assert refs.compared_products == state.references.compared_products
            assert _ids(refs.displayed_products) == [p for p in screen if p not in blocked]
            for pid in blocked:
                assert _nowhere(out, pid)
            assert out.asked("kernel_probe") is not None
            removed = any(
                pid in blocked
                for s in (
                    state.references.displayed_products,
                    *state.references.recent_sets,
                    state.parked.shown if state.parked else (),
                )
                for pid in _ids(s)
            )
            assert out.revision == state.revision + (1 if removed else 0)
            checked += 1
    assert checked


def test_r8_the_contract_is_kernel_v2_0_with_the_new_i5():
    assert KERNEL_CONTRACT_VERSION == "kernel.v2.1"
    text = (gates.ROOT / "docs" / "KERNEL-CONTRACT-v1.md").read_text(encoding="utf-8")
    assert "**Current version: `kernel.v2.1`" in text
    assert "**`kernel.v2.0` (MAJOR, NX-336 PR B" in text
    assert "identity on the CONVERSATION state" in text
    assert "replay gate is waived" in text.lower()
