"""NX-336 PR B — pasul 6 al kernelului: starea trece DOAR prin reducer.

Ce dovedește suita (cardul, „Implementation Steps → PR B", regresiile întâi):

- `_sender_tail` e extras din `_turn_proposals` fără schimbare de comportament (paritate cu corpul
  de pe `main`, copiat aici), iar cu `kernel_turn` absent commit-ul e `reduce_all` de azi (I16);
- `kernel_commit`: două treceri, a doua cu mulțimea FIXĂ (memoria întrebării + prune-ul de
  siguranță), `aside` păstrează prune-ul și memoria, revizia crește o singură dată, I20 la rulare,
  conflictul de versiune re-derivă turul pe starea proaspătă;
- orchestratorul: `ctx.kernel_turn` scris doar pe un tur SERVIT (executor sintetic), niciodată pe
  fallback; vederea de citire (`VIEW_FIELDS` + `kernel_view`) scrisă ÎNAINTEA executorilor și
  anulată pe fallback; `active_needs`/`subject_is_new` din starea porții; întrebarea NX-315
  oprită (I11);
- I3 la rulare: pe un tur servit nu rulează niciun scriitor vechi, iar propunerile lui
  `clarify_resume` nu intră în commit; întrebarea răspunsă se închide (`resolve_question`);
- harnessul la nivel de procesor: journey-urile × 5 pachete, servite de un executor sintetic, cu
  commit-ul REAL între ture: starea după fiecare tur == `fixture_catalog.kernel_step`.

Zero model, zero DB (stub-urile din `tests/kernel/stage_harness.py`)."""

from __future__ import annotations

import dataclasses
import json

import pytest

from src.agent import interpreted_turn as it
from src.config import get_settings
from src.conversation.delta import TurnDelta
from src.conversation.kernel_trace import KernelTrace
from src.conversation.needs import NeedVocabulary
from src.conversation.state_reducer import (
    ReducerPolicy,
    StateUpdateProposal,
    reduce_all,
    reduce_turn,
)
from src.conversation.state_v2 import (
    ConversationStateV2,
    DisplayedRef,
    PendingClarification,
    References,
    active_needs,
    hydrate_state_v2,
    project_v1,
    serialize,
)
from src.models import ConversationState, Reply
from src.worker import processor
from src.worker.stages import agent as agent_mod
from tests.kernel import fixture_catalog, replay
from tests.kernel import stage_harness as sh

JOURNEYS = {j.journey_id: j for j in replay.load_journeys()}


def _kc():
    """`src.worker.kernel_commit`, importat în test: pe `main` (fără modul) fiecare test pică
    individual, nu toată suita la colectare."""
    from src.worker import kernel_commit

    return kernel_commit


def _interp(journey_id: str, index: int):
    return JOURNEYS[journey_id].turns[index].expect["interpretation"]


def _events(ctx, name):
    return [e.properties for e in ctx.events if e.type == name]


def _doc(state: ConversationStateV2) -> dict:
    return serialize(state)[0]


@pytest.fixture
def electronics(monkeypatch):
    cat = sh.catalog("electronics")
    sh.install(monkeypatch, cat)
    return cat


def _refs(cat, *ids) -> tuple[DisplayedRef, ...]:
    return tuple(DisplayedRef(p, cat.items[p]["name"], float(cat.items[p]["price"])) for p in ids)


def _prune(cat, *ids) -> StateUpdateProposal:
    return StateUpdateProposal(
        "set_references",
        source="policy",
        turn_id="t",
        payload={
            "displayed_products": [
                {"product_id": p, "name": cat.items[p]["name"], "price": cat.items[p]["price"]}
                for p in ids
            ]
        },
    )


def _policy(cat) -> ReducerPolicy:
    return ReducerPolicy(vocabulary=NeedVocabulary.from_pack(cat.pack))


def _pending(key="storage", revision=4) -> PendingClarification:
    return PendingClarification(
        question_id=f"q:{key}:{revision}:1", target_key=key, asked_at_revision=revision
    )


# --- 1. `_sender_tail`: extras fără schimbare de comportament (OFF) -------------------------------


