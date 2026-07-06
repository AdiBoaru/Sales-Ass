"""Motorul proactiv (NX-70) — scheduler loop peste `proactive_jobs`.

La interval, revendică joburile scadente, apelează poarta de gating (NX-71) și scrie
mesajul în **outbox** (singurul punct de ieșire, P5) — NU trimite direct la canal.
Jobul devine `sent` (enqueue reușit) / `skipped_*` / `cancelled` / `failed`, ATOMIC cu
`enqueue_outbox` în aceeași (sub)tranzacție.

Arhitectură (ca dispatcher-ul): control plane (admin_conn) → ce tenanți au joburi
scadente → per tenant (tenant_conn, RLS) → claim (`FOR UPDATE SKIP LOCKED`) → procesare.

Emite `type=text` (în fereastra 24h) SAU `type=template` (în afara ei, PL-1): poarta NX-71
decide care, motorul pune payload-ul în outbox → dispatcher-ul rutează după `payload.type`
(template → canalul cu capabilitatea TEMPLATE; canalele fără ea degradează grațios la text).

    python -m src.proactive.scheduler
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from src.config import get_settings
from src.db.connection import admin_conn, close_pool, get_bot_pool, get_pool, tenant_conn
from src.db.queries.analytics import insert_events
from src.db.queries.contacts import get_contact_by_id
from src.db.queries.outbox import (
    OUTBOX_PRIORITY_MARKETING,
    OUTBOX_PRIORITY_TRANSACTIONAL,
    enqueue_outbox,
)
from src.db.queries.proactive import (
    business_ids_with_due_jobs,
    claim_due_jobs,
    get_proactive_route,
    get_recipient_external_id,
    mark_job,
)
from src.models import Event
from src.proactive.builders import build_message_spec
from src.proactive.templates import decide_proactive

log = logging.getLogger(__name__)

# reason → proactive_jobs.status (verdictul porții NX-71)
_SKIP_STATUS = {
    "no_optin": "skipped_no_optin",
    "no_window_no_template": "skipped_no_window",
}


def _outbox_priority_for_job(kind: str) -> int:
    """Transactional notices should outrank marketing nudges in the dispatcher queue."""
    if kind == "awb_update":
        return OUTBOX_PRIORITY_TRANSACTIONAL
    return OUTBOX_PRIORITY_MARKETING


class ProactiveRouteError(RuntimeError):
    """Jobul nu poate fi rutat (fără conversație/canal/identitate de destinatar) → failed."""


async def _process_job(conn, business_id: str, job: dict[str, Any], events: list[Event]) -> None:
    """Procesează un job: rutare → build text → poartă → outbox + mark, ATOMIC.

    Ridică (ProactiveRouteError / BuildError / orice) pentru eșecuri → caller-ul
    marchează `failed` într-un savepoint curat. Pentru deciziile normale
    (skip/cancel/sent) marchează inline (savepoint commit)."""
    job_id = job["id"]
    kind = job["kind"]
    conv_id = job["conversation_id"]
    if not conv_id:
        raise ProactiveRouteError("job fără conversation_id")

    route = await get_proactive_route(conn, business_id, conv_id)
    if route is None:
        raise ProactiveRouteError("conversație inexistentă")
    contact = await get_contact_by_id(conn, business_id, job["contact_id"])
    if contact is None:
        raise ProactiveRouteError("contact inexistent")
    to = await get_recipient_external_id(
        conn, business_id, job["contact_id"], route["channel_kind"]
    )
    if not to:
        raise ProactiveRouteError("contact fără identitate de canal")

    spec = await build_message_spec(conn, business_id, job, route)
    if spec.cancel:
        await mark_job(conn, business_id, job_id, "cancelled")
        events.append(Event("proactive_skipped", {"kind": kind, "reason": "cancelled"}))
        return

    decision = await decide_proactive(
        conn,
        business_id=business_id,
        contact=contact,
        conversation=route,
        channel_id=route["channel_id"],
        kind=kind,
        locale=route.get("locale") or "ro",
        template_name=spec.template_name,
        free_text=spec.free_text,
        variables=spec.variables,
    )

    if not decision.allowed:
        status = _SKIP_STATUS.get(decision.reason, "skipped_no_window")
        await mark_job(conn, business_id, job_id, status)
        events.append(Event("proactive_skipped", {"kind": kind, "reason": decision.reason}))
        return

    # Enqueue + mark, ATOMIC (free SAU template — PL-1: calea template e LIVE acum).
    # outbox.kind = 'message' (transport): CHECK-ul permite message/template/typing/reaction,
    # iar dispatcher-ul rutează după `payload.type`. Natura proactivă e deja în idempotency_key +
    # payload.type — NU în kind (care e strategia de transport, nu clasificarea mesajului).
    if decision.mode == "template":
        # În afara ferestrei 24h → template Meta aprobat. `text` = textul randat (floor de
        # degradare pe canale fără TEMPLATE); `template_name`/`language`/`params` → send_template.
        payload = {
            "type": "template",
            "to": to,
            "text": decision.rendered_text,
            "template_name": decision.template_name,
            "language": decision.template_language,
            "params": decision.template_params,
        }
    else:
        # mode == 'free' → mesaj liber în fereastra 24h.
        payload = {"type": "text", "to": to, "text": decision.rendered_text}
    new_id = await enqueue_outbox(
        conn,
        business_id,
        conv_id,
        f"proactive:{job_id}",
        payload,
        kind="message",
        priority=_outbox_priority_for_job(kind),
    )
    await mark_job(conn, business_id, job_id, "sent")
    events.append(
        Event(
            "proactive_enqueued",
            {"kind": kind, "deduped": new_id is None, "mode": decision.mode},
        )
    )


async def _process_tenant(business_id: str, *, batch: int) -> int:
    """Revendică + procesează joburile unui tenant. Întoarce câte au fost atinse.

    Claim-ul ține lock-urile pe durata TX-ului exterior; fiecare job rulează într-un
    savepoint (TX imbricată) → un job care crapă nu poluează restul lotului. Analytics
    se scriu în aceeași TX (tenant_conn, append-only, fără PII)."""
    handled = 0
    async with tenant_conn(business_id) as conn:
        async with conn.transaction():
            jobs = await claim_due_jobs(conn, business_id, limit=batch)
            events: list[Event] = []
            for job in jobs:
                handled += 1
                try:
                    async with conn.transaction():  # savepoint per job
                        await _process_job(conn, business_id, job, events)
                except Exception as e:  # noqa: BLE001 — un job stricat nu rupe lotul (P6)
                    async with conn.transaction():  # savepoint curat pt mark
                        await mark_job(conn, business_id, job["id"], "failed")
                    events.append(
                        Event(
                            "proactive_failed",
                            {"kind": job.get("kind"), "error_type": type(e).__name__},
                        )
                    )
                    log.warning(
                        "proactive job %s (%s) eșuat: %s",
                        job["id"],
                        job.get("kind"),
                        type(e).__name__,
                    )
            if events:
                await insert_events(conn, business_id, events)
    return handled


async def process_due(pool, *, batch: int = 20) -> int:
    """Un ciclu: control plane (tenanți cu joburi scadente) → per tenant. Întoarce nr. atins."""
    async with admin_conn(pool) as conn:
        business_ids = await business_ids_with_due_jobs(conn)
    handled = 0
    for business_id in business_ids:
        try:
            handled += await _process_tenant(business_id, batch=batch)
        except Exception:  # noqa: BLE001 — un tenant stricat nu oprește restul
            log.exception("proactive: eroare la tenantul %s", business_id)
    return handled


async def run_scheduler(pool, *, batch: int = 20, idle_sleep: float = 5.0) -> None:
    """Bucla principală (rulează până la anulare). Doarme `idle_sleep` când nu e nimic scadent."""
    log.info("proactive scheduler pornit (batch=%d)", batch)
    while True:
        handled = await process_due(pool, batch=batch)
        if handled == 0:
            await asyncio.sleep(idle_sleep)


async def _main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    if not settings.proactive_enabled:
        log.info("proactive scheduler dezactivat (PROACTIVE_ENABLED=false) — ies")
        return
    pool = await get_pool()  # admin (control plane: business_ids_with_due_jobs)
    await get_bot_pool()  # eager: parolă bot_runtime greșită → crapă la boot
    try:
        await run_scheduler(
            pool,
            batch=settings.proactive_batch_size,
            idle_sleep=settings.proactive_idle_sleep_s,
        )
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(_main())
