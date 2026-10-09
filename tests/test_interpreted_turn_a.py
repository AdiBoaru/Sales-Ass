"""NX-336 PR A — pasul 6 al kernelului: schela și traceul dark.

Ce dovedește suita (cardul, „Implementation Steps → PR A", regresiile întâi):

- poarta de boot: flagul fără stările v2, fără scurtăturile v2, fără gardul de rafinare, sau cu
  creierul unic, e refuzat;
- `try_pre_intents(exact_only=True)`: fiecare ramură GHICITOARE întoarce `False` fără efecte, iar
  ramurile exacte servesc ca azi;
- ramura din `agent_stage`: nu rulează pe acțiuni, chip-uri recunoscute, paginare pură sau cu
  flagul stins (iar stins, modulul nici nu se importă);
- orchestratorul: tot lanțul, traceul `kernel.v1.2` redactat și plafonat, evenimentele, și MEREU
  `False` (calea v1 răspunde); pe fiecare motiv de fallback turul v1 e identic cu flagul OFF, iar
  contextul predat căii v1 e egal cu cel de dinaintea ramurii;
- I13: exact un rând `purpose="interpret"` pe tur care ajunge la ramură (zero pe `internal_error`
  înainte de apel și pe o citire de intrare picată);
- journey-urile × 5 pachete prin stagiul REAL, cu traceul egal pe straturi cu lanțul pur.

Zero model, zero DB: transport fals sub clientul real, stub-uri pe pachet (`tests/kernel/`)."""

from __future__ import annotations

import copy
import dataclasses
import json
import subprocess
import sys
import textwrap
from types import SimpleNamespace as NS

import pytest

from src.agent import deterministic as det
from src.config import Settings, get_settings
from src.conversation.interpretation import (
    KERNEL_CONTRACT_VERSION,
    Act,
    Ambiguity,
    AmbiguityDecision,
    Reference,
    ResolvedRef,
    StateChange,
    TurnInterpretation,
    TurnPlan,
)
from src.conversation.kernel_trace import (
    TRACE_CAP_BYTES,
    KernelTrace,
    cap_trace,
    first_divergence,
    redact_trace,
    render,
    trace_size,
)
from src.conversation.state_v2 import ConversationStateV2, References
from src.models import ProductRef
from src.worker.stages import agent as agent_mod
from tests.kernel import gates, replay
from tests.kernel import stage_harness as sh
from tests.test_nx326_named_shortcut_targets import SHOWN, _ctx, _deps, _RecordLLM

FIND = TurnInterpretation(
    thread="continue",
    acts=[Act(kind="find", targets=[], query="telefon")],
    changes=[],
    references=[],
    ambiguities=[],
    corrects_previous_turn=False,
)


def _events(ctx, name):
    return [e.properties for e in ctx.events if e.type == name]


def _fields(ctx, skip=("events", "trace")):
    return {
        f.name: copy.deepcopy(getattr(ctx, f.name))
        for f in dataclasses.fields(ctx)
        if f.name not in skip
    }


# --- 1. poarta de boot ----------------------------------------------------------------------------

_PROFILE = {
    "CONVERSATION_STATE_V2_ENABLED": "true",
    "CONVERSATION_STATE_V2_WRITE_ENABLED": "true",
    "REFERENCE_RESOLVER_V2_SHORTCUTS_ENABLED": "true",
    "NAMED_SHORTCUT_TARGETS_ENABLED": "true",
    "REFINEMENT_GUARD_ENABLED": "true",
}


def _boot(monkeypatch, env):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return Settings()


def test_the_flag_is_off_by_default():
    assert Settings().interpreted_turn_enabled is False


def test_boot_accepts_the_flag_with_its_profile(monkeypatch):
    s = _boot(monkeypatch, {**_PROFILE, "INTERPRETED_TURN_ENABLED": "true"})
    assert s.interpreted_turn_enabled is True


@pytest.mark.parametrize("missing", sorted(_PROFILE))
def test_boot_refuses_the_flag_without_a_required_setting(monkeypatch, missing):
    env = {**_PROFILE, "INTERPRETED_TURN_ENABLED": "true", missing: "false"}
    if missing == "CONVERSATION_STATE_V2_ENABLED":
        # scrierea v2 fără v2 are poarta ei (NX-235); aici se verifică doar poarta flagului
        env["CONVERSATION_STATE_V2_WRITE_ENABLED"] = "false"
    with pytest.raises(Exception, match="INTERPRETED_TURN_ENABLED"):
        _boot(monkeypatch, env)


def test_boot_refuses_the_flag_with_the_single_brain(monkeypatch):
    env = {**_PROFILE, "INTERPRETED_TURN_ENABLED": "true", "SINGLE_BRAIN_ENABLED": "true"}
    with pytest.raises(Exception, match="INTERPRETED_TURN_ENABLED"):
        _boot(monkeypatch, env)


def test_the_env_example_declares_the_flag_off():
    text = (gates.ROOT / ".env.example").read_text(encoding="utf-8")
    assert "INTERPRETED_TURN_ENABLED=false" in text


# --- 2. `try_pre_intents(exact_only=True)`: ramurile ghicitoare întorc False fără efecte ----------


