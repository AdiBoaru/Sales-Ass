"""NX-353 — pasul 7: kernelul DARK pe tot traficul, apoi canary per conversație.

Ce dovedește suita (cardul, „Test Cases"):

- decizia de mod e pură: tenanții din listă, canary-ul sticky pe (tenant, conversație), dark doar
  cu flagul lui, iar serve-ul are prioritate doar în bucket;
- poarta de boot: dark cere starea v2 CITITĂ (nu scrisă) și refuză creierul unic;
- un tur dark e turul cu ambele flaguri stinse pe suprafața I16 (răspuns, unelte, stare,
  propuneri, citirile căii v1, evenimentele în afara celor ale kernelului); citirile din ramură sunt
  ale kernelului, declarat;
- pe un plan `search` principal, dark-ul rulează DOAR căutarea planificată și reține id-urile în
  `ctx.trace["kernel_dark"]` (zero text de client); pe un alt plan nu execută nimic; o căutare care
  aruncă se numără, iar v1 răspunde;
- pe orice fallback al lanțului, dark-ul nu caută nimic.

Zero model, zero DB: transport fals sub clientul real, stub-uri pe pachet (`tests/kernel/`)."""

from __future__ import annotations

import dataclasses
import json
from types import SimpleNamespace as NS

import pytest

from src.config import Settings, get_settings
from src.conversation.interpretation import Act, TurnInterpretation
from src.conversation.state_v2 import ConversationStateV2
from src.tools.base import ToolResult
from src.worker.stages.agent import canary_bucket, kernel_mode
from tests.kernel import gates
from tests.kernel import stage_harness as sh

FIND = TurnInterpretation(
    thread="continue",
    acts=[Act(kind="find", targets=[], query="telefon")],
    changes=[],
    references=[],
    ambiguities=[],
    corrects_previous_turn=False,
)
OTHER = FIND.model_copy(update={"acts": [Act(kind="other", targets=[], query=None)]})

_KERNEL_EVENTS = frozenset(
    {
        "turn_interpretation",
        "ambiguity_decision",
        "answer_policy",
        "kernel_delta",
        "kernel_turn",
        "kernel_dark",
    }
)


@pytest.fixture
def electronics(monkeypatch):
    cat = sh.catalog("electronics")
    sh.install(monkeypatch, cat)
    return cat


def _settings(**kw):
    base = dict(
        interpreted_turn_enabled=False,
        interpreted_turn_dark_enabled=False,
        interpreted_turn_canary_percent=100,
        interpreted_turn_tenants="",
    )
    return NS(**{**base, **kw})


BIZ = NS(id="99fe1292-f9ed-469e-8183-f994ea5b59c0", slug="sole-ro")


# --- 1. decizia de mod ---------------------------------------------------------------------------


def test_every_flag_off_is_no_kernel():
    assert kernel_mode(_settings(), BIZ, "conv") is None


def test_dark_alone_is_dark_on_every_conversation():
    s = _settings(interpreted_turn_dark_enabled=True)
    assert {kernel_mode(s, BIZ, f"c{i}") for i in range(200)} == {"dark"}


def test_serve_at_100_percent_is_serve_on_every_conversation():
    s = _settings(interpreted_turn_enabled=True, interpreted_turn_dark_enabled=True)
    assert {kernel_mode(s, BIZ, f"c{i}") for i in range(200)} == {"serve"}


def test_serve_at_0_percent_leaves_everything_dark_or_off():
    dark = _settings(
        interpreted_turn_enabled=True,
        interpreted_turn_dark_enabled=True,
        interpreted_turn_canary_percent=0,
    )
    off = _settings(interpreted_turn_enabled=True, interpreted_turn_canary_percent=0)
    assert {kernel_mode(dark, BIZ, f"c{i}") for i in range(200)} == {"dark"}
    assert {kernel_mode(off, BIZ, f"c{i}") for i in range(200)} == {None}


