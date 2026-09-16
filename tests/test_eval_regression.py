"""NX-145 felia 3 — smoke test pentru harness-ul de regresie (`scripts/eval_regression.py`).

Rulează TOATE cazurile golden (single + multi-tur) prin scriptul de regresie și verifică
că snapshot-ul e verde. Rolul lui: dacă stub-urile scriptului divergă de pipeline (ex. un
refactor mută o funcție de catalog), gate-ul CI prinde asta — scriptul nu are voie să
„putrezească" tăcut, altfel diff-ul de regresie ar deveni nefiabil. Zero OpenAI/DB real.
"""

import asyncio

from scripts.eval_regression import KNOWN_BRAIN_DIVERGENCES, _diff, _run_all


def test_eval_regression_snapshot_all_green():
    """Verde pe v1, iar pe `brain` EXACT divergențele declarate — nici mai multe, nici mai puține.

    Egalitate, nu incluziune: o divergență nouă pică gate-ul (regresie), dar și una reparată îl
    pică (ca `xfail(strict=True)`), ca lista să nu poată rămâne în urmă și să treacă drept
    „acoperire". Golurile de harness (`gap`) au `passed is None` — „n-am măsurat" nu e „a picat".
    """
    snapshot = asyncio.run(_run_all())
    red = {k: v["failures"] for k, v in snapshot.items() if v["passed"] is False}
    assert set(red) == set(KNOWN_BRAIN_DIVERGENCES), (
        f"roșii NOI: {sorted(set(red) - set(KNOWN_BRAIN_DIVERGENCES))}; "
        f"divergențe REPARATE (scoate-le din listă): "
        f"{sorted(set(KNOWN_BRAIN_DIVERGENCES) - set(red))}"
    )
    assert len(snapshot) >= 30, f"prea puține intrări în snapshot: {len(snapshot)}"


def test_every_declared_divergence_names_a_cause():
    """O listă de excepții fără motive devine, în două luni, o listă de cazuri ignorate."""
    for key, reason in KNOWN_BRAIN_DIVERGENCES.items():
        assert key.endswith("@brain"), f"{key}: divergențele declarate sunt ale căii brain"
        assert len(reason) > 40, f"{key}: motivul e prea scurt ca să fie un motiv"


def test_eval_regression_diff_detects_route_change():
    """DIFF-ul semnalează o schimbare de rută/tool-uri (semnalul de regresie de comportament)."""
    baseline = {"c1": {"route": "sales", "tools": ["search_products"], "passed": True}}
    current = {"c1": {"route": "order", "tools": [], "passed": True}}
    diff = _diff(baseline, current)
    assert any("route" in line and "c1" in line for line in diff)


def test_eval_regression_diff_empty_when_identical():
    snap = {"c1": {"route": "sales", "tools": ["search_products"], "passed": True}}
    assert _diff(snap, dict(snap)) == []
