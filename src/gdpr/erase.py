"""Strat GDPR (NX-72) — erase + export/access, fiecare urmărit în gdpr_requests + audit_log.

Leagă cele trei piese din schema_v2: funcția `gdpr_erase_contact` (security definer),
tabelul `gdpr_requests` (stare) și `audit_log` (cine/când/ce). Drumul determinist:
cerere → `processing` → execuție (erase SQL / export SELECT-uri) → `done`/`failed`.

Conexiune dedicată — `admin_conn` (CONTROL PLANE), NU `bot_runtime`/hot path: erase-ul e
security definer + atinge PII cross-tabel (channel_identities), iar exportul citește
cross-tabel — operații care preced/transcend scope-ul de tenant runtime (justificare ca la
lookup-ul canal→business). FIECARE query are totuși `business_id` în WHERE (P7, mecanism primar).

Funcțiile `_on_conn` primesc un `conn` (testabile + integration cu rollback); API-ul public
(`erase_contact`/`export_contact`/`access_contact`) deschide `admin_conn` și deleagă.
"""

from __future__ import annotations

import logging
from typing import Any

from src.db.connection import admin_conn, get_pool
from src.db.queries.gdpr import (
    contact_in_business,
    count_messages,
    create_request,
    fetch_contact,
    fetch_conversations,
    fetch_identities,
    fetch_messages,
    fetch_orders,
    mark_done,
    mark_failed,
    mark_processing,
    write_audit,
)

log = logging.getLogger(__name__)


async def _erase_on_conn(conn, business_id: str, contact_id: str, *, requested_by: str) -> str:
    """Onorează o cerere de ștergere. Întoarce gdpr_requests.id. Idempotent (erase pe
    contact deja anonimizat = no-op). Loguri DOAR cu id-uri (P12)."""
    req_id = await create_request(conn, business_id, contact_id, "erase", requested_by)
    try:
        await mark_processing(conn, business_id, req_id)
        # izolare: erase-ul SQL nu cere business_id → verificăm întâi că e al acestui tenant
        if not await contact_in_business(conn, business_id, contact_id):
            await mark_failed(conn, business_id, req_id)
            log.info("gdpr_erase req=%s: contact inexistent în tenant → failed", req_id)
            return req_id
        async with conn.transaction():
            await conn.execute("select gdpr_erase_contact($1::uuid)", contact_id)
            # audit suplimentar cu business_id + req_id (funcția scrie unul fără ele)
            await write_audit(
                conn,
                business_id,
                "gdpr_erase",
                "contact",
                contact_id,
                {"request_id": req_id, "requested_by": requested_by},
            )
        await mark_done(conn, business_id, req_id, result_ref=None)
        log.info("gdpr_erase req=%s done", req_id)
    except Exception:
        await mark_failed(conn, business_id, req_id)
        raise
    return req_id


async def _export_on_conn(
    conn, business_id: str, contact_id: str, *, requested_by: str, kind: str
) -> dict[str, Any]:
    """Portabilitate (kind='export', dump integral) sau acces (kind='access', sumar de volum).

    Read-only, `business_id` în fiecare SELECT (P7). Întoarce structura în proces (v1 —
    `result_ref` rămâne NULL; upload în storage cu TTL = task separat)."""
    req_id = await create_request(conn, business_id, contact_id, kind, requested_by)
    try:
        await mark_processing(conn, business_id, req_id)
        data: dict[str, Any] = {
            "request_id": req_id,
            "kind": kind,
            "business_id": business_id,
            "contact_id": contact_id,
            "contact": await fetch_contact(conn, business_id, contact_id),
            "identities": await fetch_identities(conn, business_id, contact_id),
            "conversations": await fetch_conversations(conn, business_id, contact_id),
            "orders": await fetch_orders(conn, business_id, contact_id),
        }
        if kind == "export":
            data["messages"] = await fetch_messages(conn, business_id, contact_id)
        else:  # access — fără dump-ul integral de mesaje, doar volumul
            data["messages_count"] = await count_messages(conn, business_id, contact_id)
        await write_audit(
            conn,
            business_id,
            f"gdpr_{kind}",
            "contact",
            contact_id,
            {"request_id": req_id, "requested_by": requested_by},
        )
        await mark_done(conn, business_id, req_id, result_ref=None)
        log.info("gdpr_%s req=%s done", kind, req_id)
        return data
    except Exception:
        await mark_failed(conn, business_id, req_id)
        raise


# --------------------------------------------------------------------------- #
# API public — deschide admin_conn (control plane) și deleagă
# --------------------------------------------------------------------------- #


async def erase_contact(business_id: str, contact_id: str, *, requested_by: str) -> str:
    """Drept de ștergere (kind='erase'). Întoarce gdpr_requests.id."""
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        return await _erase_on_conn(conn, business_id, contact_id, requested_by=requested_by)


async def export_contact(business_id: str, contact_id: str, *, requested_by: str) -> dict[str, Any]:
    """Portabilitate (kind='export'): TOATE datele contactului într-un dict serializabil."""
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        return await _export_on_conn(
            conn, business_id, contact_id, requested_by=requested_by, kind="export"
        )


async def access_contact(business_id: str, contact_id: str, *, requested_by: str) -> dict[str, Any]:
    """Drept de acces (kind='access'): subset citibil (fără dump-ul integral de mesaje)."""
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        return await _export_on_conn(
            conn, business_id, contact_id, requested_by=requested_by, kind="access"
        )
