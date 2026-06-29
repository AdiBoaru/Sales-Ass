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


async def create_proactive_job(
    conn: asyncpg.Connection,
    business_id: str,
    *,
    contact_id: str,
    conversation_id: str,
    kind: str,
    payload: dict[str, Any] | None = None,
    scheduled_at: Any = None,  # None → now() (fire ASAP); altfel timestamptz
    template_id: str | None = None,
    dedupe_key: str | None = None,
) -> str | None:
    """Inserează un job proactiv (PL-1) — primitivul lipsă: până acum NIMENI nu insera joburi.

    Idempotent prin `dedupe_key` (index unic parțial, 019): re-rularea unui sweeper pe aceeași
    sursă (același coș abandonat) NU duplică jobul → `ON CONFLICT DO NOTHING` întoarce `None`.
    `dedupe_key=None` (back_in_stock gardat de `notified_at`; follow_up ad-hoc) sare peste index
    (NULL nu intră în index parțial) → insert mereu. `scheduled_at=None` → `now()` (motorul îl
    revendică imediat). `conn` deja tenant-scoped (RLS `with check business_id`). Întoarce id sau
    `None` (deduplicat)."""
    return await conn.fetchval(
        """
        insert into proactive_jobs
            (business_id, contact_id, conversation_id, kind, scheduled_at,
             payload, template_id, dedupe_key)
        values ($1, $2, $3, $4, coalesce($5, now()), $6::jsonb, $7, $8)
        on conflict (business_id, dedupe_key) where dedupe_key is not null do nothing
        returning id::text
        """,
        business_id,
        contact_id,
        conversation_id,
        kind,
        scheduled_at,
        json.dumps(payload or {}),
        template_id,
        dedupe_key,
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


# --------------------------------------------------------------------------- #
# INIȚIATORI (PL-1) — candidați pentru sweeper-ele care creează proactive_jobs.
# Control-plane (admin_conn, cross-tenant, non-PII) → tenanți cu candidați; apoi
# per-tenant (tenant_conn, RLS) → rândurile efective. Toate cu business_id (P7).
# --------------------------------------------------------------------------- #


async def business_ids_with_abandoned_carts(
    conn: asyncpg.Connection,
    *,
    older_than_seconds: int,
    max_age_seconds: int,
    limit: int = 100,
) -> list[str]:
    """Tenanți cu coșuri abandonate eligibile — CONTROL PLANE (admin_conn). Coș abandonat =
    checkout_link neconvertit, neexpirat, abandonat de ÎNTRE `older_than` și `max_age` (nu
    reamintim coșuri ancestrale). Non-PII (doar business_id), ca `business_ids_with_due_outbox`."""
    rows = await conn.fetch(
        """
        select distinct business_id::text as business_id
        from checkout_links
        where converted_order_id is null
          and (expires_at is null or expires_at > now())
          and created_at <= now() - make_interval(secs => $1)
          and created_at >= now() - make_interval(secs => $2)
        limit $3
        """,
        older_than_seconds,
        max_age_seconds,
        limit,
    )
    return [r["business_id"] for r in rows]


async def find_abandoned_carts(
    conn: asyncpg.Connection,
    business_id: str,
    *,
    older_than_seconds: int,
    max_age_seconds: int,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Coșurile abandonate ale tenantului (checkout_links neconvertite/neexpirate, abandonate
    de > prag). `business_id = $1` (P7). Întoarce id/contact_id/conversation_id (fără PII)."""
    rows = await conn.fetch(
        """
        select id::text as id, contact_id::text as contact_id,
               conversation_id::text as conversation_id
        from checkout_links
        where business_id = $1
          and converted_order_id is null
          and (expires_at is null or expires_at > now())
          and created_at <= now() - make_interval(secs => $2)
          and created_at >= now() - make_interval(secs => $3)
        order by created_at
        limit $4
        """,
        business_id,
        older_than_seconds,
        max_age_seconds,
        limit,
    )
    return [dict(r) for r in rows]


async def business_ids_with_restocks(conn: asyncpg.Connection, *, limit: int = 100) -> list[str]:
    """Tenanți cu abonamente back-in-stock de notificat — CONTROL PLANE (admin_conn). Eligibil =
    `notified_at IS NULL` (re-subscribe re-armează la NULL) ȘI produsul e iar disponibil."""
    rows = await conn.fetch(
        """
        select distinct s.business_id::text as business_id
        from back_in_stock_subscriptions s
        join products p on p.id = s.product_id and p.business_id = s.business_id
        where s.notified_at is null
          and p.availability in ('in_stock', 'low_stock')
        limit $1
        """,
        limit,
    )
    return [r["business_id"] for r in rows]


async def find_restocked_subscriptions(
    conn: asyncpg.Connection, business_id: str, *, limit: int = 200
) -> list[dict[str, Any]]:
    """Abonamentele de notificat ale tenantului + conversația prin care rutăm. Abonamentul n-are
    conversație → luăm cea mai recentă conversație a contactului (după ultimul mesaj). `None` dacă
    contactul n-are niciuna (sweeper-ul o marchează notificată ca să nu re-scaneze). `business_id`
    (P7)."""
    rows = await conn.fetch(
        """
        select s.id::text as id, s.contact_id::text as contact_id,
               s.product_id::text as product_id,
               (
                 select c.id::text from conversations c
                 where c.business_id = s.business_id and c.contact_id = s.contact_id
                 order by coalesce(c.last_inbound_at, c.last_outbound_at) desc nulls last
                 limit 1
               ) as conversation_id
        from back_in_stock_subscriptions s
        join products p on p.id = s.product_id and p.business_id = s.business_id
        where s.business_id = $1
          and s.notified_at is null
          and p.availability in ('in_stock', 'low_stock')
        order by s.created_at
        limit $2
        """,
        business_id,
        limit,
    )
    return [dict(r) for r in rows]


async def mark_subscription_notified(
    conn: asyncpg.Connection, business_id: str, subscription_id: str
) -> None:
    """Marchează abonamentul ca notificat (`notified_at = now()`) → iese din setul de candidați.
    Re-subscribe (commerce.subscribe_back_in_stock) îl re-armează la NULL. `business_id` (P7)."""
    await conn.execute(
        "update back_in_stock_subscriptions set notified_at = now() "
        "where business_id = $1 and id = $2",
        business_id,
        subscription_id,
    )