def test_the_canary_splits_by_the_sticky_bucket():
    s = _settings(
        interpreted_turn_enabled=True,
        interpreted_turn_dark_enabled=True,
        interpreted_turn_canary_percent=50,
    )
    modes = {f"c{i}": kernel_mode(s, BIZ, f"c{i}") for i in range(1000)}
    # sticky: aceeași conversație, același verdict, la fiecare tur
    assert all(kernel_mode(s, BIZ, cid) == mode for cid, mode in modes.items())
    assert all(
        (mode == "serve") == (canary_bucket(BIZ.id, cid) < 50) for cid, mode in modes.items()
    )
    served = sum(mode == "serve" for mode in modes.values())
    assert 400 <= served <= 600, served


def test_the_bucket_is_deterministic_bounded_and_per_tenant():
    buckets = [canary_bucket(BIZ.id, f"c{i}") for i in range(500)]
    assert buckets == [canary_bucket(BIZ.id, f"c{i}") for i in range(500)]
    assert all(0 <= b < 100 for b in buckets)
    other = [canary_bucket("b-other", f"c{i}") for i in range(500)]
    assert buckets != other


def test_a_tenant_outside_the_list_gets_no_kernel():
    s = _settings(
        interpreted_turn_enabled=True,
        interpreted_turn_dark_enabled=True,
        interpreted_turn_tenants="sole-ro, smoke-prod",
    )
    assert kernel_mode(s, BIZ, "conv") == "serve"
    assert kernel_mode(s, NS(id="b2", slug="alt-magazin"), "conv") is None
    assert kernel_mode(s, NS(id="b3", slug="smoke-prod"), "conv") == "serve"


# --- 2. poarta de boot ----------------------------------------------------------------------------


def _boot(monkeypatch, env):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return Settings()


def test_the_new_settings_are_off_by_default():
    s = Settings()
    assert s.interpreted_turn_dark_enabled is False
    assert s.interpreted_turn_canary_percent == 100
    assert s.interpreted_turn_tenants == ""


def test_dark_needs_the_v2_state_read_but_not_its_write(monkeypatch):
    s = _boot(
        monkeypatch,
        {"INTERPRETED_TURN_DARK_ENABLED": "true", "CONVERSATION_STATE_V2_ENABLED": "true"},
    )
    assert s.interpreted_turn_dark_enabled and not s.conversation_state_v2_write_enabled


def test_dark_is_refused_without_the_v2_state_read(monkeypatch):
    with pytest.raises(Exception, match="INTERPRETED_TURN_DARK_ENABLED"):
        _boot(monkeypatch, {"INTERPRETED_TURN_DARK_ENABLED": "true"})


def test_dark_is_refused_with_the_single_brain(monkeypatch):
    env = {
        "INTERPRETED_TURN_DARK_ENABLED": "true",
        "CONVERSATION_STATE_V2_ENABLED": "true",
        "SINGLE_BRAIN_ENABLED": "true",
    }
    with pytest.raises(Exception, match="INTERPRETED_TURN_DARK_ENABLED"):
        _boot(monkeypatch, env)


@pytest.mark.parametrize("value", ["-1", "101"])
def test_the_canary_percent_is_bounded(monkeypatch, value):
    with pytest.raises(Exception, match="INTERPRETED_TURN_CANARY_PERCENT"):
        _boot(monkeypatch, {"INTERPRETED_TURN_CANARY_PERCENT": value})


def test_the_env_example_declares_the_new_settings_off():
    text = (gates.ROOT / ".env.example").read_text(encoding="utf-8")
    assert "INTERPRETED_TURN_DARK_ENABLED=false" in text
    assert "INTERPRETED_TURN_CANARY_PERCENT=100" in text
    assert "INTERPRETED_TURN_TENANTS=" in text


# --- 3. turul dark = turul stins pe suprafața I16 -------------------------------------------------


