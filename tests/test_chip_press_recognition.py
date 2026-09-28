"""NX-338 — apăsarea unui chip se RECUNOAȘTE și cu `CHIP_MOVES_V2_ENABLED` stins.

Condiția ramurii kernelului (NX-336) lasă pe v1 turele cu un chip apăsat: un chip e o mutare emisă
de server, nu o frază de interpretat. Recunoașterea rula însă doar sub V2 (stins în producție),
deci apăsările ar fi plecat la modelul de interpretare. Acum FAPTUL (`chip_recognized`) e separat
de SERVIRE (`chip_move`, a lui V2, neschimbată).

Recunoașterea de aici e cea REALĂ (`chip_press.recognize` pe chips-urile randate de
`chip_moves`, ca la emitere), nu un stub. Zero model real, zero DB."""

from __future__ import annotations

import ast
import copy
from pathlib import Path

import pytest

from src.config import get_settings
from src.conversation import chip_moves, chip_press
from src.worker.stages import agent as agent_mod
from tests.kernel import stage_harness as sh
from tests.test_interpreted_turn_a import FIND
from tests.test_interpreted_turn_a_review import KERNEL_EVENTS, _shown_state
from tests.test_nx326_named_shortcut_targets import _deps

ROOT = Path(__file__).resolve().parents[1]


def _unique_anchor() -> bool:  # pragul de scurtare cu care se randează chips-urile
    return bool(getattr(get_settings(), "unique_name_prefix_enabled", False))


def _offered(cat, ctx) -> dict[str, str]:
    """`{fel: text}` al chips-urilor din carduri, randate EXACT ca la emitere; `offered_chips` le
    primește id-urile, ca după un tur v1 cu `CHIP_MOVES_V1_ENABLED`."""
    cards = [
        {"product_id": p.product_id, "name": p.name, "price": p.price}
        for p in ctx.state.displayed_products
    ]
    unique = bool(getattr(get_settings(), "unique_name_prefix_enabled", False))
    moves = chip_moves.renderable(
        chip_moves.from_cards(cards, unique_anchor=unique, locale=ctx.language),
        cat.pack,
        ctx.language,
    )
    texts: dict[str, str] = {}
    ids: list[str] = []
    for move in moves:
        text = chip_moves.render_move(move, cat.pack, ctx.language)
        if text is None or move.kind not in chip_press.PRESSABLE:
            continue
        texts.setdefault(move.kind, text)
        ids.append(move.move_id)
    ctx.state.offered_chips = ids
    return texts


def _press(cat, kind: str, n: int = 3):
    ctx = sh.build_ctx(cat, _shown_state(cat, n), "placeholder")
    texts = _offered(cat, ctx)
    assert kind in texts, f"pachetul nu oferă «{kind}» pe {n} carduri: {sorted(texts)}"
    ctx.message.body = texts[kind]
    ctx.history[-1].body = texts[kind]
    return ctx


@pytest.fixture
def electronics(monkeypatch):
    cat = sh.catalog("electronics")
    sh.install(monkeypatch, cat)
    return cat


@pytest.fixture
def branch(monkeypatch):
    """Ramura spionată: id-urile turelor care au ajuns la ea (servirea întoarce `False`)."""
    from src.agent import interpreted_turn as it

    calls: list[str] = []

    async def spy(ctx, deps):
        calls.append(ctx.turn_id)
        return False

    monkeypatch.setattr(it, "run_interpreted_turn", spy)
    return calls


@pytest.fixture
def v2_off(monkeypatch):
    monkeypatch.setattr(get_settings(), "chip_moves_v2_enabled", False)


# --- faptul, independent de flag ------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["link", "detail"])
async def test_v2_off_kernel_on_a_real_press_is_recognized_and_stays_on_v1(
    electronics, branch, v2_off, kind
):
    ctx = _press(electronics, kind)
    await agent_mod.agent_stage(ctx, _deps(sh.StageLLM(FIND)))
    assert ctx.chip_recognized is not None and ctx.chip_recognized.kind == kind
    assert ctx.chip_move is None  # v1 nu o servește ca mutare fără V2: calea de text, ca azi
    assert branch == []