def _main_turn_proposals(ctx, *, is_rich, has_products):
    """Corpul lui `processor._turn_proposals` de pe `main@e762487`, copiat LITERAL: paritatea
    extragerii se verifică pe comportamentul de dinainte, nu pe noua funcție."""
    proposals = list(ctx.state_proposals)
    if (is_rich or has_products) and ctx.reply is not None and ctx.reply.products:
        proposals.append(
            StateUpdateProposal(
                "set_references",
                source="catalog",
                turn_id=ctx.turn_id,
                payload={
                    "displayed_products": processor._displayed_product_refs(ctx.reply.products)
                },
            )
        )
    if "active_search" in ctx.state_patch:
        proposals.append(
            StateUpdateProposal(
                "set_active_search", source="catalog", payload=ctx.state_patch["active_search"]
            )
        )
    elif not (is_rich or has_products) and not processor._keeps_search_session(ctx):
        proposals.append(StateUpdateProposal("set_active_search", source="catalog", payload=None))
    if "displayed_products" in ctx.state_patch:
        proposals.append(
            StateUpdateProposal(
                "set_references",
                source="policy",
                turn_id=ctx.turn_id,
                payload={"displayed_products": ctx.state_patch["displayed_products"]},
            )
        )
    return proposals


def _tail_ctx(cat, scenario):
    from src.models import RetrievalResult

    ctx = sh.build_ctx(cat, ConversationStateV2(), "x")
    ctx.state_proposals.append(StateUpdateProposal("set_need", key="brand", value="Samsung"))
    rows = sh.product_rows(cat, ["el-01", "el-02"])
    if scenario == "products":
        ctx.reply = Reply(text="ok", products=rows)
    elif scenario == "search":
        ctx.reply = Reply(text="ok", products=rows)
        ctx.state_patch["active_search"] = {"fp": "f", "pool": ["el-01"], "cursor": 0, "page": 0}
    elif scenario == "close":
        ctx.reply = Reply(text="ok")
        ctx.retrieval = RetrievalResult(products=[], catalog_read=True)
    elif scenario == "aside":
        ctx.reply = Reply(text="ok")
        ctx.retrieval = RetrievalResult(products=[], catalog_read=False)
    elif scenario == "clarify":
        ctx.reply = Reply(text="?", pending_question={"field": "storage"})
    elif scenario == "prune":
        ctx.reply = Reply(text="ok", products=rows)
        ctx.state_patch["displayed_products"] = [{"product_id": "el-02", "name": "x", "price": 1.0}]
    return ctx


SCENARIOS = ("products", "search", "close", "aside", "clarify", "prune")


@pytest.mark.parametrize("scenario", SCENARIOS)
@pytest.mark.parametrize("keeps", [True, False])
def test_turn_proposals_equal_the_main_body_and_split_into_the_sender_tail(
    electronics, monkeypatch, scenario, keeps
):
    monkeypatch.setattr(get_settings(), "aside_keeps_search_session_enabled", keeps)
    ctx = _tail_ctx(electronics, scenario)
    has_products = bool(ctx.reply.products)
    got = processor._turn_proposals(ctx, is_rich=False, has_products=has_products)
    assert got == _main_turn_proposals(ctx, is_rich=False, has_products=has_products)
    tail = processor._sender_tail(ctx, is_rich=False, has_products=has_products)
    assert got == [*ctx.state_proposals, *tail]
    assert all(p.op in ("set_references", "set_active_search") for p in tail)


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_without_a_kernel_turn_the_commit_is_todays_reduce_all(electronics, scenario):
    """I16 pe commit: `kernel_turn` absent ⇒ `reduce_all(hidratat, _turn_proposals)`, exact ca pe
    `main` (diferențialul CI rulează cu stările v2 stinse, deci ramura v2 e acoperită aici)."""
    ctx = _tail_ctx(electronics, scenario)
    assert ctx.kernel_turn is None
    base = _doc(ConversationStateV2(revision=2, references=References(_refs(electronics, "el-03"))))
    has = bool(ctx.reply.products)
    state, reduced = processor._build_state_v2(base, ctx, is_rich=False, has_products=has)
    expected = reduce_all(
        hydrate_state_v2(base, NeedVocabulary.from_pack(electronics.pack)),
        _main_turn_proposals(ctx, is_rich=False, has_products=has),
        processor._reducer_policy(ctx),
    )
    assert (reduced.applied, reduced.rejected) == (expected.applied, expected.rejected)
    assert _doc(dataclasses.replace(state, passthrough={})) == _doc(
        dataclasses.replace(expected.state, passthrough={})
    )


# --- 2. `kernel_commit`: două treceri, a doua fixată ---------------------------------------------

MEMORY = StateUpdateProposal("set_pending_question", key="storage", source="policy", turn_id="t")
NOTE = StateUpdateProposal("note_asked", key="storage", source="policy", turn_id="t")


