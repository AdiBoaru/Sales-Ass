"""Mini-scheduler intern pentru joburile de mentenanță (NX-83).

Un singur proces, buclă asyncio. NU dublează logica de job — cheamă funcțiile `run()`
existente din `src/jobs/*` la intervale fixe. Toate joburile rulează pe `admin_conn`
(mentenanță cross-tenant / DDL), exact ca în `__main__`-urile lor. Un job care crapă e
logat și sărit — NU oprește bucla (P6: degradare, nu cădere tăcută).

Decizie: mini-scheduler intern, NU pg_cron (rollup/embed rulează cod Python), NU cron de
sistem (fragil, în afara compose). Fără apscheduler/celery — `asyncio.sleep` e suficient
pentru 3-4 joburi periodice (precizia „după miezul nopții" e ok la ±câteva minute).

    python -m src.jobs.scheduler
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from src.config import get_settings
from src.db.connection import close_pool
from src.jobs import cleanup_dedupe

log = logging.getLogger(__name__)

# Healthcheck: schedulerul atinge periodic acest fișier; compose verifică vechimea lui.
HEARTBEAT = "/tmp/scheduler_alive"
# Cap pe somn: reverificăm heartbeat-ul des, chiar dacă următorul job e peste ore.
_MAX_SLEEP_S = 300.0


@dataclass
class Job:
    name: str
    run: Callable[[], Awaitable[object]]  # async () -> ... ; cheamă funcția existentă
    interval_seconds: int
    at_hour_utc: int | None = None  # opțional: ancorat la o oră UTC (rollup nocturn)
    next_run: float = 0.0  # epoch al următoarei rulări


# --------------------------------------------------------------------------- #
# Wrappers thin peste funcțiile existente (refolosim, NU rescriem logica)
# --------------------------------------------------------------------------- #


async def rollup_usage_run() -> None:
    from src.db.connection import admin_conn, get_pool
    from src.jobs import rollup_usage

    pool = await get_pool()
    async with admin_conn(pool) as conn:
        await rollup_usage.run_rollup(conn, day=rollup_usage.yesterday_utc())


async def embed_products_run() -> None:
    from src.agent.llm import get_llm
    from src.db.connection import admin_conn, get_pool
    from src.jobs import embed_products

    llm = get_llm()
    if llm is None:
        log.warning("embed_products sărit — OPENAI_API_KEY lipsește")
        return
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        await embed_products.embed_pending(conn, llm)


async def lifecycle_run() -> None:
    """Val3: reclasifică contacts.lifecycle (nocturn, admin). Un singur UPDATE determinist."""
    from src.db.connection import admin_conn, get_pool
    from src.jobs import lifecycle

    s = get_settings()
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        n = await lifecycle.run_lifecycle(conn, churn_days=s.lifecycle_churn_days)
    log.info("lifecycle: %d contacte reclasificate", n)


async def proactive_initiators_run() -> None:
    """PL-1: scanează sursele persistente și CREEAZĂ proactive_jobs (coș abandonat + back-in-stock).
    Control-plane (admin) + per-tenant (RLS) sunt în `run_initiators` — aici doar pool + log."""
    from src.db.connection import get_pool
    from src.proactive import initiators

    pool = await get_pool()
    counts = await initiators.run_initiators(pool)
    log.info("proactive initiators: %s", counts)


# --------------------------------------------------------------------------- #
# Bucla
# --------------------------------------------------------------------------- #


async def _safe_run(job: Job) -> None:
    """Rulează un job, prinde orice excepție (un job picat nu oprește restul — P6)."""
    start = time.monotonic()
    try:
        await job.run()
        log.info("job %s ok (%.1fs)", job.name, time.monotonic() - start)
    except Exception:  # noqa: BLE001 — degradare, nu cădere tăcută
        log.exception("job %s a eșuat — sărit, reîncerc la următorul interval", job.name)


def _compute_next(job: Job, now: datetime) -> float:
    """Următorul moment de rulare (epoch). Ancorat la oră UTC → azi/mâine la acea oră;
    altfel now + interval."""
    if job.at_hour_utc is not None:
        candidate = now.replace(hour=job.at_hour_utc, minute=10, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate.timestamp()
    return now.timestamp() + job.interval_seconds


def _build_jobs() -> list[Job]:
    s = get_settings()
    jobs = [
        Job(
            "rollup_usage",
            rollup_usage_run,
            interval_seconds=86400,
            at_hour_utc=s.scheduler_rollup_hour_utc,
        ),
        Job(
            "cleanup_dedupe",
            cleanup_dedupe.run,
            interval_seconds=s.scheduler_dedupe_interval_seconds,
        ),
    ]
    if s.embed_job_enabled and s.openai_api_key:  # embed cere cheie OpenAI
        jobs.append(
            Job(
                "embed_products",
                embed_products_run,
                interval_seconds=s.scheduler_embed_interval_seconds,
            )
        )
    if s.proactive_enabled and s.proactive_initiators_enabled:  # PL-1: hrănește motorul proactiv
        jobs.append(
            Job(
                "proactive_initiators",
                proactive_initiators_run,
                interval_seconds=s.proactive_initiators_interval_s,
            )
        )
    if s.lifecycle_job_enabled:  # Val3: reclasificare nocturnă a lifecycle
        jobs.append(
            Job(
                "lifecycle",
                lifecycle_run,
                interval_seconds=86400,
                at_hour_utc=s.lifecycle_hour_utc,
            )
        )
    return jobs


def _touch_heartbeat() -> None:
    try:
        with open(HEARTBEAT, "w") as f:
            f.write(str(int(time.time())))
    except OSError:  # heartbeat ne-scriibil nu trebuie să oprească bucla
        log.warning("nu pot scrie heartbeat %s", HEARTBEAT)


async def _run_due(jobs: list[Job], *, now: datetime | None = None) -> list[str]:
    """Rulează joburile scadente (next_run <= now), recalculează next_run. Întoarce numele
    celor rulate. Un job lent NU fură slotul celuilalt (fiecare cu next_run propriu)."""
    now = now or datetime.now(UTC)
    ran: list[str] = []
    for j in jobs:
        if j.next_run <= now.timestamp():
            await _safe_run(j)
            j.next_run = _compute_next(j, datetime.now(UTC))
            ran.append(j.name)
    return ran


async def _loop(jobs: list[Job], *, run_on_start: bool = True) -> None:
    now = datetime.now(UTC)
    for j in jobs:
        j.next_run = now.timestamp() if run_on_start else _compute_next(j, now)
    while True:
        await _run_due(jobs)
        _touch_heartbeat()
        sleep_for = max(5.0, min(j.next_run for j in jobs) - datetime.now(UTC).timestamp())
        await asyncio.sleep(min(sleep_for, _MAX_SLEEP_S))


async def _main() -> None:
    logging.basicConfig(level=logging.INFO)
    jobs = _build_jobs()
    log.info("scheduler pornit cu joburi: %s", [j.name for j in jobs])
    try:
        await _loop(jobs)
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(_main())