@pytest.fixture
def shortcuts(monkeypatch):
    """Scurtăturile cu resolverul v2 aprins, pe ecranul NX-326; `state` numără citirile."""
    settings = get_settings()
    monkeypatch.setattr(settings, "named_shortcut_targets_enabled", True)
    monkeypatch.setattr(settings, "reference_resolver_v2_shortcuts_enabled", True)
    state = {"fail": False, "lookups": [], "by_ids": [], "compared": [], "reviews": []}
    by_id = {r.product_id: r for r in SHOWN}

    async def fetch(deps, business_id, lookup, **kw):
        state["lookups"].append(lookup)
        if state["fail"]:
            raise ConnectionError("pooler plin")
        from src.conversation.references import ProductFacts, ReferenceFacts

        return ReferenceFacts(
            products={
                pid: ProductFacts(pid, by_id[pid].name, by_id[pid].price, True)
                for pid in lookup.ids
                if pid in by_id
            }
        )

    async def vocabulary(deps, business_id):
        from src.catalog.vocabulary import CatalogVocabulary

        return CatalogVocabulary(business_id=business_id, dimensions={})

    async def by_ids(conn, business_id, ids, **k):
        state["by_ids"].append(list(ids))
        return [
            {"id": p, "name": by_id[p].name, "price": by_id[p].price, "url": f"u/{p}"}
            for p in ids
            if p in by_id
        ]

    async def serve(ctx, deps, ids):
        state["compared"].append(list(ids))
        ctx.set_reply("tabel", cacheable=False)
        return True

    async def reviews(ctx, deps, product_id, name=""):
        state["reviews"].append(product_id)
        ctx.set_reply("recenzii", cacheable=False)

    monkeypatch.setattr(det, "fetch_reference_facts", fetch)
    monkeypatch.setattr(det, "get_vocabulary", vocabulary)
    monkeypatch.setattr(det, "get_products_by_ids", by_ids)
    monkeypatch.setattr(det, "serve_comparison", serve)
    monkeypatch.setattr(det, "serve_reviews", reviews)

    def v2(on: bool) -> None:
        monkeypatch.setattr(settings, "reference_resolver_v2_shortcuts_enabled", on)

    state["v2"] = v2
    return state


async def _exact(ctx):
    before = _fields(ctx)
    served = await det.try_pre_intents(ctx, _deps(), exact_only=True)
    return served, before


def _untouched(ctx, before):
    after = _fields(ctx)
    diff = sorted(k for k in before if before[k] != after[k])
    assert diff == [], f"ramura ghicitoare a scris în context: {diff}"


@pytest.mark.parametrize(
    ("text", "v2", "why"),
    [
        ("Trimite-mi linkul, te rog", True, "fallback: toate ancorele"),
        ("Trimite-mi linkul la Cerave", True, "model: nume negăsit"),
        ("Trimite-mi linkul la Yuja Niacin", False, "fără v2: `_link_targets` ghicește"),
        ("compară-le", True, "fallback: primele două"),
        ("Compară Wishtrend Vitamin cu Yuja Niacin", False, "fără v2: calea NX-326"),
        ("compară Yuja Niacin cu celelalte", True, "model: o singură țintă"),
    ],
)
async def test_exact_only_a_guessing_shortcut_returns_false_without_effects(
    shortcuts, text, v2, why
):
    shortcuts["v2"](v2)
    ctx = _ctx(text)
    served, before = await _exact(ctx)
    assert served is False, why
    _untouched(ctx, before)
    assert shortcuts["by_ids"] == [] and shortcuts["compared"] == []


async def test_exact_only_unreadable_facts_do_not_fall_on_the_nx326_path(shortcuts):
    """`_v2_shortcut` întoarce None (fapte necitibile): v1 cade pe `_link_targets`, care
    ghicește."""
    shortcuts["fail"] = True
    ctx = _ctx("Trimite-mi linkul la Yuja Niacin")
    served, before = await _exact(ctx)
    assert served is False
    _untouched(ctx, before)
    assert shortcuts["by_ids"] == []


async def test_exact_only_serves_an_exact_link_as_today(shortcuts):
    ctx = _ctx("Trimite-mi linkul la Yuja Niacin")
    assert await det.try_pre_intents(ctx, _deps(), exact_only=True) is True
    assert shortcuts["by_ids"] == [["p3"]]


async def test_exact_only_serves_an_exact_comparison_as_today(shortcuts):
    ctx = _ctx("compară primul cu al treilea")
    assert await det.try_pre_intents(ctx, _deps(), exact_only=True) is True
    assert shortcuts["compared"] == [["p1", "p3"]]


async def test_exact_only_false_is_todays_function(shortcuts):
    """`exact_only=False` (implicitul) = ramurile de azi, inclusiv cele ghicitoare."""
    ctx = _ctx("compară-le")
    assert await det.try_pre_intents(ctx, _deps()) is True
    assert shortcuts["compared"] == [["p1", "p2"]]


@pytest.mark.parametrize(
    "text", ["ce părere au clienții despre el?", "spune-mi mai multe despre el"]
)
async def test_exact_only_an_ambiguous_anchor_does_not_ask(shortcuts, text):
    """Recenzii / detaliu cu cinci carduri și nicio ancoră: v1 întreabă (`set_clarify`), iar
    întrebarea e a porții kernelului, nu a scurtăturii."""
    ctx = _ctx(text)
    served, before = await _exact(ctx)
    assert served is False
    _untouched(ctx, before)
    assert ctx.reply is None