FOREIGN = [
    StateUpdateProposal("set_need", key="brand", value="Samsung", source="policy"),
    StateUpdateProposal("set_topic", category_key="telefoane", source="policy"),
    StateUpdateProposal("revoke", key="brand", source="policy"),
    StateUpdateProposal("resolve_question", key="storage", source="policy"),
    StateUpdateProposal("set_active_search", source="policy", payload=None),
    StateUpdateProposal("set_references", source="policy", payload={"selected_product": "el-01"}),
    StateUpdateProposal(
        "set_references",
        source="policy",
        payload={"displayed_products": [], "selected_product": "el-01"},
    ),
    StateUpdateProposal("set_cart_ref", source="policy", payload={"ref": "c"}),
]


@pytest.mark.parametrize("proposal", FOREIGN)
@pytest.mark.parametrize("where", ["memory", "tail"])
def test_the_second_pass_drops_and_counts_anything_but_the_memory_and_the_prune(
    electronics, proposal, where
):
    """Mulțimea FIXĂ a celei de-a doua treceri: memoria întrebării + eliminarea produselor blocate.
    Orice altceva se aruncă și se numără (`second_pass_scope`), iar turul NU se pierde (P6)."""
    memory = (MEMORY, proposal) if where == "memory" else (MEMORY,)
    tail = [proposal] if where == "tail" else []
    kept, dropped = _kc().second_pass_input(memory, tail, frozenset())
    assert [p.op for p in kept] == ["set_pending_question"]
    assert [(r.op, r.reason) for r in dropped] == [(proposal.op, "second_pass_scope")]
    out = _kc().commit_kernel_turn(
        _screen_state(electronics),
        _kc().KernelTurn(TurnDelta(thread="continue"), (), None, False, memory=memory),
        tail,
        _policy(electronics),
    )
    assert (proposal.op, "second_pass_scope") in [(r.op, r.reason) for r in out.rejected]
    assert out.state.active_needs() == () and out.state.pending_clarification is not None


def test_the_second_pass_consumes_the_v1_prune_as_a_removal(electronics):
    kept, dropped = _kc().second_pass_input(
        (MEMORY, NOTE), [_prune(electronics, "el-01")], frozenset({"el-02"})
    )
    assert [p.op for p in kept] == ["set_pending_question", "note_asked", "prune_products"]
    assert kept[-1].payload == {"product_ids": ["el-02"]} and dropped == ()
    assert set(_kc().SECOND_PASS_OPS) == {"set_pending_question", "note_asked", "prune_products"}


def test_blocked_ids_are_the_loaded_screen_minus_the_pruned_list(electronics):
    screen = ["el-01", "el-02", "el-03"]
    assert _kc().blocked_ids(screen, [_prune(electronics, "el-01", "el-03")]) == {"el-02"}
    assert _kc().blocked_ids(screen, []) == frozenset()


def test_i20_at_runtime_an_executor_need_is_rejected(electronics):
    """I20: ce scriu executorii trece prin commit ca propunere de executor; orice nu e referință
    sau sesiune se respinge CU înregistrare (`executor_state_scope`), și pe `aside`."""
    need = StateUpdateProposal("set_need", key="brand", value="Samsung", source="catalog")
    for thread in ("continue", "aside"):
        out = _kc().commit_kernel_turn(
            ConversationStateV2(),
            _kc().KernelTurn(TurnDelta(thread=thread), (), None, False),
            [need],
            _policy(electronics),
        )
        assert [(r.op, r.reason) for r in out.rejected] == [("set_need", "executor_state_scope")]
        assert out.state.active_needs() == ()


def _screen_state(cat, revision=4) -> ConversationStateV2:
    return ConversationStateV2(
        revision=revision,
        references=References(
            displayed_products=_refs(cat, "el-01", "el-02", "el-03"),
            displayed_revision=revision,
        ),
    )


SCREEN = ("el-01", "el-02", "el-03")


def test_an_aside_keeps_the_safety_prune_and_the_question_memory(electronics):
    """I5 (`kernel.v2.0`): pe `aside` prima trecere e identitatea, iar a doua aplică totuși memoria
    întrebării și eliminarea produselor blocate; revizia crește O dată, ca un ordinal pe lista veche
    să iasă `stale`."""
    before = _screen_state(electronics)
    turn = _kc().KernelTurn(TurnDelta(thread="aside"), (), None, False, memory=(MEMORY,))
    out = _kc().commit_kernel_turn(
        before,
        turn,
        [_prune(electronics, "el-01", "el-03")],
        _policy(electronics),
        screen_before=SCREEN,
    )
    shown = [d.product_id for d in out.state.references.displayed_products]
    assert shown == ["el-01", "el-03"]
    assert out.state.pending_clarification is not None
    assert out.state.pending_clarification.target_key == "storage"
    assert out.state.revision == before.revision + 1
    assert out.state.references.displayed_revision == before.revision + 1
    assert out.state.needs == before.needs and out.state.topic == before.topic