def _stub_search(monkeypatch, cat, ids, *, fail=False):
    """`run_planned_search` pe catalogul de fixture: id-urile date, o citire și un eveniment
    `product_search` cu treapta lexicală (ca în producție, evenimentul căutării)."""
    from src.tools import catalog_tools

    calls = []

    async def search(ctx, deps, args):
        calls.append(args)
        async with deps.db("search_products_ladder"):
            pass
        if fail:
            raise RuntimeError("căutarea a picat")
        ctx.emit("product_search", lexical_step="strict", n=len(ids))
        rows = sh.product_rows(cat, list(ids))
        ctx.retrieval = NS(products=rows)
        ctx.state_patch["active_search"] = {"fp": "dark", "pool": list(ids)}
        return ToolResult(ok=True, products=rows)

    monkeypatch.setattr(catalog_tools, "run_planned_search", search)
    return calls


def _mode(monkeypatch, *, dark, serve=False, tenants="", percent=100):
    s = get_settings()
    monkeypatch.setattr(s, "interpreted_turn_enabled", serve)
    monkeypatch.setattr(s, "interpreted_turn_dark_enabled", dark)
    monkeypatch.setattr(s, "interpreted_turn_tenants", tenants)
    monkeypatch.setattr(s, "interpreted_turn_canary_percent", percent)


async def _run(monkeypatch, cat, interp=FIND, *, fault=None, **mode):
    _mode(monkeypatch, **mode)
    llm = sh.StageLLM(interp)
    db = sh.RecordingDb()
    if fault in {"refused", "truncated", "invalid_json", "schema_violation", "provider_error"}:
        llm.transport.fault = fault
    elif fault == "vocabulary_unavailable":
        db.fail.add("kernel_load_vocabulary")
    elif fault == "facts_exception":
        db.fail.add("kernel_reference_facts")
    ctx = sh.build_ctx(cat, ConversationStateV2(), "Vreau un telefon.")
    return await sh.run_turn(monkeypatch, cat, ctx, llm, db=db)


def _surface(run: sh.StageRun) -> dict:
    from src.worker.processor import _turn_proposals

    ctx = run.ctx
    return {
        "reply": dataclasses.asdict(ctx.reply) if ctx.reply is not None else None,
        "tool_loops": list(run.llm.loops),
        "state": ctx.state,
        "state_patch": ctx.state_patch,
        "retrieval": repr(ctx.retrieval),
        "proposals": [repr(p) for p in _turn_proposals(ctx, is_rich=False, has_products=False)],
        # citirile din ramură sunt ale kernelului (declarat); restul sunt ale căii v1
        "db": run.ops_outside_branch(),
        "events": [
            (e.type, json.dumps(e.properties, sort_keys=True, default=str))
            for e in ctx.events
            if e.type not in _KERNEL_EVENTS
        ],
    }


def _events(ctx, name):
    return [e.properties for e in ctx.events if e.type == name]


async def test_a_dark_turn_is_the_flag_off_turn_on_the_i16_surface(monkeypatch, electronics):
    calls = _stub_search(monkeypatch, electronics, ["el-01", "el-02"])
    dark = await _run(monkeypatch, electronics, dark=True)
    off = await _run(monkeypatch, electronics, dark=False)
    assert dark.reached and not off.reached
    assert dark.branch_result is False
    assert _surface(dark) == _surface(off)
    assert len(calls) == 1, "dark rulează căutarea planificată o singură dată"


async def test_the_context_handed_to_v1_equals_the_one_before_the_dark_branch(
    monkeypatch, electronics
):
    _stub_search(monkeypatch, electronics, ["el-01"])
    run = await _run(monkeypatch, electronics, dark=True)
    diff = sorted(k for k in run.before_branch if run.before_branch[k] != run.after_branch[k])
    assert diff == []


