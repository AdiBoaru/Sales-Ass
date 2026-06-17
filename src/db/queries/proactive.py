"""Query-uri pe `proactive_jobs` + read-uri pentru construirea payload-ului (NX-70).

Motorul proactiv revendică joburi scadente (`scheduled_at <= now`, `status='scheduled'`)
cu `FOR UPDATE SKIP LOCKED` (ca outbox-ul) — fără status intermediar `claimed`: jobul
rămâne `scheduled` până la mark-ul terminal, atomic cu `enqueue_outbox` în aceeași TX.

`business_ids_with_due_jobs` rulează pe `admin_conn` (CONTROL PLANE, non-PII): motorul
nu știe dinainte ce tenanți au joburi scadente, exact ca `business_ids_with_due_outbox`.
Restul query-urilor sunt tenant-scoped (RLS) și au `business_id = $1` (P7).

PII: telefonul/chat.id (recipient) vine din `channel_identities` DOAR în `payload.to`;
nicăieri în loguri/analytics (P12).
"""

import json
from typing import Any

import asyncpg

# Câmpurile claim-ului — fără PII (contact_id e uuid, nu telefon).
_JOB_COLS = (
    "id::text, kind, contact_id::text, conversation_id::text, "
    "payload, template_id::text, scheduled_at"
)


def _job_row(row: asyncpg.Record) -> dict[str, Any]:
    d = dict(row)
    d["payload"] = (
        json.loads(d["payload"]) if isinstance(d["payload"], str) else (d["payload"] or {})
    )
    return d


async def business_ids_with_due_jobs(conn: asyncpg.Connection, *, limit: int = 100) -> list[str]:
    """Tenanții cu joburi proactive scadente — CONTROL PLANE (admin_conn, cross-tenant).

    Excepție documentată de la „business_id pe tot" (ca `business_ids_with_due_outbox`):
    precede deschiderea unui tenant_conn per business. Întoarce doar id-uri (non-PII)."""
    rows = await conn.fetch(
        """
        select distinct business_id::text as business_id
        from proactive_jobs
        where status = 'scheduled' and scheduled_at <= now()
        limit $1
        """,
        limit,
    )
    return [r["business_id"] for r in rows]


async def claim_due_jobs(
    conn: asyncpg.Connection, business_id: str, *, limit: int = 20
) -> list[dict[str, Any]]:
    """Revendică joburile scadente ale tenantului (`FOR UPDATE SKIP LOCKED`).

    Lock pe rând cât e ținut TX-ul caller-ului → doi workeri nu revendică același job
    (zero dublu-enqueue). Jobul rămâne `scheduled` până la `mark_job` terminal."""
    rows = await conn.fetch(
        f"""
        select {_JOB_COLS}
        from proactive_jobs
        where business_id = $1 and status = 'scheduled' and scheduled_at <= now()
        order by scheduled_at
        for update skip locked
        limit $2
        """,
        business_id,
        limit,
    )
    return [_job_row(r) for r in rows]


async def mark_job(conn: asyncpg.Connection, business_id: str, job_id: str, status: str) -> None:
    """Marchează jobul terminal (sent / skipped_no_window / skipped_no_optin /
    cancelled / failed)."""
    await conn.execute(
        "update proactive_jobs set status = $3, executed_at = now() "
        "where business_id = $1 and id = $2",
        business_id,
        job_id,
        status,
    )


# --------------------------------------------------------------------------- #
# Read-uri pentru rutare + construirea payload-ului (toate cu business_id = $1)
# --------------------------------------------------------------------------- #


async def get_proactive_route(
    conn: asyncpg.Connection, business_id: str, conversation_id: str
) -> dict[str, Any] | None:
    """Conversația + canalul ei: id, channel_id, locale, channel_kind (din join `channels`).

    Necesar ca să știm CUM rutăm (kind de canal → recipient din channel_identities) și
    CE limbă (pentru cheia de template, P11). `None` dacă conversația nu există."""
    row = await conn.fetchrow(
        """
        select c.id::text as id, c.channel_id::text as channel_id, c.locale,
               ch.kind as channel_kind
        from conversations c
        join channels ch on ch.id = c.channel_id
        where c.business_id = $1 and c.id = $2
        """,
        business_id,
        conversation_id,
    )
    return dict(row) if row else None


async def get_recipient_external_id(
    conn: asyncpg.Connection, business_id: str, contact_id: str, channel_kind: str
) -> str | None:
    """Id-ul de canal al destinatarului (wa_id / chat.id) din `channel_identities`.

    SINGURUL loc cu PII; intră DOAR în `payload.to` (necesar dispatcher-ului), niciodată
    în loguri/analytics (P12). `None` = contact fără identitate pe acest canal."""
    return await conn.fetchval(
        """
        select external_id from channel_identities
        where business_id = $1 and contact_id = $2 and channel_kind = $3
        limit 1
        """,
        business_id,
        contact_id,
        channel_kind,
    )


async def get_shipment_for_order(
    conn: asyncpg.Connection, business_id: str, order_id: str
) -> dict[str, Any] | None:
    """Cea mai recentă expediere a unei comenzi (confirmare opțională pt awb_update)."""
    row = await conn.fetchrow(
        """
        select carrier, awb, status from shipments
        where business_id = $1 and order_id = $2
        order by updated_at desc limit 1
        """,
        business_id,
        order_id,
    )
    return dict(row) if row else None


async def get_product_for_notice(
    conn: asyncpg.Connection, business_id: str, product_id: str
) -> dict[str, Any] | None:
    """Nume + URL produs (back_in_stock)."""
    row = await conn.fetchrow(
        "select name, product_url from products where business_id = $1 and id = $2",
        business_id,
        product_id,
    )
    return dict(row) if row else None


async def get_latest_checkout(
    conn: asyncpg.Connection, business_id: str, conversation_id: str
) -> dict[str, Any] | None:
    """Cel mai recent checkout link al conversației + dacă e convertit/expirat.

    `expired` e derivat în SQL (now() consistent în TX). Builder-ul de abandoned_cart
    decide: convertit/expirat → cancelled; altfel → reamintire cu URL."""
    row = await conn.fetchrow(
        """
        select id::text, url, converted_order_id::text as converted_order_id,
               (expires_at is not null and expires_at < now()) as expired
        from checkout_links
        where business_id = $1 and conversation_id = $2
        order by created_at desc limit 1
        """,
        business_id,
        conversation_id,
    )
    return dict(row) if row else None