def test_an_aside_without_a_list_change_keeps_the_revision(electronics):
    before = _screen_state(electronics)
    same = _prune(electronics, "el-01", "el-02", "el-03")
    out = _kc().commit_kernel_turn(
        before,
        _kc().KernelTurn(TurnDelta(thread="aside"), (), None, False, memory=(NOTE,)),
        [same],
        _policy(electronics),
        screen_before=SCREEN,
    )
    assert out.state.revision == before.revision
    assert out.state.references.displayed_revision == before.revision
    assert out.state.asked("storage") is not None


def test_a_prune_on_a_continue_turn_bumps_the_revision_once(electronics):
    before = _screen_state(electronics)
    out = _kc().commit_kernel_turn(
        before,
        _kc().KernelTurn(TurnDelta(thread="continue"), (), None, False, memory=(MEMORY,)),
        [_prune(electronics, "el-02")],
        _policy(electronics),
        screen_before=SCREEN,
    )
    assert [d.product_id for d in out.state.references.displayed_products] == ["el-02"]
    assert out.state.revision == before.revision + 1
    assert out.state.references.displayed_revision == before.revision + 1
    assert out.state.pending_clarification.asked_at_revision == before.revision + 1


def test_the_v1_prune_is_unchanged_off_the_kernel_path(electronics):
    """Calea v1 (flag stins) NU se atinge: prune-ul rămâne o ÎNLOCUIRE a listei, pus ultimul, deci
    pe un tur cu carduri noi setul vechi curățat îl înlocuiește pe cel nou. Defect latent DECLARAT
    (cardul, „Corecturi după recenzie (PR B)"), candidat pentru un card propriu; pe calea kernelului
    prune-ul e o eliminare (`tests/test_interpreted_turn_b_review.py`, r1)."""
    before = _screen_state(electronics)
    shown = StateUpdateProposal(
        "set_references",
        source="catalog",
        payload={"displayed_products": [d.to_jsonb() for d in _refs(electronics, "el-04")]},
    )
    v1 = reduce_all(before, [shown, _prune(electronics, "el-01")], _policy(electronics))
    assert [d.product_id for d in v1.state.references.displayed_products] == ["el-01"]
    kernel = _kc().commit_kernel_turn(
        before,
        _kc().KernelTurn(TurnDelta(thread="continue"), (), None, False),
        [shown, _prune(electronics, "el-01")],
        _policy(electronics),
        screen_before=SCREEN,
    )
    assert [d.product_id for d in kernel.state.references.displayed_products] == ["el-04"]


def test_both_passes_report_their_records(electronics):
    """`need_update_rejected` vede și respingerile celei de-a doua treceri (întrebare deja vie)."""
    before = dataclasses.replace(_screen_state(electronics), pending_clarification=_pending())
    out = _kc().commit_kernel_turn(
        before,
        _kc().KernelTurn(TurnDelta(thread="aside"), (), None, False, memory=(MEMORY,)),
        [],
        _policy(electronics),
    )
    assert ("set_pending_question", "already_pending") in [(r.op, r.reason) for r in out.rejected]


def test_the_memory_second_pass_is_the_fixtures_form(electronics):
    """Paritatea cu `kernel_step`: memoria se aplică cu `reduce_all` pe revizia fixată a turului
    (forma producției; `kernel_step` a fost aliniat, nu invers)."""
    before = _screen_state(electronics)
    delta = TurnDelta(thread="continue")
    policy = _policy(electronics)
    first = reduce_turn(before, delta, (), (), None, False, policy).state
    expected = reduce_all(first, (MEMORY,), policy, revision=first.revision).state
    got = _kc().commit_kernel_turn(
        before, _kc().KernelTurn(delta, (), None, False, (MEMORY,)), [], policy
    )
    assert got.state == expected


# --- 3. commit-ul procesorului pe un tur servit -------------------------------------------------


def _kernel_ctx(cat, state, *, kernel_turn, proposals=(), reply=None):
    ctx = sh.build_ctx(cat, state, "x")
    ctx.reply = reply or Reply(text="ok")
    ctx.kernel_turn = kernel_turn
    ctx.state_proposals.extend(proposals)
    return ctx