async def test_v2_on_serving_is_the_nx316_behaviour(electronics, branch, monkeypatch):
    monkeypatch.setattr(get_settings(), "chip_moves_v2_enabled", True)
    ctx = _press(electronics, "link")
    await agent_mod.agent_stage(ctx, _deps(sh.StageLLM(FIND)))
    assert ctx.chip_move is not None and ctx.chip_move is ctx.chip_recognized
    assert branch == []
    assert all(e.properties.get("handler") != "text_path" for e in ctx.events)


async def test_a_reworded_chip_is_a_new_message_and_reaches_the_kernel(electronics, branch, v2_off):
    ctx = _press(electronics, "link")
    ctx.message.body = ctx.history[-1].body = ctx.message.body + " te rog"
    await agent_mod.agent_stage(ctx, _deps(sh.StageLLM(FIND)))
    assert ctx.chip_recognized is None and branch == ["t0"]


# --- fără consumator, fără muncă ------------------------------------------------------------------


async def test_both_flags_off_nothing_runs(electronics, v2_off, monkeypatch):
    monkeypatch.setattr(get_settings(), "interpreted_turn_enabled", False)
    calls: list[str] = []

    def spy(*a, **k):
        calls.append("recognize")
        return None

    async def phrases(*a, **k):
        calls.append("phrases")
        return {}

    monkeypatch.setattr(chip_press, "recognize", spy)
    monkeypatch.setattr(agent_mod, "_offered_phrases", phrases)
    ctx = _press(electronics, "link")
    await agent_mod.agent_stage(ctx, _deps(sh.StageLLM(FIND)))
    assert calls == []
    assert ctx.chip_recognized is None and ctx.chip_move is None


async def test_a_failing_recognizer_leaves_both_empty_and_the_turn_goes_on(
    electronics, branch, v2_off, monkeypatch
):
    def boom(*a, **k):
        raise RuntimeError("stricat")

    monkeypatch.setattr(chip_press, "recognize", boom)
    ctx = _press(electronics, "link")
    await agent_mod.agent_stage(ctx, _deps(sh.StageLLM(FIND)))
    assert ctx.chip_recognized is None and ctx.chip_move is None
    assert branch == ["t0"]  # un mesaj obișnuit, deci ramura îl primește (P6: nu tace)


# --- paritatea: kernel aprins vs stins, pe o apăsare recunoscută ----------------------------------


def _surface(run: sh.StageRun) -> dict:
    ctx = run.ctx
    events = [
        (e.type, {k: v for k, v in e.properties.items() if k != "turn_id"})
        for e in ctx.events
        if e.type not in KERNEL_EVENTS
        and not (e.type == "chip_pressed" and e.properties.get("handler") == "text_path")
    ]
    return {
        "reply": copy.deepcopy(ctx.reply),
        "state_patch": copy.deepcopy(ctx.state_patch),
        "retrieval": copy.deepcopy(ctx.retrieval),
        "db": list(run.db.ops),
        "loops": len(run.llm.loops),
        "calls": len(run.per_call),
        "events": events,
    }


@pytest.mark.parametrize(("kind", "n"), [("link", 3), ("compare", 2), ("detail", 3)])
async def test_a_recognized_press_is_served_the_same_with_the_kernel_on_or_off(
    monkeypatch, electronics, v2_off, kind, n
):
    runs = {}
    for kernel in (False, True):
        monkeypatch.setattr(get_settings(), "interpreted_turn_enabled", kernel)
        ctx = _press(electronics, kind, n)
        runs[kernel] = await sh.run_turn(monkeypatch, electronics, ctx, sh.StageLLM(FIND))
    assert runs[True].reached is False  # ramura nici n-a fost atinsă
    assert runs[True].ctx.chip_recognized is not None
    assert _surface(runs[True]) == _surface(runs[False])
    assert runs[True].ctx.reply is not None


# --- sonda: `nx316_chip_press_probe --recognize`, pe fixture --------------------------------------


