"""NX-328 felia 1d — harnessul diferențial al invariantului I16.

I16: cu flagurile kernelului stinse, calea v1 e neschimbată pe suprafața observabilă. Harnessul
înregistrează suprafața suitei golden pe două versiuni de cod și le compară. Testele de aici
dovedesc trei lucruri, fără de care poarta din CI ar fi teatru:

1. e DETERMINISTĂ: același cod înregistrat de două ori dă diff gol;
2. POATE PICA: o schimbare de suprafață introdusă deliberat (un flag inversat) dă un diff care
   numește testul, turul și suprafața;
3. se aplică exact PR-urilor de kernel.

Zero model, zero DB: suita golden e ermetică (LLM scriptat, query-uri stub-uite)."""

from __future__ import annotations

import json

import pytest

from scripts import kernel_differential as kd
from scripts.kernel_diff import kernel_diff_plugin as plugin

# --- piese pure -----------------------------------------------------------------------------------


def _turn(**over):
    return {"reply": {"text": "ok"}, "tool_calls": [], "state": {}, "db": [], **over}


def test_diff_is_empty_on_identical_recordings():
    doc = {"t::a": [_turn()], "t::b": [_turn(), _turn()]}
    assert kd.diff(doc, json.loads(json.dumps(doc))) == []


def test_diff_names_the_test_the_turn_and_the_surface():
    base = {"t::a": [_turn(), _turn()]}
    head = {"t::a": [_turn(), _turn(tool_calls=[{"name": "search_products", "args": {}}])]}
    assert kd.diff(base, head) == ["t::a #1: suprafața `tool_calls` diferă"]


def test_diff_reports_missing_new_and_resized_tests():
    base = {"t::gone": [_turn()], "t::same": [_turn()]}
    head = {"t::new": [_turn()], "t::same": [_turn(), _turn()]}
    lines = kd.diff(base, head)
    assert any("t::gone" in line and "lipsește" in line for line in lines)
    assert any("t::new" in line and "nou" in line for line in lines)
    assert any("t::same" in line and "ture" in line for line in lines)


def test_plain_is_deterministic_and_drops_telemetry():
    value = {"b": {3, 1, 2}, "a": 1.23456789, "events": ["x"], "latency_ms": 12, "diagnostics": {}}
    assert plugin.plain(value) == {"a": 1.234568, "b": [1, 2, 3]}


def test_the_recording_connection_behaves_like_object_and_logs_access():
    log: list[str] = []
    token = plugin._db_access.set(log)
    try:
        conn = plugin.RecordingConn()
        with pytest.raises(AttributeError):
            conn.execute  # noqa: B018 — accesul e exact ce se măsoară
        assert log == ["execute"]
    finally:
        plugin._db_access.reset(token)


REGISTRY = {
    "roles": {
        "pure": {"modules": ["src/conversation/delta.py"]},
        "executor": {"modules": ["src/agent/deterministic.py"]},
    },
    "planned": {"src/agent/turn_planner.py": "planner"},
}


@pytest.mark.parametrize(
    ("changed", "applies"),
    [
        (["src/conversation/delta.py"], True),  # modul pur înscris
        (["src/agent/turn_planner.py"], True),  # modul planificat, creat în acest PR
        (["tests/kernel/gates.py"], True),  # infrastructura kernelului
        (["scripts/kernel_differential.py"], True),
        (["src/agent/deterministic.py"], False),  # executor = calea de azi (NX-326)
        (["src/worker/processor.py", "CLAUDE.md"], False),
        ([], False),
    ],
)
def test_i16_applies_exactly_to_kernel_prs(changed, applies):
    assert kd.touches_kernel(changed, REGISTRY) is applies


# --- harnessul real, pe suita golden --------------------------------------------------------------


@pytest.fixture(scope="module")
def recordings(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("kernel_diff")
    paths = {name: tmp / f"{name}.json" for name in ("a", "b", "flipped")}
    kd.record(kd.DEFAULT_ROOT, paths["a"])
    kd.record(kd.DEFAULT_ROOT, paths["b"])
    # `compare_intent_enabled` schimbă DOAR turul de comparație al unei conversații golden: un diff
    # îngust dovedește că poarta vede o schimbare mică, nu doar una care rupe totul.
    kd.record(kd.DEFAULT_ROOT, paths["flipped"], flip="compare_intent_enabled=false")
    return {name: json.loads(p.read_text(encoding="utf-8")) for name, p in paths.items()}


def test_the_recording_covers_the_golden_suite_with_real_surfaces(recordings):
    turns = [t for v in recordings["a"].values() for t in v]
    assert len(turns) >= 150
    assert sum(1 for t in turns if t["tool_calls"]) >= 50
    assert sum(1 for t in turns if (t["reply"] or {}).get("products")) >= 50
    assert sum(1 for t in turns if (t["state"] or {}).get("active_search")) >= 50


def test_the_same_code_recorded_twice_gives_an_empty_diff(recordings):
    assert kd.diff(recordings["a"], recordings["b"]) == []


def test_a_deliberate_surface_change_is_caught_and_named(recordings):
    lines = kd.diff(recordings["a"], recordings["flipped"])
    assert lines, "harnessul n-a văzut o schimbare de suprafață: poarta I16 ar fi oarbă"
    assert all("compare" in line for line in lines)
    assert any("`reply`" in line for line in lines)
