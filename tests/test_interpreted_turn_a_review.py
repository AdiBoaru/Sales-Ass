"""NX-336 PR A — corecturile după recenzia adversarială independentă.

Fiecare test de aici a fost scris ÎNAINTE de reparație și a picat pe codul livrat (tiparul
`tests/test_nx335_review.py`). Numerotarea urmează constatările recenziei:

1. redactarea: `CheckedChange.canonical_value` (și etichetele nevoilor din stare) scăpau în trace;
2. fallback ≠ OFF: scurtăturile exacte rulau de două ori (citiri + evenimente dublate);
3. o singură ancoră: verdictul `fallback` al linkului / al comparației pe exact 2 carduri e exact;
6. chip-urile: cu `CHIP_MOVES_V2_ENABLED` stins (implicitul) textul unui chip ajunge la ramură;
7. vocabularul indisponibil e un fallback ÎNAINTEA apelului;
8. instantaneul nu restaurează prin `setattr` niciun câmp de stare;
9. instantaneul picat, restaurarea picată, evenimentele emise o singură dată.

Zero model real, zero DB."""

from __future__ import annotations

import dataclasses
import json
from types import SimpleNamespace as NS

import pytest

from src.agent import deterministic as det
from src.config import get_settings
from src.conversation.interpretation import (
    Act,
    Ambiguity,
    Reference,
    StateChange,
    TurnInterpretation,
)
from src.conversation.state_v2 import ConversationStateV2, DisplayedRef, References
from src.worker.stages import agent as agent_mod
from tests.kernel import stage_harness as sh
from tests.test_interpreted_turn_a import FIND, shortcuts  # noqa: F401 — fixture
from tests.test_nx326_named_shortcut_targets import SHOWN, _ctx, _deps

MAIL = "ana.pop@example.com"
PHONE = "0722 123 456"

#: Evenimentele KERNELULUI: singurele pe care un tur căzut le are în plus față de flagul stins.
KERNEL_EVENTS = frozenset(
    {"turn_interpretation", "ambiguity_decision", "answer_policy", "kernel_delta", "kernel_turn"}
)


@pytest.fixture
def electronics(monkeypatch):
    cat = sh.catalog("electronics")
    sh.install(monkeypatch, cat)
    return cat


def _events(ctx, name):
    return [e.properties for e in ctx.events if e.type == name]


def _shown_state(cat, n: int) -> ConversationStateV2:
    ids = list(cat.items)[:n]
    shown = tuple(DisplayedRef(p, cat.items[p]["name"], float(cat.items[p]["price"])) for p in ids)
    return ConversationStateV2(references=References(displayed_products=shown))