def _trace_pair(cat, chip_texts, client_text, *, recommended=None, comparison_chips=()):
    shown = list(cat.items)[:3]
    refs = recommended or [
        {"product_id": p, "name": cat.items[p]["name"], "price": float(cat.items[p]["price"])}
        for p in shown
    ]
    prev = {
        "conv": "c1",
        "client_text": "arata-mi telefoane",
        "recommended": refs,
        "reply": {
            "rich": {"chips": [{"label": t} for t in chip_texts]},
            "comparison": {"chips": [{"label": t} for t in comparison_chips]},
        },
    }
    return [prev, {"conv": "c1", "client_text": client_text, "reply": {}}]


def test_probe_counts_a_real_press_as_recognized_with_v2_off(electronics, v2_off):
    from scripts.nx316_chip_press_probe import recognition_report

    ctx = sh.build_ctx(electronics, _shown_state(electronics, 3), "x")
    texts = _offered(electronics, ctx)
    rows = _trace_pair(electronics, list(texts.values()), texts["link"])
    rep = recognition_report(rows, electronics.pack, "ro", unique_anchor=_unique_anchor())
    assert rep["presses"] == 1 and rep["recognized_off"] == 1 and rep["pressable_miss"] == 0
    assert rep["by_kind"]["link"]["off"] == 1
    assert get_settings().chip_moves_v2_enabled is False  # brațul aprins s-a restaurat


def test_probe_flags_a_pressable_miss_when_a_real_move_is_not_recognized(
    electronics, v2_off, monkeypatch
):
    """Un chip cu forma unei mutări (sloturile numesc produse afișate) pe care recunoașterea nu-l
    prinde e un defect VIZIBIL, nu o absență. Recunoașterea e stricată aici ca să se vadă clasa."""
    from scripts import nx316_chip_press_probe as probe

    monkeypatch.setattr(probe, "recognize_press", lambda *a, **k: None)
    ctx = sh.build_ctx(electronics, _shown_state(electronics, 3), "x")
    texts = _offered(electronics, ctx)
    rows = _trace_pair(electronics, [texts["link"]], texts["link"])
    rep = probe.recognition_report(rows, electronics.pack, "ro", unique_anchor=_unique_anchor())
    assert rep["recognized_off"] == 0 and rep["pressable_miss"] == 1


def test_probe_a_text_that_only_looks_like_a_template_is_not_a_move(electronics, v2_off):
    """Pe `sole-ro`, «Compară IUNIK cu BELIF pentru ten uscat» (scris de model) și «Compară-l cu
    un produs similar» (copy determinist) se potriveau pe șablonul comparației: slotul care nu
    numește un produs afișat îi scoate din clasa mutărilor."""
    from scripts.nx316_chip_press_probe import recognition_report

    first = electronics.items[next(iter(electronics.items))]["name"].split()[0]
    for chip in (f"Compara {first} cu ceva pentru acasa", "Compară-l cu un produs similar"):
        rows = _trace_pair(electronics, [chip], chip)
        rep = recognition_report(rows, electronics.pack, "ro", unique_anchor=_unique_anchor())
        assert rep["misses"] == {"not_a_move": 1}, (chip, rep)
        assert rep["pressable_miss"] == 0


def test_probe_two_cards_with_the_same_name_are_a_pressable_miss(electronics, v2_off):
    """Recenzia NX-338: două carduri cu ACELAȘI nume (pe SOLE: 4 perechi KUNDAL) dau două mutări cu
    același text, iar recunoașterea le refuză pe amândouă (ambiguu), deci apăsarea ar pleca la
    kernel. E o ratare de văzut, nu un text care doar seamănă cu șablonul."""
    from scripts.nx316_chip_press_probe import recognition_report

    ids = list(electronics.items)[:3]
    twin = electronics.items[ids[0]]["name"]
    refs = [
        {"product_id": ids[0], "name": twin, "price": 10.0},
        {"product_id": ids[1], "name": twin, "price": 12.0},
        {"product_id": ids[2], "name": electronics.items[ids[2]]["name"], "price": 30.0},
    ]
    moves = chip_moves.renderable(
        chip_moves.from_cards(refs, unique_anchor=_unique_anchor(), locale="ro"),
        electronics.pack,
        "ro",
    )
    link = next(
        chip_moves.render_move(m, electronics.pack, "ro") for m in moves if m.kind == "link"
    )
    rows = _trace_pair(electronics, [link], link, recommended=refs)
    rep = recognition_report(rows, electronics.pack, "ro", unique_anchor=_unique_anchor())
    assert rep["recognized_off"] == 0 and rep["pressable_miss"] == 1, rep