@pytest.mark.parametrize("thread", ["continue", "aside"])
def test_the_kernel_commit_ignores_the_stage_proposals(electronics, thread):
    """Propunerile stagiilor (aici forma celor ale lui `clarify_resume`: răspunsul brut ca nevoie)
    NU intră în commit-ul unui tur servit de kernel: nevoile vin doar din interpretare."""
    state = dataclasses.replace(_screen_state(electronics), pending_clarification=_pending())
    clarify = [
        StateUpdateProposal("resolve_question", key="storage", source="user_explicit"),
        StateUpdateProposal("set_need", key="storage", value="256", source="user_explicit"),
        StateUpdateProposal("set_need", key="brand", value="Samsung", source="user_explicit"),
    ]
    ctx = _kernel_ctx(
        electronics,
        state,
        kernel_turn=_kc().KernelTurn(TurnDelta(thread=thread), (), None, False),
        proposals=clarify,
    )
    got, reduced = processor._build_state_v2(_doc(state), ctx, is_rich=False, has_products=False)
    assert got.active_needs() == ()
    assert got.pending_clarification == state.pending_clarification
    assert all(r.op != "set_need" for r in reduced.applied)
    # nici măcar nu ajung la reducer (altfel ar apărea ca respingeri `executor_state_scope`)
    assert reduced.rejected == ()


def test_the_version_conflict_rebuild_rederives_the_kernel_turn_on_fresh_state(electronics):
    """Conflictul de versiune: `rebuild_state` cheamă `_build_new_state(proaspăt, ctx)`, care
    re-aplică ACEEAȘI intrare a kernelului pe starea proaspătă (pur), fără să piardă ce a scris
    celălalt writer între timp."""
    base = _screen_state(electronics)
    fresh = dataclasses.replace(base, revision=7, pending_clarification=_pending(revision=7))
    need = StateUpdateProposal(
        "set_need", key="brand", value="Samsung", source="user_explicit", turn_id="t"
    )
    turn = _kc().KernelTurn(TurnDelta(thread="continue", proposals=(need,)), (), None, False)
    ctx = _kernel_ctx(electronics, base, kernel_turn=turn)
    doc = processor._build_new_state(_doc(fresh), ctx, is_rich=False, has_products=False)
    rebuilt = hydrate_state_v2(doc, NeedVocabulary.from_pack(electronics.pack))
    tail = processor._sender_tail(ctx, is_rich=False, has_products=False)
    expected = _kc().commit_kernel_turn(fresh, turn, tail, processor._reducer_policy(ctx)).state
    assert _doc(rebuilt) == _doc(expected)
    assert rebuilt.revision == 8 and rebuilt.pending_clarification is not None
    assert [n.key for n in rebuilt.active_needs()] == ["brand"]
    # pe baza de la încărcare, același tur dă alt document: re-derivat, nu copiat
    stale = processor._build_new_state(_doc(base), ctx, is_rich=False, has_products=False)
    assert stale != doc


def test_second_pass_rejections_reach_the_events(electronics):
    state = dataclasses.replace(_screen_state(electronics), pending_clarification=_pending())
    ctx = _kernel_ctx(
        electronics,
        state,
        kernel_turn=_kc().KernelTurn(TurnDelta(thread="aside"), (), None, False, memory=(MEMORY,)),
    )
    processor._build_new_state(_doc(state), ctx, is_rich=False, has_products=False)
    assert {"reason": "already_pending", "operation": "set_pending_question", "turn_id": "t0"} in (
        _events(ctx, "need_update_rejected")
    )


# --- 4. orchestratorul: servit vs fallback -------------------------------------------------------

G01 = ("g01-electronics-cart-on-an-exact-target", 0)
K01 = ("k01-electronics-park-and-resume", 0)


async def _served(monkeypatch, cat, journey=K01, *, state=None, executor=None, previous=()):
    turn = JOURNEYS[journey[0]].turns[journey[1]]
    monkeypatch.setattr(it, "execute_plans", executor or sh.synthetic_executor(cat, turn.shown))
    ctx = sh.build_ctx(cat, state or ConversationStateV2(), turn.user_input, previous)
    return await sh.run_turn(monkeypatch, cat, ctx, sh.StageLLM(turn.expect["interpretation"]))


async def test_a_served_turn_writes_the_commit_input_and_returns_true(monkeypatch, electronics):
    run = await _served(monkeypatch, electronics)
    ctx = run.ctx
    assert run.branch_result is True
    assert run.llm.loops == [], "calea v1 nu mai rulează pe un tur servit"
    assert isinstance(ctx.kernel_turn, _kc().KernelTurn)
    assert ctx.kernel_turn.delta.proposals, "nevoile turului vin din interpretare"
    [turn] = _events(ctx, "kernel_turn")
    assert turn == {
        "served": True,
        "executor": "search",
        "plans": 1,
        "fallback_reason": None,
        "turn_id": "t0",
    }
    trace = KernelTrace.model_validate(ctx.trace["kernel"])
    assert trace.executor == "search" and "kernel_fallback" not in ctx.trace