async def test_exact_only_a_resolved_anchor_serves_the_reviews(shortcuts):
    ctx = _ctx("ce recenzii are?", shown=SHOWN[:1])
    assert await det.try_pre_intents(ctx, _deps(), exact_only=True) is True
    assert shortcuts["reviews"] == ["p1"]


async def test_exact_only_a_detail_with_new_constraints_goes_to_the_kernel(shortcuts):
    """Ancoră unică, dar mesajul cere ceva în plus («sub 3000 lei»): nu e o scurtătură exactă."""
    from tests.kernel import fixture_catalog

    ctx = _ctx("spune-mi mai multe despre el, dar sub 3000 lei", shown=SHOWN[:1])
    ctx.business.domain_pack = fixture_catalog.pack("electronics")
    served, before = await _exact(ctx)
    assert served is False
    _untouched(ctx, before)


async def test_exact_only_leaves_no_selection_proposal_on_an_unhydratable_anchor(
    shortcuts, monkeypatch
):
    """Precedența v2 rezolvă „el" pe `selected_product` (în afara ecranului): v1 propune selecția
    și întreabă. Sub `exact_only` nu rămâne nicio propunere în context."""
    monkeypatch.setattr(get_settings(), "reference_precedence_v2_enabled", True)
    ctx = _ctx("spune-mi mai multe despre el")
    ctx.state_v2 = ConversationStateV2(references=References(selected_product="p9"))
    served, before = await _exact(ctx)
    assert served is False
    _untouched(ctx, before)
    assert ctx.state_proposals == []


async def test_exact_only_keeps_the_server_chip_compare_with_similar(shortcuts, monkeypatch):
    """«Compară-l cu un produs similar» e chip-ul NOSTRU sub un detaliu (textul re-randat, o
    singură ancoră): o mutare a serverului, nu text de interpretat (abatere declarată: cardul
    îl credea prins de `chip_move`)."""
    settings = get_settings()
    monkeypatch.setattr(settings, "compare_with_similar_enabled", True)
    monkeypatch.setattr(settings, "compare_intent_enabled", True)
    served_with: list[str] = []

    async def similar(ctx, deps, anchor_id):
        served_with.append(anchor_id)
        ctx.set_reply("tabel", cacheable=False)
        return True

    monkeypatch.setattr(det, "serve_compare_with_similar", similar)
    ctx = _ctx("Compară-l cu un produs similar", shown=SHOWN[:1])
    assert await det.try_pre_intents(ctx, _deps(), exact_only=True) is True
    assert served_with == ["p1"]


def test_pure_pagination_is_todays_show_more_predicate(monkeypatch):
    ctx = _ctx("mai arată-mi")
    ctx.state.active_search = {"fp": "x", "pool": ["p1"], "cursor": 0}
    assert det.is_pure_pagination(ctx) is True
    refined = _ctx("mai arată-mi, dar sub 100 lei")
    refined.state.active_search = {"fp": "x", "pool": ["p1"], "cursor": 0}
    assert det.is_pure_pagination(refined) is False


# --- 3. ramura în `agent_stage` ------------------------------------------------------------------


@pytest.fixture
def electronics(monkeypatch):
    cat = sh.catalog("electronics")
    sh.install(monkeypatch, cat)
    return cat


@pytest.fixture
def branch(monkeypatch):
    """Spionul ramurii: câte ture au ajuns la orchestrator (întoarce `False`, ca în PR A)."""
    from src.agent import interpreted_turn as it

    calls: list[str] = []

    async def spy(ctx, deps):
        calls.append(ctx.turn_id)
        return False

    monkeypatch.setattr(it, "run_interpreted_turn", spy)
    return calls


async def test_the_branch_runs_on_an_ordinary_turn(electronics, branch):
    ctx = sh.build_ctx(electronics, ConversationStateV2(), "Vreau un telefon.")
    await agent_mod.agent_stage(ctx, _deps(sh.StageLLM(FIND)))
    assert branch == ["t0"]


async def test_the_branch_does_not_run_with_the_flag_off(electronics, branch, monkeypatch):
    monkeypatch.setattr(get_settings(), "interpreted_turn_enabled", False)
    ctx = sh.build_ctx(electronics, ConversationStateV2(), "Vreau un telefon.")
    await agent_mod.agent_stage(ctx, _deps(sh.StageLLM(FIND)))
    assert branch == []


async def test_the_branch_needs_a_hydrated_v2_state(electronics, branch):
    ctx = sh.build_ctx(electronics, ConversationStateV2(), "Vreau un telefon.")
    ctx.state_v2 = None
    await agent_mod.agent_stage(ctx, _deps(sh.StageLLM(FIND)))
    assert branch == []


async def test_an_action_turn_stays_on_v1(electronics, branch, monkeypatch):
    monkeypatch.setattr(agent_mod, "action_command", lambda ctx: object())
    ctx = sh.build_ctx(electronics, ConversationStateV2(), "Vreau un telefon.")
    await agent_mod.agent_stage(ctx, _deps(sh.StageLLM(FIND)))
    assert branch == []


