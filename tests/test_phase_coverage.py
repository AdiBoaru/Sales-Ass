"""NX-300 — fiecare bucată de timp a turului aparține unei FAZE, în profilul PRODUCȚIEI.

(vezi `tasks/stage1/NX-300.md` pentru măsurătoarea care a produs cardul)

Contextul, măsurat pe trafic real înainte de card (`sole-ro`, 53 de ture, 30 de zile): din cele
zece faze din `deadline.PHASES`, cinci nu apăreau NICIODATĂ, iar `phase_ms_total / e2e_ms` avea
p50 46,7% și minim 6,5%. Pe turul `3e582c6d` (95.153 ms) evenimentul raporta 6.157 ms în faze.

Testele de aici sunt scrise cu flagurile NX-241 **STINSE**, deliberat. Cu `.env`-ul de dezvoltare
(`TURN_DEADLINE_ENABLED=true`) una dintre cauze dispare de la sine, testele trec și pare reparat —
exact tiparul „CI verde pe profilul greșit" din care s-a născut cardul.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from src.observability import turn_latency
from src.runtime.deadline import PHASES

# ── Poarta pe FORMĂ: niciun apel de model fără fază ────────────────────────────────────────────


def test_every_chat_call_is_wrapped_in_a_model_span() -> None:
    """Derivarea e MECANICĂ, nu o listă întreținută de mână.

    Înainte de NX-300, `llm.py` avea șapte apelanți ai lui `_chat`; patru erau înveliți (ambele
    bucle de tool-calling) și trei nu — `classify_json`, `complete` și `complete_schema`. Ultimul
    e cel care compune răspunsul bogat: 86,7 secunde dintr-un tur de 95,2, invizibile.

    Un al optulea apelant adăugat mâine ar arăta exact ca primul: corect, util și nemăsurat.
    """
    tree = ast.parse(Path("src/agent/llm.py").read_text(encoding="utf-8"))

    spanned: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.With):
            continue
        if not any(_is_model_span(item.context_expr) for item in node.items):
            continue
        for inner in ast.walk(node):
            spanned.add(id(inner))

    offenders = [
        f"llm.py:{node.lineno}"
        for node in ast.walk(tree)
        if _is_chat_call(node) and id(node) not in spanned
    ]
    assert not offenders, (
        "apel de model fără `turn_latency.span('model')` — timpul lui nu apare în nicio fază: "
        + ", ".join(offenders)
    )


def _is_chat_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_chat"
    )


def _is_model_span(expr: ast.AST) -> bool:
    return (
        isinstance(expr, ast.Call)
        and isinstance(expr.func, ast.Attribute)
        and expr.func.attr == "span"
        and bool(expr.args)
        and isinstance(expr.args[0], ast.Constant)
        and expr.args[0].value == "model"
    )


def test_the_tools_phase_is_not_behind_a_budget_flag() -> None:
    """A doua sub-cauză, ca poartă pe formă.

    Span-ul `tools` exista din NX-241, dar stătea pe ramura de după `if ledger is None and d is
    None: return ...` — adică pe ramura pe care producția (flaguri stinse) NU o ia. Rezultat: 0
    ture din 53 aveau faza. Acum trăiește în `_execute_serialized`, punctul comun al ambelor
    ramuri; testul refuză întoarcerea lui în `execute`.
    """
    tree = ast.parse(Path("src/agent/tool_executor.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "execute":
            offenders = [
                f"tool_executor.py:{inner.lineno}"
                for inner in ast.walk(node)
                if isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Attribute)
                and inner.func.attr == "span"
                and inner.args
                and isinstance(inner.args[0], ast.Constant)
                and inner.args[0].value == "tools"
            ]
            assert not offenders, (
                "faza `tools` s-a întors în `execute`, unde ramura fără buget o ocolește: "
                + ", ".join(offenders)
            )
            return
    pytest.fail("`ToolRun.execute` n-a mai fost găsit — testul măsoară altceva decât crede")


# ── Producători: fiecare fază din vocabular are unul, sau e declarată fără ─────────────────────

#: Fazele pentru care NX-300 a adăugat sau reparat un producător, cu locul lui.
PRODUCERS: dict[str, str] = {
    "queue": "src/worker/processor.py",  # `turn_latency.record("queue", ...)` la admission
    "load": "src/worker/turn_uow.py",
    "gates": "src/worker/runner.py",  # `_PHASE_BY_STAGE`, prin `record`
    "model": "src/agent/llm.py",
    "tools": "src/agent/tool_executor.py",
    "validation": "src/agent/validator.py",
    "commit": "src/worker/processor.py",
    "aftercare": "src/worker/aftercare.py",
    "retrieval": "src/agent/brain.py",
    "projection": "src/web/turn_events.py",
}


def test_every_declared_phase_has_a_producer_in_src() -> None:
    """Vocabularul e ÎNCHIS, deci trebuie să fie și COMPLET: o etichetă fără producător e o
    promisiune pe care instrumentul n-o ține. Înainte, `queue`, `commit` și `aftercare` erau
    declarate în `PHASES` și aveau chiar și nume de span în taxonomia NX-246, dar nimic din `src/`
    nu le emitea vreodată."""
    assert set(PRODUCERS) == set(PHASES), (
        "vocabularul de faze s-a schimbat fără ca producătorul să fie declarat aici: "
        f"lipsă={sorted(set(PHASES) - set(PRODUCERS))}, "
        f"în plus={sorted(set(PRODUCERS) - set(PHASES))}"
    )
    missing = []
    for phase, module in PRODUCERS.items():
        source = Path(module).read_text(encoding="utf-8")
        if f'"{phase}"' not in source:
            missing.append(f"{phase} → {module}")
    assert not missing, "fază fără producător la locul declarat: " + ", ".join(missing)


# ── Acoperirea, ca proprietate a acumulatorului ───────────────────────────────────────────────


def test_coverage_is_published_as_a_number() -> None:
    """`phase_ms_total` și `e2e_ms` existau amândouă; raportul lor, nu. Cifra e publicată acum ca
    `phase_coverage_pct`, fiindcă o regresie de instrument trebuie să se vadă ca o regresie, nu să
    ceară cuiva să împartă două câmpuri din proprie inițiativă."""
    from types import SimpleNamespace

    from src.models import BusinessConfig, Contact, InboundMessage, TurnContext
    from src.worker import runner

    acc = turn_latency.TurnLatencyAccumulator()
    acc.record("model", 800.0)
    acc.record("tools", 150.0)
    ctx = TurnContext(
        turn_id="t",
        business=BusinessConfig(id="b", slug="s", name="N"),
        contact=Contact(id="c", business_id="b"),
        message=InboundMessage(provider_msg_id="m", body="salut"),
        conversation_id="conv",
    )
    runner.emit_turn_latency(ctx, SimpleNamespace(latency=acc, ledger=None, deadline=None), 1000.0)

    event = next(e for e in ctx.events if e.type == "turn_latency")
    assert event.properties["phase_ms_total"] == 950.0
    assert event.properties["phase_coverage_pct"] == 95.0


async def test_a_whole_turn_attributes_queue_load_and_commit(monkeypatch) -> None:
    """Testul COMPORTAMENTAL, pe un tur întreg prin `handle_turn`, cu flagurile NX-241 stinse.

    Cele trei faze verificate aici sunt exact cele care cădeau în afara pipeline-ului: `queue` și
    `load` se întâmplă ÎNAINTE, `commit` DUPĂ. Evenimentul se emitea la sfârșitul lui
    `run_pipeline`, deci niciuna n-avea unde să apară — și, măsurat, niciuna nu apărea.
    """
    from src.db.provider import static_db
    from src.models import BusinessConfig
    from src.worker import aftercare as ac
    from src.worker import processor as proc
    from src.worker import turn_uow as uow
    from src.worker.processor import handle_turn

    settings = __import__("src.config", fromlist=["get_settings"]).get_settings()
    for flag in ("turn_deadline_enabled", "turn_budget_enforced"):
        monkeypatch.setattr(settings, flag, False, raising=False)
    monkeypatch.setattr(settings, "turn_latency_spans_enabled", True, raising=False)

    class _FakeTx:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *a):
            return False

    class _FakeConn:
        def transaction(self):
            return _FakeTx()

    async def anoop(*a, **k):
        return None

    async def fake_conv(*a, **k):
        return {"id": "conv", "state": {}, "state_version": 0, "locale": "ro", "bot_active": True}

    seen: dict = {}

    async def capture_stage(ctx, deps):
        seen["ctx"] = ctx
        ctx.set_reply("Bună, uite ce am găsit.")

    monkeypatch.setattr(uow, "claim_inbound", lambda *a, **k: _true())
    monkeypatch.setattr(uow, "mark_inbound_completed", anoop)
    monkeypatch.setattr(uow, "get_or_create_contact", lambda *a, **k: _contact())
    monkeypatch.setattr(uow, "get_or_create_conversation", fake_conv)
    monkeypatch.setattr(uow, "insert_message", lambda *a, **k: _msg_id())
    monkeypatch.setattr(uow, "touch_last_inbound", anoop)
    monkeypatch.setattr(uow, "get_recent_messages", anoop)
    monkeypatch.setattr(uow, "get_summary_for_context", anoop)
    monkeypatch.setattr(uow, "enqueue_outbox", lambda *a, **k: _msg_id())
    monkeypatch.setattr(uow, "patch_conversation_state", anoop)
    monkeypatch.setattr(proc, "persist_events", anoop)
    monkeypatch.setattr(proc, "_record_turn_cost", anoop)
    monkeypatch.setattr(proc, "_llm_within_budget", anoop)
    monkeypatch.setattr(ac, "_cache_writeback", anoop)
    monkeypatch.setattr(ac, "_summarize_if_needed", anoop)
    monkeypatch.setattr(ac, "_extract_profile_and_score", anoop)

    await handle_turn(
        static_db(_FakeConn()),
        BusinessConfig(id="biz-1", slug="s", name="n"),
        "chan-1",
        {
            "channel_kind": "webchat",
            "sender_external_id": "u1",
            "provider_msg_id": "m1",
            "content_type": "text",
            "body": "vreau ceva sa scap de cosuri",
            "admission_wait_ms": 12.5,  # NX-300: cifra exista deja; acum devine și FAZĂ
        },
        stages=[capture_stage],
    )

    events = [e for e in seen["ctx"].events if e.type == "turn_latency"]
    assert len(events) == 1, "un tur = UN eveniment de latență (ca `llm_usage`)"
    phases = events[0].properties["phases"]
    # Doar cele trei din AFARA pipeline-ului: `gates` vine din `_PHASE_BY_STAGE`, deci cere un
    # stagiu cu nume real — n-are legătură cu defectul pe care testul îl apără.
    for phase in ("queue", "load", "commit"):
        assert phase in phases, (
            f"faza `{phase}` lipsește din turul complet — se întâmplă în afara pipeline-ului, "
            f"iar evenimentul o rata. Prezente: {sorted(phases)}"
        )
    assert phases["queue"]["ms"] == pytest.approx(12.5)


async def _true():
    return True


async def _contact():
    from src.models import Contact as _C

    return _C(id="c", business_id="biz-1")


async def _msg_id():
    return "id-1"


def test_an_unknown_phase_is_counted_not_invented() -> None:
    """Perechea: acoperirea nu se poate umfla inventând o etichetă. Vocabularul respinge, și
    respingerea se NUMĂRĂ — un bug de instrumentare trebuie să fie vizibil (P12)."""
    acc = turn_latency.TurnLatencyAccumulator()
    acc.record("rerank", 500.0)  # nu e în PHASES
    assert acc.total_ms == 0.0
    assert acc.as_event_props()["unknown_phases"] == 1