async def test_a_dark_seam_keeps_the_turn_dark(monkeypatch, electronics):
    """Un seam care întoarce `None` (harnessul implicit; în producție, un executor încă nelegat,
    vezi `test_interpreted_turn_c`) lasă turul DARK, ca în PR A."""
    turn = JOURNEYS[K01[0]].turns[0]
    ctx = sh.build_ctx(electronics, ConversationStateV2(), turn.user_input)
    run = await sh.run_turn(
        monkeypatch, electronics, ctx, sh.StageLLM(turn.expect["interpretation"])
    )
    assert run.branch_result is False and ctx.kernel_turn is None and ctx.kernel_view is None
    assert _events(ctx, "kernel_turn")[0]["fallback_reason"] == "dark"


async def _refuse(ctx, deps, planned, *rest):
    ctx.retrieval = None
    ctx.state_patch["active_search"] = {"fp": "leak", "pool": ["el-01"], "cursor": 0, "page": 0}
    ctx.routine = object()
    return False


async def _true_silent(ctx, deps, planned, *rest):
    ctx.state_patch["active_search"] = {"fp": "leak", "pool": ["el-01"], "cursor": 0, "page": 0}
    return True


@pytest.mark.parametrize("executor", [_refuse, _true_silent], ids=["false", "true-no-reply"])
async def test_a_refusing_executor_falls_back_without_a_trace_of_the_kernel(
    monkeypatch, electronics, executor
):
    run = await _served(monkeypatch, electronics, executor=executor)
    ctx = run.ctx
    assert run.branch_result is False and ctx.kernel_turn is None and ctx.kernel_view is None
    assert ctx.trace["kernel_fallback"]["reason"] == "executor_refused"
    assert "kernel" not in ctx.trace
    diff = sorted(k for k in run.before_branch if run.before_branch[k] != run.after_branch[k])
    assert diff == []


async def test_a_false_verdict_with_a_reply_is_served(monkeypatch, electronics):
    """§4: „Dacă a setat totuși un răspuns, turul e servit.\" """

    async def partial(ctx, deps, planned, *rest):
        ctx.set_reply("ok", cacheable=False)
        return False

    run = await _served(monkeypatch, electronics, executor=partial)
    assert run.branch_result is True and isinstance(run.ctx.kernel_turn, _kc().KernelTurn)


async def test_a_failure_after_the_view_restores_everything(monkeypatch, electronics):
    """Vederea se scrie înaintea executorilor și intră în instantaneu: traceul picat ⇒ fallback
    `exception`, iar contextul predat căii v1 e cel de dinaintea ramurii (inclusiv
    `constraints`/`search_constraints` și `kernel_view`)."""
    original = it._chain_record

    def broken(ctx, chain, *, served=False):
        assert ctx.kernel_view is not None and ctx.state.search_constraints, "vederea e scrisă"
        raise RuntimeError("trace")

    monkeypatch.setattr(it, "_chain_record", broken)
    run = await _served(monkeypatch, electronics)
    monkeypatch.setattr(it, "_chain_record", original)
    ctx = run.ctx
    assert run.branch_result is False and ctx.kernel_turn is None and ctx.kernel_view is None
    assert ctx.trace["kernel_fallback"]["reason"] == "exception"
    diff = sorted(k for k in run.before_branch if run.before_branch[k] != run.after_branch[k])
    assert diff == []


async def test_the_view_copies_only_the_needs_and_subject_fields(monkeypatch, electronics):
    """§3: `constraints`/`search_constraints` din proiecția stării PORȚII; obiectul `ctx.state` nu
    se înlocuiește (scrierile de dinainte, aici setul curățat de siguranță, rămân);
    `active_needs` și `kernel_view` citesc starea porții."""
    kept = [{"product_id": "el-09", "name": "n", "price": 1.0}]
    captured = {}

    async def execute(ctx, deps, planned, *rest):
        captured["state_obj"] = ctx.state
        ctx.set_reply("ok", cacheable=False)
        return True

    turn = JOURNEYS[K01[0]].turns[0]
    monkeypatch.setattr(it, "execute_plans", execute)
    ctx = sh.build_ctx(electronics, ConversationStateV2(), turn.user_input)
    ctx.state.displayed_products = []
    ctx.state_patch["displayed_products"] = kept
    ctx.state.asked_intents = ["x"]
    run = await sh.run_turn(
        monkeypatch, electronics, ctx, sh.StageLLM(turn.expect["interpretation"])
    )
    assert run.branch_result is True
    gate = ctx.kernel_view.gate_state
    view = ConversationState.from_jsonb(project_v1(gate))
    assert ctx.state is captured["state_obj"]
    assert ctx.state.search_constraints == view.search_constraints
    assert ctx.state.constraints == view.constraints
    assert "price_max" in json.dumps(ctx.state.search_constraints) or ctx.state.search_constraints
    assert ctx.state.asked_intents == ["x"] and ctx.state_patch["displayed_products"] == kept
    assert active_needs(ctx) == gate.active_needs() != ()
    assert ctx.state_v2 is run.ctx.state_v2 and ctx.state_v2.active_needs() == ()
    assert ctx.kernel_view.subject_is_new is True