async def test_a_recognized_chip_stays_on_v1(electronics, branch, monkeypatch):
    """Recunoașterea REALĂ (`_recognize_chip_press`, cu poarta de flag), doar funcția pură
    `chip_press.recognize` e stub. Implicitul de azi (flag stins ⇒ chip-ul ajunge la ramură) e fixat
    în `tests/test_interpreted_turn_a_review.py`, ca blocant al PR-ului C."""
    from src.conversation import chip_press

    monkeypatch.setattr(
        chip_press, "recognize", lambda *a, **k: NS(kind="reviews", move_id="m", slot_map=dict)
    )
    monkeypatch.setattr(get_settings(), "chip_moves_v2_enabled", True)
    monkeypatch.setattr(det, "serve_chip_move", _no_serve)
    ctx = sh.build_ctx(electronics, _paging_state(electronics), "Vreau un telefon.")
    await agent_mod.agent_stage(ctx, _deps(sh.StageLLM(FIND)))
    assert ctx.chip_move is not None and branch == []


async def _no_serve(ctx, deps, move):
    return False


def _paging_state(cat) -> ConversationStateV2:
    ids = list(cat.items)[:3]
    from src.conversation.state_v2 import DisplayedRef

    shown = tuple(DisplayedRef(p, cat.items[p]["name"], float(cat.items[p]["price"])) for p in ids)
    return ConversationStateV2(
        references=References(displayed_products=shown),
        active_search={"fp": "x", "pool": ids, "cursor": 3, "page": 1},
    )


async def test_pure_pagination_stays_on_v1(electronics, branch):
    ctx = sh.build_ctx(electronics, _paging_state(electronics), "mai arată-mi")
    await agent_mod.agent_stage(ctx, _deps(sh.StageLLM(FIND)))
    assert branch == []


async def test_a_pagination_with_residue_goes_to_the_kernel(electronics, branch):
    ctx = sh.build_ctx(electronics, _paging_state(electronics), "mai arată-mi, dar sub 100 lei")
    await agent_mod.agent_stage(ctx, _deps(sh.StageLLM(FIND)))
    assert branch == ["t0"]


async def test_after_a_false_kernel_the_full_shortcuts_run_as_today(electronics, branch):
    """«compară-le» cu patru carduri: scurtătura exactă nu servește (fără țintă numită), kernelul
    întoarce `False`, iar `try_pre_intents` complet dă comparația de azi (primele două)."""
    state = _paging_state(electronics)
    compared: list[list[str]] = []

    async def serve(ctx, deps, ids):
        compared.append(list(ids))
        ctx.set_reply("tabel", cacheable=False)
        return True

    det_serve = det.serve_comparison
    det.serve_comparison = serve
    try:
        ctx = sh.build_ctx(electronics, state, "compară-le")
        await agent_mod.agent_stage(ctx, _deps(sh.StageLLM(FIND)))
    finally:
        det.serve_comparison = det_serve
    assert branch == ["t0"]
    assert compared == [[d.product_id for d in state.references.displayed_products][:2]]


