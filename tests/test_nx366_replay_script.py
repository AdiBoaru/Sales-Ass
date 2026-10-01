"""NX-366 — raportul de replay nu are voie să arate „reparat" sau „identic" când nu e.

Fiecare test vine dintr-o constatare a recenziei adversariale: detectorii rulați pe replay fără
datele de catalog ieșeau mereu „reparați", iar poarta de fidelitate ignora turele divergente.
"""

from __future__ import annotations

from argparse import Namespace

from scripts import trace_replay as script
from scripts import turn_defects as td
from src.evals.trace_replay import ReplayResult

HEAD = "a" * 40


def _entry(status="replayed", **kw):
    base = {"turn_id": "t", "status": status, "reason": None, "same_release": True}
    base.update(kw)
    return base


def test_diverged_shortened_and_written_turns_count_against_fidelity():
    others = [_entry("diverged"), _entry("wrote"), _entry("shortened")]
    results = [_entry() for _ in range(19)] + others
    f = script.fidelity(results, gate=True, head=HEAD)
    assert (f["judged"], f["same"]) == (22, 19)
    assert f["verdict"] == "FAIL"  # 19/22 < 95%


def test_a_changed_input_is_not_identical():
    results = [_entry() for _ in range(19)] + [_entry(inputs_changed=[0])]
    assert script.fidelity(results, gate=True, head=HEAD)["same"] == 19


def test_catalog_drift_and_unreplayable_turns_leave_the_denominator():
    results = [_entry(), _entry(catalog_drift=["p1"]), _entry("not_replayable")]
    f = script.fidelity(results, gate=True, head=HEAD)
    assert (f["judged"], f["same"], f["verdict"]) == (1, 1, "PASS")


def test_fidelity_on_another_release_is_not_a_verdict():
    results = [_entry(), _entry(same_release=False)]
    assert script.fidelity(results, gate=True, head=HEAD)["verdict"] == "WRONG_RELEASE"
    assert script.fidelity(results, gate=True, head=None)["verdict"] == "WRONG_RELEASE"


def test_out_of_stock_detector_can_fire_on_the_replayed_turn():
    """Fără disponibilitatea cardurilor rejucate, `oos_on_card` (P0) era mereu fals pe replay, deci
    orice tur înregistrat cu un epuizat ieșea „reparat"."""
    reply = {"products": [{"product_id": "p1", "name": "Crema"}]}
    recorded = td.Turn("t", "c", "2026-10-01", reply, {}, {}, availability={"p1": "out_of_stock"})
    res = ReplayResult(turn_id="t", status="replayed", reply=reply)
    replayed = script.replay_as_defect_turn(recorded, res, {"p1": ("out_of_stock", "crema")})
    was, now = script.detector_hits(recorded), script.detector_hits(replayed)
    assert "oos_on_card" in was and "oos_on_card" in now  # nu e „reparat"


def test_environment_detectors_never_count_as_fixed():
    assert "slow_turn" in script.ENVIRONMENT_DETECTORS
    slow = td.Turn("t", "c", "x", {}, {}, {"turn_latency": ({"e2e_ms": 60_000},)})
    assert "slow_turn" not in script.detector_hits(slow)


def test_export_requires_a_set_report():
    try:
        script.main(["--business", "sole-ro", "--conversation", "x", "--export", "d"])
    except SystemExit as exc:
        assert exc.code == 2
    else:  # pragma: no cover
        raise AssertionError("--export fără --set-report trebuia refuzat")


def test_summary_without_gate_has_no_verdict():
    args = Namespace(business="sole-ro", flags="recorded", fidelity=False)
    report = script.summarize([_entry(fixed=["oos_on_card"])], args, HEAD)
    assert report["fidelity"]["verdict"] is None
    assert report["fixed"] == {"oos_on_card": 1}