def test_subject_is_new_compares_the_gate_subject_with_the_one_before():
    from src.conversation.state_v2 import Topic

    empty = ConversationStateV2()
    phones = ConversationStateV2(topic=Topic(category_key="telefoane", product_type=None))
    cases = phones.topic
    assert it._subject_is_new(empty, empty, "continue") is True
    assert it._subject_is_new(empty, phones, "continue") is True
    assert it._subject_is_new(phones, phones, "continue") is False
    typed = dataclasses.replace(phones, topic=dataclasses.replace(cases, product_type="husa"))
    assert it._subject_is_new(phones, typed, "continue") is True
    # un subiect RELUAT nu e nou (recenzia, constatarea 2)
    assert it._subject_is_new(phones, typed, "resume") is False


@pytest.mark.parametrize("index,expected", [(0, True), (2, False)])
async def test_drop_dead_reads_the_subject_flag_from_the_view_inside_the_executor(
    monkeypatch, index, expected
):
    """Compunerea rulează ÎN executor: `_drop_dead_moves` vede flagul din vederea turului (primul
    tur al subiectului: da; reluarea telefoanelor: nu). Înlocuiește testul cu `kernel_view` pus de
    mână, care nu vedea că vederea se scria DUPĂ executor (recenzia, constatarea 2)."""
    from src.agent import finalize
    from src.conversation import chip_moves

    journey = JOURNEYS["k01-electronics-park-and-resume"]
    cat = sh.catalog(journey.pack)
    sh.install(monkeypatch, cat)
    item = list(sh.chain(journey, cat))[index]
    seen = []
    monkeypatch.setattr(get_settings(), "chip_drop_dead_enabled", True)
    monkeypatch.setattr(
        chip_moves, "drop_dead", lambda c, **kw: seen.append(kw["first_subject_turn"]) or (c, 0)
    )

    async def execute(ctx, deps, planned, *rest):
        finalize._drop_dead_moves(ctx, [], n_cards=2)
        ctx.set_reply("ok", cacheable=False)
        return True

    monkeypatch.setattr(it, "execute_plans", execute)
    ctx = sh.build_ctx(
        cat, item.state_before, item.turn.user_input, item.previous, turn_id=f"t{index}"
    )
    run = await sh.run_turn(monkeypatch, cat, ctx, sh.StageLLM(item.turn.expect["interpretation"]))
    assert run.branch_result is True and seen == [expected]


def test_the_view_writes_live_only_in_the_two_declared_sites():
    """Allowlistul porții I3 potrivește pe câmp și formă, nu pe funcție; regula „toate atribuirile
    vederii într-un singur loc" e fixată aici, mecanic."""
    from scripts import state_writers as sw
    from tests.kernel import gates

    rel = "src/agent/interpreted_turn.py"
    source = (gates.ROOT / rel).read_text(encoding="utf-8")
    writers = sw.scan_source(source, rel, sw.load_fields()).writers
    v1 = {(w.function, w.raw_key) for w in writers if w.path == "v1"}
    assert v1 == {
        ("_apply_turn_view", "constraints"),
        ("_apply_turn_view", "search_constraints"),
        ("_restore_state_fields", "constraints"),
        ("_restore_state_fields", "search_constraints"),
    }
    assert set(it.VIEW_FIELDS) == {"constraints", "search_constraints"}


# --- 5. I3 la rulare + întrebarea răspunsă --------------------------------------------------------


async def test_i3_no_legacy_writer_runs_on_a_served_turn(monkeypatch, electronics):
    ran: list[str] = []
    for name in ("merge_constraints", "_v2_constraints", "_learn_constraints", "_learn_subject"):
        original = getattr(agent_mod, name)

        def spy(*a, _n=name, _o=original, **k):
            ran.append(_n)
            return _o(*a, **k)

        monkeypatch.setattr(agent_mod, name, spy)
    run = await _served(monkeypatch, electronics)
    assert run.branch_result is True
    assert ran == []
    assert run.ctx.state_proposals == run.before_branch["state_proposals"]


