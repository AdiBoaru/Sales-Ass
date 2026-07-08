"""NX-145 felia 3 — smoke test pentru harness-ul de regresie (`scripts/eval_regression.py`).

Rulează TOATE cazurile golden (single + multi-tur) prin scriptul de regresie și verifică
că snapshot-ul e verde. Rolul lui: dacă stub-urile scriptului divergă de pipeline (ex. un
refactor mută o funcție de catalog), gate-ul CI prinde asta — scriptul nu are voie să
„putrezească" tăcut, altfel diff-ul de regresie ar deveni nefiabil. Zero OpenAI/DB real.
"""

import asyncio

from scripts.eval_regression import _diff, _run_all


def test_eval_regression_snapshot_all_green():
    snapshot = asyncio.run(_run_all())
    red = {k: v["failures"] for k, v in snapshot.items() if not v["passed"]}
    assert not red, f"cazuri roșii în harness-ul de regresie: {red}"
    assert len(snapshot) >= 30, f"prea puține intrări în snapshot: {len(snapshot)}"


def test_eval_regression_diff_detects_route_change():
    """DIFF-ul semnalează o schimbare de rută/tool-uri (semnalul de regresie de comportament)."""
    baseline = {"c1": {"route": "sales", "tools": ["search_products"], "passed": True}}
    current = {"c1": {"route": "order", "tools": [], "passed": True}}
    diff = _diff(baseline, current)
    assert any("route" in line and "c1" in line for line in diff)


def test_eval_regression_diff_empty_when_identical():
    snap = {"c1": {"route": "sales", "tools": ["search_products"], "passed": True}}
    assert _diff(snap, dict(snap)) == []
