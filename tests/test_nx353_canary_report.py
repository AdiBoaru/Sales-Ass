"""NX-353 — raportul dark/canary și regula GO pre-înregistrată, pe date sintetice (zero DB, zero
model). `load` rulează pe o conexiune falsă: verifică tenantul în fiecare query și hidratarea
într-un singur query."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest

from scripts import kernel_canary_report as rep

CARDS = {
    "p1": rep.Card("ser", {"concerns": ["acne"]}),
    "p2": rep.Card("ser", {"concerns": ["hydration"]}),
    "p3": rep.Card("crema", {"concerns": ["acne", "hydration"]}),
    "p4": rep.Card("sampon", {}),
}


def _kt(turn, day, reason="dark", served=False, mode="dark"):
    props = {"served": served, "fallback_reason": reason}
    if mode:
        props["mode"] = mode
    return rep.Event(turn, "kernel_turn", props, day)


def _dark(turn, kernel_ids, v1_ids, *, types=("ser",), needs=(), searched=True, learned=False):
    record = {
        "executor": "search",
        "searched": searched,
        "kernel_ids": list(kernel_ids),
        "truth": {"types": list(types), "type_learned": learned, "needs": [list(n) for n in needs]},
    }
    return rep.DarkTurn(turn, "2026-10-01", record, tuple(v1_ids))


def _latency(*ms):
    rows = [{"purpose": "interpret", "ms": m} for m in ms] + [{"purpose": None, "ms": 99999}]
    return rep.Event("t", "llm_usage", {"per_call": rows}, "2026-10-01")


def _healthy(n=60, days=5):
    events = [_kt(f"t{i}", f"2026-10-0{1 + i % days}") for i in range(n)]
    events.append(_latency(1500, 1800, 2000))
    need = [("concerns", "acne")]
    dark = [_dark(f"t{i}", ["p1", "p3"], ["p1", "p4"], needs=need) for i in range(10)]
    return events, dark


# --- metricile ------------------------------------------------------------------------------------


def test_percentile_interpolates_and_is_none_on_nothing():
    assert rep.percentile([], 0.9) is None
    assert rep.percentile([1, 2, 3, 4, 5], 0.5) == 3
    assert rep.percentile([0, 10], 0.9) == pytest.approx(9.0)


def test_subject_share_counts_the_umbrella_codes():
    cards = [CARDS["p1"], CARDS["p3"], CARDS["p4"]]
    assert rep.subject_share(["ser", "crema"], cards) == pytest.approx(2 / 3)
    assert rep.subject_share([], cards) is None
    assert rep.subject_share(["ser"], []) == 0.0


def test_need_share_is_the_mean_over_needs():
    cards = [CARDS["p1"], CARDS["p2"]]
    assert rep.need_share([["concerns", "acne"], ["concerns", "hydration"]], cards) == 0.5
    assert rep.need_share([], cards) is None
    assert rep.need_share([["concerns", "acne"]], []) == 0.0


def test_only_interpret_calls_count_for_latency():
    assert rep.interpret_latencies([_latency(100, 200)]) == [100.0, 200.0]


def test_the_manual_sample_is_reproducible_and_bounded():
    ids = [f"t{i}" for i in range(100)]
    first = rep.sample_turns(ids)
    assert first == rep.sample_turns(list(reversed(ids)))
    assert len(first) == rep.MANUAL_SAMPLE and len(set(first)) == rep.MANUAL_SAMPLE
    assert rep.sample_turns(["a", "b"]) == ["a", "b"]


# --- regula GO ------------------------------------------------------------------------------------


def test_a_healthy_window_waits_for_the_manual_review():
    events, dark = _healthy()
    report = rep.summarize(events, dark, CARDS)
    assert report["verdict"] == "PENDING-MANUAL", report["failed"]
    assert report["gates"]["G3_subject"]["kernel"] == 0.5
    assert report["gates"]["G3_subject"]["v1"] == 0.5
    assert report["gates"]["G4_needs"]["kernel"] == 1.0
    assert len(report["G6_manual_sample"]) == 10


def test_an_unmeasured_gate_is_insufficient_not_go():
    events, _ = _healthy()
    dark = [_dark(f"t{i}", ["p1"], ["p1"]) for i in range(10)]  # nicio nevoie activă
    report = rep.summarize(events, dark, CARDS)
    assert report["unmeasured"] == ["G4_needs"] and report["verdict"] == "INSUFFICIENT"


def test_too_few_turns_or_days_is_insufficient_not_go():
    events, dark = _healthy(n=59)
    assert rep.summarize(events, dark, CARDS)["verdict"] == "INSUFFICIENT"
    events, dark = _healthy(n=80, days=4)
    assert rep.summarize(events, dark, CARDS)["verdict"] == "INSUFFICIENT"


def test_g1_counts_only_error_fallbacks():
    events, dark = _healthy(n=100)
    events += [_kt(f"e{i}", "2026-10-01", "provider_error") for i in range(5)]
    report = rep.summarize(events, dark, CARDS)
    assert report["gates"]["G1_errors"]["errors"] == 5 and report["gates"]["G1_errors"]["passed"]
    events += [_kt("e9", "2026-10-01", "invalid_json")]
    assert rep.summarize(events, dark, CARDS)["gates"]["G1_errors"]["passed"] is False


def test_g2_counts_an_empty_or_failed_kernel_search_where_v1_served():
    events, dark = _healthy()
    dark += [_dark(f"x{i}", [], ["p1"]) for i in range(2)]
    assert rep.summarize(events, dark, CARDS)["gates"]["G2_empty"]["passed"] is True
    dark += [_dark("x9", ["p1"], ["p1"], searched=False)]  # căutarea dark a picat
    report = rep.summarize(events, dark, CARDS)
    assert report["gates"]["G2_empty"]["empty"] == 3
    assert report["verdict"] == "NO-GO" and "G2_empty" in report["failed"]


def test_turns_without_a_v1_set_or_a_search_plan_are_not_compared():
    events, dark = _healthy()
    dark += [_dark("n1", [], [])]
    dark += [rep.DarkTurn("n2", "2026-10-01", {"executor": "detail", "searched": False}, ("p1",))]
    report = rep.summarize(events, dark, CARDS)
    assert "n1" not in report["compared_turns"] and "n2" not in report["compared_turns"]
    assert report["gates"]["G2_empty"]["empty"] == 0


def test_a_type_learned_from_v1s_screen_is_not_truth():
    events, _ = _healthy()
    dark = [_dark(f"t{i}", ["p4"], ["p1"], learned=True) for i in range(10)]
    assert rep.summarize(events, dark, CARDS)["gates"]["G3_subject"]["n"] == 0


def test_g3_fails_when_the_kernel_serves_another_kind():
    events, _ = _healthy()
    dark = [_dark(f"t{i}", ["p4"], ["p1", "p2"]) for i in range(10)]
    report = rep.summarize(events, dark, CARDS)
    assert report["gates"]["G3_subject"]["passed"] is False and report["verdict"] == "NO-GO"


def test_g4_compares_the_kernels_active_needs():
    events, _ = _healthy()
    need = [("concerns", "acne")]
    dark = [_dark(f"t{i}", ["p2"], ["p1", "p3"], needs=need) for i in range(10)]
    report = rep.summarize(events, dark, CARDS)
    assert report["gates"]["G4_needs"]["kernel"] == 0.0 and report["gates"]["G4_needs"]["v1"] == 1.0
    assert "G4_needs" in report["failed"]


def test_g5_fails_on_a_slow_interpretation():
    events, dark = _healthy()
    events.append(_latency(*([5000] * 10)))
    assert rep.summarize(events, dark, CARDS)["gates"]["G5_latency"]["passed"] is False


def test_served_turns_are_counted_apart_and_shadow_fields_aggregated():
    events, dark = _healthy()
    events.append(_kt("s1", "2026-10-01", None, served=True, mode=None))
    events.append(_kt("s2", "2026-10-01", "executor_refused", mode=None))
    events.append(
        rep.Event(
            "t1",
            "conversation_state_shadow_diff",
            {"fields": ["needs", "topic"], "differs": True},
            "2026-10-01",
        )
    )
    events.append(
        rep.Event("t2", "conversation_state_shadow_diff", {"fields": [], "differs": False}, "d")
    )
    report = rep.summarize(events, dark, CARDS)
    assert report["volume"]["served"] == 1 and report["volume"]["dark"] == 60
    assert report["state_shadow"] == {
        "turns": 2,
        "differs": 1,
        "fields": {"needs": 1, "topic": 1},
    }


def test_serve_turns_do_not_pad_the_dark_population():
    """Recenzia (F4): fallback-urile turelor servite de canary nu contează la volum, zile, G1."""
    events, dark = _healthy(n=59)
    events += [_kt(f"s{i}", "2026-10-09", "executor_refused", mode=None) for i in range(30)]
    report = rep.summarize(events, dark, CARDS)
    assert report["volume"]["dark"] == 59 and report["volume"]["days"] == 5
    assert report["verdict"] == "INSUFFICIENT"


def test_a_turn_counts_once():
    events, dark = _healthy(n=60)
    events += [_kt("t0", "2026-10-01", "provider_error")]  # același tur, a doua oară
    dark += [_dark("t0", [], ["p1"])]
    report = rep.summarize(events, dark, CARDS)
    assert report["volume"]["dark"] == 60 and report["gates"]["G1_errors"]["errors"] == 0
    assert report["gates"]["G2_empty"]["empty"] == 0


def test_a_failing_dark_search_counts_as_an_error_in_g1():
    """Recenzia (F3): turul rămâne `fallback_reason: dark`; eroarea vine din `kernel_dark`."""
    events, dark = _healthy(n=60)
    events += [
        rep.Event(f"t{i}", "kernel_dark", {"error": "RuntimeError"}, "2026-10-01") for i in range(4)
    ]
    assert rep.summarize(events, dark, CARDS)["gates"]["G1_errors"]["errors"] == 4
    events.append(rep.Event("t5", "kernel_dark", {"error": "RuntimeError"}, "2026-10-01"))
    assert rep.summarize(events, dark, CARDS)["gates"]["G1_errors"]["passed"] is False


def test_a_dark_timeout_is_an_error_and_a_censored_latency():
    events, dark = _healthy(n=100)
    events += [_kt(f"x{i}", "2026-10-01", "dark_timeout") for i in range(3)]
    report = rep.summarize(events, dark, CARDS)
    assert report["gates"]["G1_errors"]["errors"] == 3
    # 3 apeluri reale rapide + 3 tăiate: p90 cade peste prag, nu sub
    assert report["gates"]["G5_latency"]["calls"] == 6
    assert report["gates"]["G5_latency"]["passed"] is False


def test_unmapped_words_do_not_dilute_g4():
    """Recenzia (F2): o pereche `unmapped` ar fi (0, 0) pe ambele părți și ar trage marja."""
    assert rep.need_share([["unmapped", "x"]], [CARDS["p1"]]) is None
    both = [["concerns", "acne"], ["unmapped", "x"]]
    assert rep.need_share(both, [CARDS["p1"], CARDS["p2"]]) == 0.5


def test_the_console_carries_no_customer_text():
    events, dark = _healthy()
    text = rep.render_console(rep.summarize(events, dark, CARDS))
    assert "verdict: PENDING-MANUAL" in text and "t0" in text


# --- citirea: tenantul pe fiecare query ----------------------------------------------------------


class _Conn:
    def __init__(self, log):
        self.log = log

    async def fetchval(self, sql, *args):
        self.log.append(("val", sql, args))
        return "b-1"

    async def fetch(self, sql, *args):
        self.log.append(("rows", sql, args))
        if "analytics_events" in sql:
            return [
                {
                    "turn_id": "t1",
                    "event_type": "kernel_turn",
                    "properties": '{"served": false, "fallback_reason": "dark"}',
                    "created_at": datetime(2026, 10, 1, tzinfo=UTC),
                }
            ]
        if "conversation_traces" in sql:
            return [
                {
                    "turn_id": "t1",
                    "created_at": datetime(2026, 10, 1, tzinfo=UTC),
                    "recommended": '[{"product_id": "p1"}]',
                    "dark": {"executor": "search", "searched": True, "kernel_ids": ["p3"]},
                }
            ]
        return [
            {"id": "p1", "attributes": '{"product_type": "ser"}'},
            {"id": "p3", "attributes": {}},
        ]


async def test_load_scopes_every_query_to_the_tenant():
    admin_log, tenant_log = [], []

    @asynccontextmanager
    async def admin():
        yield _Conn(admin_log)

    tenants = []

    @asynccontextmanager
    async def tenant(business_id):
        tenants.append(business_id)
        yield _Conn(tenant_log)

    since, until = datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 10, 8, tzinfo=UTC)
    bid, events, dark, cards = await rep.load("sole-ro", since, until, admin=admin, tenant=tenant)
    assert bid == "b-1" and set(tenants) == {"b-1"}
    # evenimentele pe operator (append-only pentru `bot_runtime`), cu `business_id` explicit
    (admin_rows,) = [e for e in admin_log if e[0] == "rows"]
    assert "analytics_events" in admin_rows[1]
    tenant_rows = [e for e in tenant_log if e[0] == "rows"]
    assert len(tenant_rows) == 2, "traceurile și O hidratare"
    for _, sql, args in [admin_rows, *tenant_rows]:
        assert args[0] == "b-1" and "business_id = $1" in sql
    assert events[0].properties["fallback_reason"] == "dark" and events[0].day == "2026-10-01"
    assert dark[0].v1_ids == ("p1",) and dark[0].record["kernel_ids"] == ["p3"]
    assert cards["p1"].product_type == "ser" and cards["p3"].product_type is None
    assert sorted(tenant_rows[1][2][1]) == ["p1", "p3"]


# --- NX-356: fereastra care cuprinde deploy-ul lui interpret.v4.1 -------------------------------


def test_prompt_version_filter_keeps_only_that_versions_turns() -> None:
    """NX-356 a schimbat vederea interpretării (replica botului netăiată) și versiunea. Fără filtru,
    o fereastră care cuprinde deploy-ul amesteca două măsurători în G1-G5."""
    events = [
        rep.Event("old", "turn_interpretation", {"prompt_version": "interpret.v4"}, "2026-09-29"),
        rep.Event("old", "kernel_turn", {"mode": "dark"}, "2026-09-29"),
        rep.Event("new", "turn_interpretation", {"prompt_version": "interpret.v4.1"}, "2026-09-30"),
        rep.Event("new", "kernel_turn", {"mode": "dark"}, "2026-09-30"),
    ]
    dark = [
        rep.DarkTurn("old", "2026-09-29", {"executor": "search"}, ()),
        rep.DarkTurn("new", "2026-09-30", {"executor": "search"}, ()),
    ]
    assert rep.prompt_versions(events) == {"interpret.v4": 1, "interpret.v4.1": 1}
    kept_events, kept_dark = rep.only_prompt_version(events, dark, "interpret.v4.1")
    assert {e.turn_id for e in kept_events} == {"new"}
    assert [t.turn_id for t in kept_dark] == ["new"]