async def test_clarify_resume_proposals_do_not_enter_and_the_answered_question_closes(
    monkeypatch, electronics
):
    """Un tur care răspunde unei întrebări vii: `clarify_resume` (rulat înaintea agentului, ca în
    pipeline) propune răspunsul BRUT ca nevoie; pe un tur servit de kernel propunerile lui nu intră,
    iar întrebarea se închide prin `resolve_question` al orchestratorului."""
    from src.worker.stages.clarify import clarify_resume_stage

    state = ConversationStateV2(revision=3, pending_clarification=_pending("storage", 3))
    turn = JOURNEYS[K01[0]].turns[0]
    monkeypatch.setattr(it, "execute_plans", sh.synthetic_executor(electronics, turn.shown))
    ctx = sh.build_ctx(electronics, state, turn.user_input)
    await clarify_resume_stage(ctx, sh.PipelineDeps(db=sh.RecordingDb(), llm=sh.StageLLM()))
    raw = [p for p in ctx.state_proposals if p.op == "set_need"]
    assert raw and raw[0].value == turn.user_input, "clarify_resume a propus răspunsul brut"
    run = await sh.run_turn(
        monkeypatch, electronics, ctx, sh.StageLLM(turn.expect["interpretation"])
    )
    assert run.branch_result is True
    committed = sh.commit(electronics, ctx, state)
    assert committed.pending_clarification is None
    assert committed.asked("storage") is not None
    assert turn.user_input not in json.dumps(_doc(committed), ensure_ascii=False)
    assert ctx.kernel_turn.delta.proposals[0].op == "resolve_question"


async def test_an_aside_leaves_the_question_alive(monkeypatch, electronics):
    state = ConversationStateV2(revision=3, pending_clarification=_pending("storage", 3))
    run = await _served(monkeypatch, electronics, ("c08-aside-keeps-state", 0), state=state)
    assert run.branch_result is True
    committed = sh.commit(electronics, run.ctx, state)
    assert committed.pending_clarification == state.pending_clarification
    assert committed.revision == state.revision


# --- 6. harnessul la nivel de procesor: journey-urile × 5 pachete --------------------------------


def _kernel_step(journey, cat, state, index):
    turn = journey.turns[index]
    previous = tuple(t.user_input for t in journey.turns[:index])
    return fixture_catalog.kernel_step(
        journey.pack,
        state,
        turn.expect["interpretation"],
        turn.user_input,
        earlier=previous[::-1],
        shown_ids=turn.shown,
        turn_id=f"t{index}",
        locale=journey.locale,
        loaded=cat.pack,
        vocab=cat.vocab,
        catalog=cat.facts,
        answer_pending=True,
    )


@pytest.mark.parametrize("pack", sh.PACKS)
async def test_the_committed_state_after_each_turn_equals_kernel_step(monkeypatch, pack):
    """Turele servite de un executor sintetic, cu commit-ul REAL între ele (`_build_new_state` →
    `kernel_commit`), pe starea hidratată din documentul scris, ca în producție. După FIECARE tur
    servit starea == `kernel_step` pe aceeași stare de intrare; pe journey-urile servite în
    întregime starea finală == lanțul pur (`stage_harness.chain`). Turele oprite înainte de ramură
    (scurtătură exactă, paginare pură) sunt ale căii v1: acolo harnessul continuă din starea
    lanțului pur (declarat)."""
    cat = sh.catalog(pack)
    sh.install(monkeypatch, cat)
    journeys = [j for j in JOURNEYS.values() if j.pack == pack]
    served = full = 0
    for journey in journeys:
        state = ConversationStateV2()
        whole = True
        for index, turn in enumerate(journey.turns):
            where = f"{journey.journey_id}#{index}"
            step = _kernel_step(journey, cat, state, index)
            monkeypatch.setattr(it, "execute_plans", sh.synthetic_executor(cat, turn.shown))
            previous = tuple(t.user_input for t in journey.turns[:index])
            ctx = sh.build_ctx(
                cat, state, turn.user_input, previous, turn_id=f"t{index}", locale=journey.locale
            )
            run = await sh.run_turn(
                monkeypatch, cat, ctx, sh.StageLLM(turn.expect["interpretation"])
            )
            if not run.branch_result:
                assert not run.reached or "kernel_fallback" in ctx.trace, where
                whole = False
                state = step.state_after
                continue
            served += 1
            assert ctx.kernel_turn is not None, where
            committed = sh.commit(cat, ctx, state)
            assert _doc(committed) == _doc(step.state_after), where
            state = committed
        if whole:
            full += 1
            final = list(sh.chain(journey, cat))[-1].step.state_after
            assert _doc(state) == _doc(final), journey.journey_id
    assert served and full, (pack, served, full)
