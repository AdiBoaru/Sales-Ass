"""NX-83 — mini-scheduler joburi. ZERO DB/OpenAI: settings monkeypatch + joburi mock.

Testăm: `_compute_next` (ancorat la oră vs interval), `_safe_run` (prinde excepția,
nu propagă), `_build_jobs` (exclude embed fără cheie), `_run_due` (rulează exact
joburile scadente + recalculează next_run), embed wrapper sărit fără cheie.
"""

from datetime import UTC, datetime
from types import SimpleNamespace

from src.jobs import scheduler as sch
from src.jobs.scheduler import Job, _build_jobs, _compute_next, _run_due, _safe_run


def _settings(*, key="sk-x", embed=True):
    return SimpleNamespace(
        openai_api_key=key,
        embed_job_enabled=embed,
        scheduler_rollup_hour_utc=0,
        scheduler_dedupe_interval_seconds=21600,
        scheduler_embed_interval_seconds=3600,
    )


# --------------------------------------------------------------------------- #
# _compute_next
# --------------------------------------------------------------------------- #


def test_compute_next_interval():
    now = datetime(2026, 6, 17, 12, 0, 0, tzinfo=UTC)
    job = Job("x", None, interval_seconds=100)
    assert _compute_next(job, now) == now.timestamp() + 100


def test_compute_next_anchored_hour_today_if_future():
    now = datetime(2026, 6, 17, 10, 0, 0, tzinfo=UTC)
    job = Job("rollup", None, interval_seconds=86400, at_hour_utc=23)
    nxt = datetime.fromtimestamp(_compute_next(job, now), tz=UTC)
    assert nxt.date() == now.date()
    assert (nxt.hour, nxt.minute) == (23, 10)


def test_compute_next_anchored_hour_tomorrow_if_past():
    now = datetime(2026, 6, 17, 23, 55, 0, tzinfo=UTC)
    job = Job("rollup", None, interval_seconds=86400, at_hour_utc=0)
    nxt = datetime.fromtimestamp(_compute_next(job, now), tz=UTC)
    assert nxt.day == 18  # azi 00:10 deja trecut → mâine
    assert (nxt.hour, nxt.minute) == (0, 10)


# --------------------------------------------------------------------------- #
# _safe_run — un job picat nu propagă (P6)
# --------------------------------------------------------------------------- #


async def test_safe_run_swallows_exception():
    async def boom():
        raise RuntimeError("job a crăpat")

    await _safe_run(Job("boom", boom, interval_seconds=10))  # NU propagă


async def test_safe_run_runs_ok():
    ran = []

    async def ok():
        ran.append(1)

    await _safe_run(Job("ok", ok, interval_seconds=10))
    assert ran == [1]


# --------------------------------------------------------------------------- #
# _build_jobs — embed gated pe cheie OpenAI
# --------------------------------------------------------------------------- #


def test_build_jobs_includes_embed_with_key(monkeypatch):
    monkeypatch.setattr(sch, "get_settings", lambda: _settings(key="sk-x", embed=True))
    assert [j.name for j in _build_jobs()] == ["rollup_usage", "cleanup_dedupe", "embed_products"]


def test_build_jobs_excludes_embed_without_key(monkeypatch):
    monkeypatch.setattr(sch, "get_settings", lambda: _settings(key="", embed=True))
    names = [j.name for j in _build_jobs()]
    assert names == ["rollup_usage", "cleanup_dedupe"]  # rollup + cleanup chiar fără cheie


def test_build_jobs_excludes_embed_when_disabled(monkeypatch):
    monkeypatch.setattr(sch, "get_settings", lambda: _settings(key="sk-x", embed=False))
    assert "embed_products" not in [j.name for j in _build_jobs()]


# --------------------------------------------------------------------------- #
# _run_due — rulează exact joburile scadente + recalculează next_run
# --------------------------------------------------------------------------- #


async def test_run_due_runs_only_due_and_recomputes():
    calls = []

    async def f():
        calls.append(1)

    future_ts = datetime.now(UTC).timestamp() + 9999
    due = Job("due", f, interval_seconds=100, next_run=0.0)
    future = Job("future", f, interval_seconds=100, next_run=future_ts)
    ran = await _run_due([due, future])
    assert ran == ["due"]
    assert len(calls) == 1
    assert due.next_run > datetime.now(UTC).timestamp()  # recalculat în viitor
    assert future.next_run == future_ts  # neatins


async def test_run_due_two_due_both_run():
    calls = []

    async def f():
        calls.append(1)

    jobs = [Job("a", f, 100, next_run=0.0), Job("b", f, 100, next_run=0.0)]
    ran = await _run_due(jobs)
    assert ran == ["a", "b"]
    assert len(calls) == 2


# --------------------------------------------------------------------------- #
# embed_products_run — sărit curat fără cheie (get_llm → None)
# --------------------------------------------------------------------------- #


async def test_embed_run_skips_without_llm(monkeypatch):
    import src.agent.llm as llm_mod

    monkeypatch.setattr(llm_mod, "get_llm", lambda: None)
    await sch.embed_products_run()  # iese curat, fără să atingă DB