async def test_dark_records_what_the_kernel_would_have_served(monkeypatch, electronics):
    _stub_search(monkeypatch, electronics, ["el-01", "el-02"])
    run = await _run(monkeypatch, electronics, dark=True)
    record = run.ctx.trace["kernel_dark"]
    assert record["executor"] == "search" and record["searched"] is True
    assert record["kernel_ids"] == ["el-01", "el-02"]
    assert record["lexical_step"] == "strict"
    assert isinstance(record["ms"], float)
    # adevărul proxy al raportului: starea kernelului după reducer (chei de catalog)
    assert set(record["truth"]) == {"types", "type_learned", "shelf", "needs"}
    (event,) = _events(run.ctx, "kernel_dark")
    assert {k: v for k, v in event.items() if k != "turn_id"} == {
        "executor": "search",
        "searched": True,
        "kernel_n": 2,
        "lexical_step": "strict",
    }
    (turn,) = _events(run.ctx, "kernel_turn")
    assert turn["served"] is False and turn["fallback_reason"] == "dark"
    assert "kernel" in run.ctx.trace, "traceul lanțului, ca pe PR A"
    assert len(run.interpret_rows) == 1, "un singur apel de model în plus: interpretarea"
    # zero text de client în înregistrare
    assert "telefon" not in json.dumps(record, ensure_ascii=False).lower()


async def test_dark_keeps_at_most_card_slots_ids(monkeypatch, electronics):
    many = [f"el-0{i}" for i in range(1, 10)]
    _stub_search(monkeypatch, electronics, many)
    monkeypatch.setattr(get_settings(), "card_slots", 3)
    run = await _run(monkeypatch, electronics, dark=True)
    assert len(run.ctx.trace["kernel_dark"]["kernel_ids"]) <= 3


async def test_a_plan_without_search_records_the_executor_and_runs_nothing(
    monkeypatch, electronics
):
    calls = _stub_search(monkeypatch, electronics, ["el-01"])
    dark = await _run(monkeypatch, electronics, OTHER, dark=True)
    off = await _run(monkeypatch, electronics, OTHER, dark=False)
    record = dark.ctx.trace["kernel_dark"]
    assert record["executor"] != "search" and record["searched"] is False
    assert record["kernel_ids"] == [] and calls == []
    assert _surface(dark) == _surface(off)


async def test_a_failing_dark_search_is_counted_and_v1_answers(monkeypatch, electronics):
    _stub_search(monkeypatch, electronics, ["el-01"], fail=True)
    dark = await _run(monkeypatch, electronics, dark=True)
    off = await _run(monkeypatch, electronics, dark=False)
    record = dark.ctx.trace["kernel_dark"]
    assert record == {"executor": "search", "searched": False, "error": "RuntimeError"}
    assert _events(dark.ctx, "kernel_dark")[0]["searched"] is False
    assert _surface(dark) == _surface(off)


DARK_FAULTS = (
    "refused",
    "truncated",
    "invalid_json",
    "schema_violation",
    "provider_error",
    "vocabulary_unavailable",
    "facts_exception",
)


@pytest.mark.parametrize("fault", DARK_FAULTS)
async def test_a_chain_fallback_in_dark_searches_nothing(monkeypatch, electronics, fault):
    calls = _stub_search(monkeypatch, electronics, ["el-01"])
    dark = await _run(monkeypatch, electronics, fault=fault, dark=True)
    off = await _run(monkeypatch, electronics, fault=fault, dark=False)
    assert dark.reached and calls == []
    assert "kernel_dark" not in dark.ctx.trace
    assert not _events(dark.ctx, "kernel_dark")
    assert "kernel_fallback" in dark.ctx.trace
    assert _surface(dark) == _surface(off)


async def test_a_tenant_outside_the_list_makes_no_model_call(monkeypatch, electronics):
    calls = _stub_search(monkeypatch, electronics, ["el-01"])
    run = await _run(monkeypatch, electronics, dark=True, tenants="sole-ro")
    assert not run.reached and calls == [] and run.interpret_rows == []


async def test_a_conversation_outside_the_canary_goes_dark(monkeypatch, electronics):
    """Canary 0%: flagul de servire aprins, dar conversația e în afara bucket-ului ⇒ dark."""
    _stub_search(monkeypatch, electronics, ["el-01"])
    run = await _run(monkeypatch, electronics, dark=True, serve=True, percent=0)
    assert run.reached and run.branch_result is False
    assert run.ctx.trace["kernel_dark"]["searched"] is True