def test_probe_a_name_containing_the_template_connector_is_still_a_move(
    electronics, v2_off, monkeypatch
):
    """Recenzia NX-338: pe «Compara Crema cu acid … cu Ser …» un regex taie primul nume la primul
    «cu». Forma se judecă pe ORICE împărțire, deci o ratare reală nu se ascunde în `not_a_move`."""
    from scripts import nx316_chip_press_probe as probe

    refs = [
        {"product_id": "a", "name": "Crema cu acid hialuronic", "price": 10.0},
        {"product_id": "b", "name": "Ser Vitamina C", "price": 20.0},
    ]
    moves = chip_moves.renderable(
        chip_moves.from_cards(refs, unique_anchor=_unique_anchor(), locale="ro"),
        electronics.pack,
        "ro",
    )
    compare = next(
        chip_moves.render_move(m, electronics.pack, "ro") for m in moves if m.kind == "compare"
    )
    assert compare.lower().count(" cu ") >= 2  # premisa: conectorul apare și în nume
    monkeypatch.setattr(probe, "recognize_press", lambda *a, **k: None)
    rows = _trace_pair(electronics, [compare], compare, recommended=refs)
    rep = probe.recognition_report(rows, electronics.pack, "ro", unique_anchor=_unique_anchor())
    assert rep["pressable_miss"] == 1, rep


def test_probe_classifies_comparison_chips_apart(electronics, v2_off):
    from scripts.nx316_chip_press_probe import recognition_report

    rows = _trace_pair(electronics, [], "Care e mai bun?", comparison_chips=["Care e mai bun?"])
    rep = recognition_report(rows, electronics.pack, "ro", unique_anchor=_unique_anchor())
    assert rep["misses"] == {"comparison_chip": 1} and rep["pressable_miss"] == 0


# --- proprietarul unic ----------------------------------------------------------------------------


def _attribute_uses(name: str) -> tuple[list[str], list[str]]:
    """(scrieri, citiri) ale atributului `name` în `src/`, pe AST (nu pe text), ca
    `fișier::funcție`: `x.name = …` și `setattr(x, "name", …)` sunt scrieri, orice alt `x.name` și
    `getattr(x, "name", …)` sunt citiri. Pe FUNCȚIE, nu pe fișier: o a doua scriere în alt loc
    al aceluiași modul (de pildă un reset în `agent_stage`) trebuie să pice."""
    writes: list[str] = []
    reads: list[str] = []
    for path in sorted((ROOT / "src").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        rel = path.relative_to(ROOT).as_posix()
        owner: dict[int, str] = {}
        for fn in ast.walk(tree):
            if isinstance(fn, ast.AsyncFunctionDef | ast.FunctionDef):
                for node in ast.walk(fn):
                    owner[id(node)] = fn.name  # funcția cea mai interioară câștigă (walk BFS)
        where = lambda node: f"{rel}::{owner.get(id(node), '<module>')}"  # noqa: E731
        stored = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign | ast.AugAssign | ast.AnnAssign):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for t in targets:
                    if isinstance(t, ast.Attribute) and t.attr == name:
                        stored.add(id(t))
                        writes.append(where(t))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == name and id(node) not in stored:
                reads.append(where(node))
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in ("getattr", "setattr", "hasattr")
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value == name
            ):
                (writes if node.func.id == "setattr" else reads).append(where(node))
    return writes, reads


def test_chip_recognized_has_one_writer_and_one_reader():
    writes, reads = _attribute_uses("chip_recognized")
    # UN loc de scriere (`_recognize_chip_press`) și UN loc de citire (condiția ramurii)
    assert writes == ["src/worker/stages/agent.py::_recognize_chip_press"]
    assert reads == ["src/worker/stages/agent.py::agent_stage"]


def test_chip_move_writer_is_unchanged():
    writes, _ = _attribute_uses("chip_move")
    assert writes == ["src/worker/stages/agent.py::_recognize_chip_press"]