def test_flag_off_does_not_import_the_orchestrator():
    code = textwrap.dedent(
        """
        import sys
        import src.worker.stages.agent  # noqa: F401
        print("src.agent.interpreted_turn" in sys.modules)
        """
    )
    out = subprocess.run(
        [sys.executable, "-c", code], cwd=gates.ROOT, capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "False"


# --- 4. orchestratorul: traceul dark, evenimentele, I13 -------------------------------------------


async def _one(monkeypatch, cat, text="Vreau un telefon.", interpretation=FIND, **kw):
    llm = sh.StageLLM(interpretation)
    ctx = sh.build_ctx(cat, kw.pop("state", ConversationStateV2()), text)
    return await sh.run_turn(monkeypatch, cat, ctx, llm, **kw)


async def test_the_dark_turn_writes_the_trace_and_returns_false(monkeypatch, electronics):
    run = await _one(monkeypatch, electronics)
    assert run.reached and run.branch_result is False
    assert run.llm.loops, "calea v1 a răspuns"
    trace = KernelTrace.model_validate(run.ctx.trace["kernel"])
    assert trace.contract_version == KERNEL_CONTRACT_VERSION == "kernel.v10.0"
    assert trace.executor == "none" and trace.plan.executor == "search"
    assert trace.plans == [trace.plan] and trace.truncated is False
    assert "kernel_fallback" not in run.ctx.trace
    [turn] = _events(run.ctx, "kernel_turn")
    assert turn == {
        "served": False,
        "executor": "search",
        "plans": 1,
        "fallback_reason": "dark",
        "turn_id": "t0",
    }
    [interp] = _events(run.ctx, "turn_interpretation")
    assert interp["contract_version"] == KERNEL_CONTRACT_VERSION and interp["outcome"] == "ok"
    assert _events(run.ctx, "ambiguity_decision")
    assert _events(run.ctx, "kernel_delta")


async def test_i13_exactly_one_interpret_call_on_a_turn_that_reaches_the_branch(
    monkeypatch, electronics
):
    run = await _one(monkeypatch, electronics)
    assert len(run.interpret_rows) == 1
    assert len(run.llm.transport.calls) == 1


@pytest.mark.parametrize(
    "fault", ["refused", "truncated", "invalid_json", "schema_violation", "provider_error"]
)
async def test_a_provider_outcome_falls_back_with_one_call(monkeypatch, electronics, fault):
    llm = sh.StageLLM(FIND)
    llm.transport.fault = fault
    ctx = sh.build_ctx(electronics, ConversationStateV2(), "Vreau un telefon.")
    run = await sh.run_turn(monkeypatch, electronics, ctx, llm)
    assert run.branch_result is False and llm.loops
    assert run.ctx.trace["kernel_fallback"]["reason"] == fault
    assert "kernel" not in run.ctx.trace
    assert len(run.interpret_rows) == 1
    [turn] = _events(run.ctx, "kernel_turn")
    assert (turn["served"], turn["fallback_reason"]) == (False, fault)


async def test_internal_error_before_the_call_has_zero_interpret_rows(monkeypatch, electronics):
    monkeypatch.setattr(get_settings(), "llm_reasoning_effort_interpret", "bogus")
    run = await _one(monkeypatch, electronics)
    assert run.ctx.trace["kernel_fallback"]["reason"] == "internal_error"
    assert run.interpret_rows == [] and run.llm.loops


async def test_an_input_read_that_fails_falls_back_without_a_call(monkeypatch, electronics):
    db = sh.RecordingDb()
    db.fail.add("kernel_category_menu")
    run = await _one(monkeypatch, electronics, db=db)
    assert run.ctx.trace["kernel_fallback"]["reason"] == "exception"
    assert run.interpret_rows == [] and run.llm.loops


async def test_a_facts_read_that_fails_after_the_call_falls_back(monkeypatch, electronics):
    db = sh.RecordingDb()
    db.fail.add("kernel_reference_facts")
    run = await _one(monkeypatch, electronics, db=db)
    assert run.ctx.trace["kernel_fallback"]["reason"] == "exception"
    assert len(run.interpret_rows) == 1 and run.llm.loops


async def test_the_fallback_record_carries_no_text(monkeypatch, electronics):
    llm = sh.StageLLM(FIND)
    llm.transport.fault = "refused"
    ctx = sh.build_ctx(electronics, ConversationStateV2(), "Vreau un telefon.")
    run = await sh.run_turn(monkeypatch, electronics, ctx, llm)
    assert set(run.ctx.trace["kernel_fallback"]) == {"reason", "vocabulary_snapshot"}


async def test_a_live_question_is_answered_in_the_gate_state(monkeypatch, electronics):
    """O întrebare vie + un tur care nu e paranteză ⇒ `resolve_question` în propunerile porții,
    deci poarta nu mai vede întrebarea în așteptare (§1)."""
    from src.conversation.state_v2 import PendingClarification

    state = ConversationStateV2(
        revision=3,
        pending_clarification=PendingClarification(
            question_id="q:storage:3:1", target_key="storage", asked_at_revision=3
        ),
    )
    run = await _one(monkeypatch, electronics, state=state)
    trace = KernelTrace.model_validate(run.ctx.trace["kernel"])
    assert trace.proposals[0] == {"op": "resolve_question", "key": "storage"}


async def test_an_aside_does_not_answer_the_question(monkeypatch, electronics):
    from src.conversation.state_v2 import PendingClarification

    aside = FIND.model_copy(
        update={"thread": "aside", "acts": [Act(kind="store_info", targets=[], query="livrare")]}
    )
    state = ConversationStateV2(
        revision=3,
        pending_clarification=PendingClarification(
            question_id="q:storage:3:1", target_key="storage", asked_at_revision=3
        ),
    )
    run = await _one(monkeypatch, electronics, "Cât durează livrarea?", aside, state=state)
    trace = KernelTrace.model_validate(run.ctx.trace["kernel"])
    assert all(p["op"] != "resolve_question" for p in trace.proposals)


async def test_the_stored_dark_trace_is_redacted(monkeypatch, electronics):
    mail = "ana.pop@example.com"
    interp = FIND.model_copy(
        update={"acts": [Act(kind="find", targets=[], query=f"telefon pentru {mail}")]}
    )
    run = await _one(monkeypatch, electronics, f"Vreau un telefon pentru {mail}", interp)
    stored = json.dumps(run.ctx.trace["kernel"], ensure_ascii=False)
    assert mail not in stored and "[email]" in stored


@pytest.mark.parametrize("pending", [False, True])
async def test_show_more_without_changes_pages_and_a_closed_question_is_not_a_change(
    monkeypatch, electronics, pending
):
    """`changed` = propunerile de NEVOI ale deltei (§2). «mai arată-mi, te rog frumos» cu reziduu
    ajunge la ramură; fără schimbări și cu sesiune, plannerul paginează. Închiderea unei întrebări
    vii (`resolve_question`) nu e o schimbare, deci nu transformă paginarea într-o căutare."""
    from src.conversation.state_v2 import PendingClarification

    state = _paging_state(electronics)
    if pending:
        state = dataclasses.replace(
            state,
            revision=2,
            pending_clarification=PendingClarification(
                question_id="q:storage:2:1", target_key="storage", asked_at_revision=2
            ),
        )
    more = FIND.model_copy(update={"acts": [Act(kind="show_more", targets=[], query=None)]})
    run = await _one(
        monkeypatch, electronics, "mai arată-mi, am mai multe întrebări", more, state=state
    )
    assert run.reached
    trace = KernelTrace.model_validate(run.ctx.trace["kernel"])
    assert trace.plan.executor == "page"


# --- 5. fallback = OFF pe suprafața I16; contextul predat căii v1 e neatins -----------------------

FAULTS = (
    "dark",
    "refused",
    "truncated",
    "invalid_json",
    "schema_violation",
    "provider_error",
    "internal_error",
    "input_exception",
    "facts_exception",
    "vocabulary_unavailable",
)


def _surface(run: sh.StageRun) -> dict:
    from src.agent.interpreted_turn import KERNEL_READS
    from src.worker.processor import _turn_proposals

    ctx = run.ctx
    return {
        "reply": dataclasses.asdict(ctx.reply) if ctx.reply is not None else None,
        "tool_loops": list(run.llm.loops),
        "state": ctx.state,
        "state_patch": ctx.state_patch,
        "proposals": [repr(p) for p in _turn_proposals(ctx, is_rich=False, has_products=False)],
        "db": [op for op in run.db.ops if op not in KERNEL_READS],
        # recenzia NX-336 (constatarea 2): și evenimentele, în afara celor ale kernelului
        "events": [
            (e.type, json.dumps(e.properties, sort_keys=True, default=str))
            for e in ctx.events
            if e.type not in _KERNEL_EVENTS
        ],
    }


_KERNEL_EVENTS = frozenset(
    {"turn_interpretation", "ambiguity_decision", "answer_policy", "kernel_delta", "kernel_turn"}
)


async def _flagged_run(monkeypatch, cat, fault, flag_on):
    monkeypatch.setattr(get_settings(), "interpreted_turn_enabled", flag_on)
    llm = sh.StageLLM(FIND)
    db = sh.RecordingDb()
    if fault in {"refused", "truncated", "invalid_json", "schema_violation", "provider_error"}:
        llm.transport.fault = fault
    elif fault == "internal_error":
        monkeypatch.setattr(get_settings(), "llm_reasoning_effort_interpret", "bogus")
    elif fault == "input_exception":
        db.fail.add("kernel_category_menu")
    elif fault == "vocabulary_unavailable":
        db.fail.add("kernel_load_vocabulary")
    elif fault == "facts_exception":
        db.fail.add("kernel_reference_facts")
    ctx = sh.build_ctx(cat, ConversationStateV2(), "Vreau un telefon.")
    return await sh.run_turn(monkeypatch, cat, ctx, llm, db=db)


@pytest.mark.parametrize("fault", FAULTS)
async def test_every_fallback_is_the_flag_off_turn_on_the_i16_surface(
    monkeypatch, electronics, fault
):
    on = await _flagged_run(monkeypatch, electronics, fault, True)
    off = await _flagged_run(monkeypatch, electronics, fault, False)
    assert on.reached and not off.reached
    assert _surface(on) == _surface(off)


@pytest.mark.parametrize("fault", FAULTS)
async def test_the_context_handed_to_v1_equals_the_one_before_the_branch(
    monkeypatch, electronics, fault
):
    run = await _flagged_run(monkeypatch, electronics, fault, True)
    assert run.before_branch is not None and run.after_branch is not None
    diff = sorted(k for k in run.before_branch if run.before_branch[k] != run.after_branch[k])
    assert diff == []


def test_the_snapshot_restores_every_field_an_executor_can_write(electronics):
    """Un executor (PR C/D) scrie în context înainte de verdictul „servit"; pe fallback
    instantaneul aduce ÎNAPOI tot, iar testul compară TOATE câmpurile, nu lista declarată."""
    from src.agent.interpreted_turn import EXECUTOR_WRITABLE, ContextSnapshot
    from src.models import RetrievalResult

    ctx = sh.build_ctx(electronics, ConversationStateV2(), "Vreau un telefon.")
    before = _fields(ctx)
    saved = ContextSnapshot.take(ctx)
    ctx.set_reply("x", cacheable=False)
    ctx.retrieval = RetrievalResult(products=[{"id": "el-01"}], source="kernel")
    ctx.state_patch["active_search"] = {"fp": "x"}
    ctx.safety_decision = NS(blocked=("el-01",))
    ctx.routine = NS(slots=())
    ctx.state_proposals.append("proposal")
    ctx.emit("executor_ran")
    restored = saved.restore(ctx)
    assert set(restored) >= {"reply", "retrieval", "state_patch", "safety_decision", "routine"}
    assert _fields(ctx) == before
    assert {"reply", "retrieval", "safety_decision", "routine"} <= set(EXECUTOR_WRITABLE)
    assert _events(ctx, "executor_ran"), "evenimentele sunt în afara suprafeței restaurate"


def test_the_orchestrator_reducer_policy_is_the_processors(electronics):
    from src.agent.interpreted_turn import reducer_policy
    from src.worker.processor import _reducer_policy

    ctx = sh.build_ctx(electronics, ConversationStateV2(), "x")
    assert reducer_policy(electronics.pack) == _reducer_policy(ctx)


# --- 6. traceul v1.2: câmpuri aditive, redactare, plafon ------------------------------------------


def _trace(**kw) -> KernelTrace:
    base = dict(
        contract_version=KERNEL_CONTRACT_VERSION,
        vocabulary_snapshot="fixture:x",
        interpretation=FIND,
        checked_changes=[],
        resolved_refs=[],
        state_before={},
        proposals=[],
        rejected=[],
        state_after={},
        ambiguity=AmbiguityDecision(verdict="act", reason="clear", question=None),
        plan=TurnPlan(executor="reply_only", product_ids=[], search_args=None, depends_on=None),
        executor="none",
        answer_policy=None,
    )
    base.update(kw)
    return KernelTrace(**base)


def test_the_v1_2_fields_are_additive_with_defaults():
    trace = _trace()
    assert (trace.plans, trace.gaps, trace.disclosures, trace.dropped_acts) == ([], [], [], 0)
    assert (trace.delta_counters, trace.gate_memory, trace.truncated) == ({}, None, False)


def _redact(text: str) -> str:
    from src.privacy.boundary import make_safe

    return make_safe(text).text


def test_the_trace_is_redacted_before_it_is_stored():
    mail, phone = "ana.pop@example.com", "0722 123 456"
    interp = TurnInterpretation(
        thread="continue",
        acts=[
            Act(kind="find", targets=["r1"], query=f"scrie-mi la {mail}"),
            Act(kind="detail", targets=["r1"], query=None, question=f"ajunge la {phone}?"),
        ],
        changes=[
            StateChange(
                op="add",
                target=None,
                dimension="unmapped",
                relation="eq",
                value=phone,
                number=None,
                unit=None,
                relative_to=None,
                quote=f"sună la {phone}",
            )
        ],
        references=[
            Reference(
                id="r1",
                text=mail,
                kind="name",
                ordinal=None,
                name=mail,
                dimension=None,
                value=None,
                direction=None,
            )
        ],
        ambiguities=[Ambiguity(about="value", target=None, readings=[phone])],
        corrects_previous_turn=False,
    )
    from src.tools.catalog_tools import SearchArgs

    plan = TurnPlan(
        executor="search",
        product_ids=[],
        search_args=SearchArgs(query=mail, product_name=mail, rank_terms=[phone]),
        depends_on=None,
    )
    raw = _trace(interpretation=interp, plan=plan, plans=[plan])
    stored = json.dumps(redact_trace(raw, _redact).model_dump(mode="json"), ensure_ascii=False)
    assert mail not in stored and phone not in stored
    assert "[email]" in stored and "[telefon]" in stored


def test_a_huge_trace_is_capped_in_the_declared_order_and_still_renders():
    ids = [f"p{i:04d}" for i in range(40)]
    resolved = [
        ResolvedRef(
            ref_id="r1",
            kind="name",
            outcome="ambiguous",
            product_ids=ids,
            source="catalog",
            reason="name_shared",
        )
    ]
    small = _trace(resolved_refs=resolved)
    capped = cap_trace(small, limit=trace_size(small) - 1)
    assert capped.truncated is True
    assert capped.resolved_refs[0].product_ids == ids[:6], "primul pas: id-urile peste 6"
    assert capped.interpretation == small.interpretation, "restul rămâne cât ajunge"

    words = " ".join(["cuvânt"] * 20_000)
    act = Act(kind="find", targets=[], query=words)
    huge = _trace(
        interpretation=FIND.model_copy(update={"acts": [act] * 5}),
        resolved_refs=resolved * 30,
        state_before={"topic": "x", "needs": ["a"] * 500, "shown": ids * 10},
    )
    assert trace_size(huge) > TRACE_CAP_BYTES
    out = cap_trace(huge)
    assert out.truncated is True and trace_size(out) <= TRACE_CAP_BYTES
    assert "EXECUTOR" in render(out)


def test_the_default_cap_is_16_kb():
    assert TRACE_CAP_BYTES == 16 * 1024


# --- 7. registrul, porțile, diferențialul ---------------------------------------------------------


def test_the_orchestrator_role_is_registered_with_its_gates():
    roles = gates.modules_by_role()
    # NX-336 PR B: commit-ul kernelului a trecut din `planned` în rol, în PR-ul care l-a creat.
    # NX-336 PR C: executorii kernelului sunt lipici NOU, deci au porțile orchestratorului, nu ale
    # rolului `executor` (modulele de azi): recenzia PR C, constatarea 7.
    # NX-382: compozitorul unic și faptele lui, chemați de executori.
    assert roles["orchestrator"] == [
        "src/agent/interpreted_turn.py",
        "src/agent/kernel_executors.py",
        "src/worker/kernel_commit.py",
        "src/agent/composer.py",
        "src/agent/detail_answer.py",
    ]
    assert set(gates.GATES_BY_ROLE["orchestrator"]) == {
        "search_args",
        "state_writes",
        "raw_text",
        "raw_readers",
    }
    assert "src/worker/kernel_commit.py" not in gates.load_modules()["planned"]


def test_the_orchestrator_reads_raw_text_only_in_declared_readers():
    declared = {
        e["function"]
        for e in gates.load_raw_readers()
        if e["file"] == "src/agent/interpreted_turn.py"
    }
    # NX-336 PR B (recenzia, constatarea 3): și răspunsul BOTULUI, pentru memoria întrebării.
    assert declared == {"_turn_words", "_redact", "_asked_in_reply"}


@pytest.mark.parametrize(
    "call", ["complete_schema_raw", "run_tool_loop_structured", "moderate", "describe_image"]
)
def test_i13_gate_fails_on_every_model_method(call):
    assert call in gates.LLM_CALLS
    source = f"async def f(deps):\n    return await deps.llm.{call}('x')\n"
    assert gates.llm_calls(source, "fixture.py")


def test_the_differential_applies_to_the_orchestrator_and_not_to_the_v1_path():
    from scripts import kernel_differential as kd

    registry = gates.load_modules()
    assert kd.touches_kernel(["src/agent/interpreted_turn.py"], registry)
    assert kd.touches_kernel(["src/worker/kernel_commit.py"], registry)
    assert not kd.touches_kernel(["src/worker/stages/agent.py"], registry)
    assert not kd.touches_kernel(["src/agent/deterministic.py"], registry)


# --- 8. `scripts/kernel_trace.py` -----------------------------------------------------------------


class _Conn:
    def __init__(self, value=None, row=None):
        self.value, self.row, self.queries = value, row, []

    async def fetchval(self, sql, *args):
        self.queries.append((sql, args))
        return self.value

    async def fetchrow(self, sql, *args):
        self.queries.append((sql, args))
        return self.row


async def test_kernel_trace_script_prints_a_journey_trace_from_the_tenant():
    from scripts import kernel_trace as script

    trace = _trace()
    tenant = _Conn(
        row={
            "diagnostics": json.dumps({"kernel": trace.model_dump(mode="json")}),
            "client_text": "Vreau un telefon.",
        }
    )
    admin = _Conn(value="11111111-1111-1111-1111-111111111111")
    out = await script.load(
        "sole-ro",
        "22222222-2222-2222-2222-222222222222",
        admin=lambda: _ctxmgr(admin),
        tenant=lambda bid: _ctxmgr(tenant),
    )
    assert out == render(trace, user_text="Vreau un telefon.")
    sql, args = tenant.queries[0]
    assert "business_id = $1" in sql and args[0] == "11111111-1111-1111-1111-111111111111"


async def test_kernel_trace_script_prints_a_fallback():
    from scripts import kernel_trace as script

    tenant = _Conn(
        row={
            "diagnostics": {"kernel_fallback": {"reason": "refused", "vocabulary_snapshot": "abc"}},
            "client_text": None,
        }
    )
    out = await script.load(
        "sole-ro",
        "t",
        admin=lambda: _ctxmgr(_Conn(value="b")),
        tenant=lambda bid: _ctxmgr(tenant),
    )
    assert "refused" in out and "abc" in out


def _ctxmgr(conn):
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _cm():
        yield conn

    return _cm()


# --- 9. journey-urile × 5 pachete prin stagiul REAL -----------------------------------------------

JOURNEYS = replay.load_journeys()


@pytest.mark.parametrize("pack", sh.PACKS)
async def test_journeys_through_the_real_stage_match_the_pure_chain(monkeypatch, pack):
    cat = sh.catalog(pack)
    sh.install(monkeypatch, cat)
    journeys = [j for j in JOURNEYS if j.pack == pack]
    assert journeys, pack
    reached = 0
    for journey in journeys:
        for item in sh.chain(journey, cat):
            where = f"{journey.journey_id}#{item.index}"
            ctx = sh.build_ctx(
                cat,
                item.state_before,
                item.turn.user_input,
                item.previous,
                turn_id=f"t{item.index}",
                locale=journey.locale,
            )
            llm = sh.StageLLM(item.turn.expect["interpretation"])
            run = await sh.run_turn(monkeypatch, cat, ctx, llm)
            if not run.reached:
                # oprit înainte de ramură: o scurtătură exactă a servit sau paginare pură, 0 apeluri
                assert run.interpret_rows == [], where
                assert ctx.reply is not None or det.is_pure_pagination(ctx), where
                continue
            reached += 1
            assert run.branch_result is False, where
            assert llm.loops or ctx.reply is not None, f"{where}: calea v1 n-a răspuns"
            assert len(run.interpret_rows) == 1, where
            assert "kernel" in ctx.trace, (where, ctx.trace.get("kernel_fallback"))
            trace = KernelTrace.model_validate(ctx.trace["kernel"])
            divergence = first_divergence(sh.expected_layers(item.step), trace)
            assert divergence is None, f"{where}: {divergence.report()}"
            assert trace.plans == list(item.step.planned.plans), where
    assert reached, f"niciun tur din {pack} n-a ajuns la ramură"


def test_the_replay_vocabulary_of_sole_stays_empty():
    from tests.kernel import fixture_catalog

    assert fixture_catalog.vocabulary("sole-ro").is_empty()


def test_the_harness_uses_invented_products_for_sole():
    from tests.kernel import fixture_sole_catalog

    assert fixture_sole_catalog.PRODUCTS and all(
        p["id"].startswith("so-") for p in fixture_sole_catalog.PRODUCTS
    )


def test_the_shown_fixture_is_unchanged():
    assert [r.product_id for r in SHOWN] == ["p1", "p2", "p3", "p4", "p5"]
    assert isinstance(SHOWN[0], ProductRef) and _RecordLLM