def _leaves(obj, path=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _leaves(v, f"{path}.{k}")
    elif isinstance(obj, list | tuple):
        for i, v in enumerate(obj):
            yield from _leaves(v, f"{path}[{i}]")
    elif isinstance(obj, str):
        yield path, obj


# --- 1. redactarea, prin stagiul REAL -------------------------------------------------------------


def _pii_interpretation() -> TurnInterpretation:
    """PII-ul semănat în FIECARE câmp text scris de model, inclusiv id-ul referinței (a doua
    recenzie: `ResolvedRef.ref_id` îl copia neredactat), țintele actului, `target` și
    `relative_to` ale schimbării și ținta ambiguității."""
    return TurnInterpretation(
        thread="continue",
        acts=[Act(kind="find", targets=[MAIL], query=f"telefon pentru {MAIL}")],
        changes=[
            StateChange(
                op="add",
                target=None,
                dimension="unmapped",
                relation="eq",
                value=MAIL,
                number=None,
                unit=None,
                relative_to=None,
                quote=f"telefon pentru {MAIL}",
            ),
            StateChange(
                op="replace",
                target=PHONE,
                dimension="price",
                relation="lte",
                value=None,
                number=None,
                unit=None,
                relative_to=MAIL,
                quote=f"sunati la {PHONE}",
            ),
            StateChange(
                op="add",
                target=None,
                dimension="unmapped",
                relation="eq",
                value=PHONE,
                number=None,
                unit=PHONE,
                relative_to=None,
                quote=f"sunati la {PHONE}",
            ),
        ],
        references=[
            Reference(
                id=MAIL,
                text=MAIL,
                kind="name",
                ordinal=None,
                name=MAIL,
                dimension=None,
                value=PHONE,
                direction=None,
            )
        ],
        ambiguities=[Ambiguity(about="value", target=MAIL, readings=[MAIL, PHONE])],
        corrects_previous_turn=False,
    )


async def test_r1_no_seeded_pii_anywhere_in_the_stored_trace(monkeypatch, electronics):
    text = f"Vreau un telefon pentru {MAIL}, sunati la {PHONE}"
    ctx = sh.build_ctx(electronics, ConversationStateV2(), text)
    await sh.run_turn(monkeypatch, electronics, ctx, sh.StageLLM(_pii_interpretation()))
    assert "kernel" in ctx.trace, ctx.trace.get("kernel_fallback")
    leaked = [
        (path, value)
        for path, value in _leaves(ctx.trace["kernel"])
        if MAIL in value or "0722" in value
    ]
    assert leaked == [], f"PII în traceul stocat: {leaked}"
    assert not [e.type for e in ctx.events if MAIL in json.dumps(e.properties, default=str)], (
        "PII într-un eveniment"
    )


def test_r1_the_contract_lists_canonical_value_as_redacted():
    from tests.kernel import gates

    doc = (gates.ROOT / "docs" / "KERNEL-CONTRACT-v1.md").read_text(encoding="utf-8")
    assert "`CheckedChange.canonical_value`" in doc
    assert "`ResolvedRef.ref_id`" in doc


# --- 2. fallback = OFF pe scurtături: aceleași citiri, aceleași evenimente ------------------------

SHORTCUT_TEXTS = (
    ("Trimite-mi linkul la Cerave", 3),  # nume negăsit: v2 → model
    ("compară Cerave cu primul", 3),  # comparație cu o țintă numită negăsită
    ("spune-mi mai multe despre el", 3),  # detaliu, ancoră ambiguă
    ("ce părere au clienții despre el?", 3),  # recenzii, ancoră ambiguă
    ("Compară-l cu un produs similar", 1),  # chip-ul nostru, fără partener
)


def _no_clock(value):
    if isinstance(value, dict):
        return {k: _no_clock(v) for k, v in value.items() if k != "asked_at"}
    if isinstance(value, list):
        return [_no_clock(v) for v in value]
    return value


def _surface(run: sh.StageRun) -> dict:
    from src.agent.interpreted_turn import KERNEL_READS
    from src.worker.processor import _turn_proposals

    ctx = run.ctx
    return {
        # I16 exclude ceasul: `pending_question.asked_at` e momentul turului, nu suprafața lui
        "reply": _no_clock(dataclasses.asdict(ctx.reply)) if ctx.reply is not None else None,
        "tool_loops": list(run.llm.loops),
        "state": ctx.state,
        "state_patch": ctx.state_patch,
        "proposals": [repr(p) for p in _turn_proposals(ctx, is_rich=False, has_products=False)],
        "db": [op for op in run.db.ops if op not in KERNEL_READS],
        "events": [
            (e.type, json.dumps(e.properties, sort_keys=True, default=str))
            for e in ctx.events
            if e.type not in KERNEL_EVENTS
        ],
    }


async def _run(monkeypatch, cat, text, shown, *, on, fault=None):
    monkeypatch.setattr(get_settings(), "interpreted_turn_enabled", on)
    monkeypatch.setattr(get_settings(), "compare_with_similar_enabled", True)
    llm = sh.StageLLM(FIND)
    llm.transport.fault = fault
    ctx = sh.build_ctx(cat, _shown_state(cat, shown), text)
    return await sh.run_turn(monkeypatch, cat, ctx, llm)


@pytest.mark.parametrize("fault", [None, "refused"])
@pytest.mark.parametrize(("text", "shown"), SHORTCUT_TEXTS)
async def test_r2_a_fallback_on_a_shortcut_text_is_the_flag_off_turn(
    monkeypatch, electronics, text, shown, fault
):
    on = await _run(monkeypatch, electronics, text, shown, on=True, fault=fault)
    off = await _run(monkeypatch, electronics, text, shown, on=False, fault=fault)
    assert on.reached, "textul trebuie să ajungă la ramură (nu e o scurtătură exactă)"
    a, b = _surface(on), _surface(off)
    diff = {k: (a[k], b[k]) for k in a if a[k] != b[k]}
    assert diff == {}


@pytest.mark.parametrize(
    "text", ["spune-mi mai multe despre el", "ce părere au clienții despre el?"]
)
async def test_r2_an_anchor_resolved_by_the_v2_precedence_is_not_doubled(
    monkeypatch, electronics, text
):
    """Cu precedența v2 (`web_reference_resolved` + propunerea de selecție pe `selected_product`
    ieșit de pe ecran), rezolvarea ancorei are efecte: pe un tur căzut se redau, nu se repetă."""
    monkeypatch.setattr(get_settings(), "reference_precedence_v2_enabled", True)
    state = dataclasses.replace(
        _shown_state(electronics, 3),
        references=dataclasses.replace(
            _shown_state(electronics, 3).references, selected_product="el-99"
        ),
    )

    async def run(on):
        monkeypatch.setattr(get_settings(), "interpreted_turn_enabled", on)
        ctx = sh.build_ctx(electronics, state, text)
        return await sh.run_turn(monkeypatch, electronics, ctx, sh.StageLLM(FIND))

    on, off = await run(True), await run(False)
    assert on.reached
    assert len(_events(on.ctx, "web_reference_resolved")) == 1
    a, b = _surface(on), _surface(off)
    assert {k: (a[k], b[k]) for k in a if a[k] != b[k]} == {}


async def test_r2_the_kernel_vocabulary_read_has_its_own_label(monkeypatch, electronics):
    from src.agent.interpreted_turn import KERNEL_READS

    run = await _run(monkeypatch, electronics, "Vreau un telefon.", 0, on=True)
    assert "load_vocabulary" not in KERNEL_READS
    assert "kernel_load_vocabulary" in KERNEL_READS
    assert "kernel_load_vocabulary" in run.db.ops


# --- 3. o singură ancoră: verdictul `fallback` e exact --------------------------------------------


@pytest.mark.parametrize("text", ["Trimite-mi linkul", "dă-mi linkul, te rog"])
async def test_r3_a_link_with_one_anchor_is_exact(shortcuts, text):  # noqa: F811
    ctx = _ctx(text, shown=SHOWN[:1])
    assert await det.try_pre_intents(ctx, _deps(), exact_only=True) is True
    assert shortcuts["by_ids"] == [["p1"]]


async def test_r3_our_link_chip_with_one_anchor_is_exact(shortcuts):  # noqa: F811
    chip = det._detail_copy("ro")["link_chip"]
    ctx = _ctx(chip, shown=SHOWN[:1])
    assert await det.try_pre_intents(ctx, _deps(), exact_only=True) is True
    assert shortcuts["by_ids"] == [["p1"]]


async def test_r3_a_comparison_on_exactly_two_cards_is_exact(shortcuts):  # noqa: F811
    ctx = _ctx("compară-le", shown=SHOWN[:2])
    assert await det.try_pre_intents(ctx, _deps(), exact_only=True) is True
    assert shortcuts["compared"] == [["p1", "p2"]]


@pytest.mark.parametrize(("text", "n"), [("Trimite-mi linkul", 3), ("compară-le", 3)])
async def test_r3_three_or_more_cards_still_go_to_the_kernel(shortcuts, text, n):  # noqa: F811
    ctx = _ctx(text, shown=SHOWN[:n])
    assert await det.try_pre_intents(ctx, _deps(), exact_only=True) is False
    assert shortcuts["by_ids"] == [] and shortcuts["compared"] == []


# --- 6. chip-urile: implicitul de azi, fixat ------------------------------------------------------


@pytest.fixture
def branch(monkeypatch):
    from src.agent import interpreted_turn as it

    calls: list[str] = []

    async def spy(ctx, deps):
        calls.append(ctx.turn_id)
        return False

    monkeypatch.setattr(it, "run_interpreted_turn", spy)
    return calls


def _recognizing(monkeypatch):
    """`chip_press.recognize` recunoaște textul ca pe un chip oferit (funcția pură); restul lui
    `_recognize_chip_press` e cel real, inclusiv poarta de flag."""
    from src.conversation import chip_press

    move = NS(kind="reviews", move_id="m1", slot_map=dict)
    monkeypatch.setattr(chip_press, "recognize", lambda *a, **k: move)
    monkeypatch.setattr(det, "serve_chip_move", _no_serve)
    return move


async def _no_serve(ctx, deps, move):
    return False


async def test_r6_with_v2_chips_on_a_recognized_chip_stays_on_v1(monkeypatch, electronics, branch):
    _recognizing(monkeypatch)
    monkeypatch.setattr(get_settings(), "chip_moves_v2_enabled", True)
    ctx = sh.build_ctx(electronics, _shown_state(electronics, 3), "Vreau un telefon.")
    await agent_mod.agent_stage(ctx, _deps(sh.StageLLM(FIND)))
    assert ctx.chip_move is not None and branch == []


async def test_r6_pin_with_v2_chips_off_the_default_a_chip_text_reaches_the_branch(
    monkeypatch, electronics, branch
):
    """BLOCANT pentru PR C: cu `CHIP_MOVES_V2_ENABLED` stins (implicitul de producție) chip-ul nu e
    recunoscut, deci textul lui intră în kernel. Înainte ca ramura să servească ture, apăsările
    trebuie recunoscute independent de flag, sau flagul cerut la boot."""
    _recognizing(monkeypatch)
    assert get_settings().chip_moves_v2_enabled is False
    ctx = sh.build_ctx(electronics, _shown_state(electronics, 3), "Vreau un telefon.")
    await agent_mod.agent_stage(ctx, _deps(sh.StageLLM(FIND)))
    assert ctx.chip_move is None and branch == ["t0"]


# --- 7. vocabularul indisponibil: fallback ÎNAINTEA apelului --------------------------------------


async def test_r7_an_unavailable_vocabulary_falls_back_before_the_call(monkeypatch, electronics):
    from src.agent import interpreted_turn as it
    from src.catalog import vocabulary_cache

    async def broken(conn, business_id):
        raise ConnectionError("pooler plin")

    vocabulary_cache.clear_vocabulary_cache()
    monkeypatch.setattr(vocabulary_cache, "load_vocabulary", broken)
    monkeypatch.setattr(it, "get_vocabulary", vocabulary_cache.get_vocabulary)
    llm = sh.StageLLM(FIND)
    ctx = sh.build_ctx(electronics, ConversationStateV2(), "Vreau un telefon.")
    run = await sh.run_turn(monkeypatch, electronics, ctx, llm)
    assert ctx.trace["kernel_fallback"]["reason"] == "vocabulary_unavailable"
    assert run.interpret_rows == [] and llm.transport.calls == []
    assert llm.loops, "calea v1 răspunde"


# --- 8. instantaneul nu restaurează stare prin `setattr` ------------------------------------------


def test_r8_no_state_field_is_restored_through_setattr():
    from src.agent.interpreted_turn import EXECUTOR_WRITABLE

    assert not {"state", "state_v2", "state_patch", "state_proposals"} & set(EXECUTOR_WRITABLE)


def test_r8_the_proposals_an_executor_appends_are_restored(electronics):
    from src.agent.interpreted_turn import ContextSnapshot

    ctx = sh.build_ctx(electronics, ConversationStateV2(), "x")
    ctx.state_proposals.append("before")
    saved = ContextSnapshot.take(ctx)
    ctx.state_proposals.append("executor")
    ctx.state_patch["active_search"] = {"fp": "x"}
    saved.restore(ctx)
    assert ctx.state_proposals == ["before"] and ctx.state_patch == {}


# --- 9. instantaneul picat, restaurarea picată, evenimentele o singură dată -----------------------


async def test_r9_a_failing_snapshot_is_recorded(monkeypatch, electronics):
    from src.agent import interpreted_turn as it

    def boom(ctx):
        raise RuntimeError("deepcopy")

    monkeypatch.setattr(it.ContextSnapshot, "take", classmethod(lambda cls, ctx: boom(ctx)))
    ctx = sh.build_ctx(electronics, ConversationStateV2(), "Vreau un telefon.")
    run = await sh.run_turn(monkeypatch, electronics, ctx, sh.StageLLM(FIND))
    assert ctx.trace["kernel_fallback"]["reason"] == "snapshot_error"
    [turn] = _events(ctx, "kernel_turn")
    assert turn["fallback_reason"] == "snapshot_error"
    assert run.interpret_rows == []


async def test_r9_a_failing_restore_is_counted_and_v1_continues(monkeypatch, electronics):
    from src.agent import interpreted_turn as it

    def broken(self, ctx):
        raise RuntimeError("restore")

    monkeypatch.setattr(it.ContextSnapshot, "restore", broken)
    ctx = sh.build_ctx(electronics, ConversationStateV2(), "Vreau un telefon.")
    run = await sh.run_turn(monkeypatch, electronics, ctx, sh.StageLLM(FIND))
    assert len(_events(ctx, "kernel_restore_failed")) == 1
    assert run.branch_result is False and run.llm.loops


async def test_r9_no_chain_event_before_an_exception(monkeypatch, electronics):
    """Traceul picat după lanț: niciun eveniment al lanțului (`ambiguity_decision`,
    `kernel_delta`), un singur `kernel_turn` cu `exception`."""
    from src.agent import interpreted_turn as it

    def boom(*a, **k):
        raise RuntimeError("cap")

    monkeypatch.setattr(it, "cap_trace", boom)
    ctx = sh.build_ctx(electronics, ConversationStateV2(), "Vreau un telefon.")
    await sh.run_turn(monkeypatch, electronics, ctx, sh.StageLLM(FIND))
    assert _events(ctx, "ambiguity_decision") == [] and _events(ctx, "kernel_delta") == []
    [turn] = _events(ctx, "kernel_turn")
    assert turn["fallback_reason"] == "exception"
    assert len(_events(ctx, "turn_interpretation")) == 1
