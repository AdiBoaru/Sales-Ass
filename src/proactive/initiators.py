"""Inițiatorii proactivi (PL-1) — sweeper-ele care CREEAZĂ `proactive_jobs`.

Motorul (NX-70) + poarta (NX-71) + calea template (PR #142) erau gata, dar NIMENI nu insera
joburi → zero mesaje proactive în prod (gap CRITICAL în CONV-COMMERCE-DEEP-ANALYSIS-2026).
Aici sunt sweeper-ele care scanează surse PERSISTENTE și pun joburi (idempotent):
  • abandoned_cart — `checkout_links` neconvertite, abandonate de > prag (sursă: tool checkout).
    Idempotent prin `dedupe_key = abandoned_cart:<checkout_link_id>` (un reminder per coș).
  • back_in_stock  — abonamente cu `notified_at IS NULL` al căror produs e iar pe stoc (tool
    subscribe). Idempotent prin `notified_at` (re-subscribe îl re-armează → suportă re-notificarea),
    NU prin dedupe_key.

`awb_update` + `follow_up` sunt EVENIMENTE, nu sweeper-e (AWB la expediere — webhook orders, încă
TODO, `shipments` n-are writer azi; follow_up = decizie ad-hoc dintr-un flux) → expuse ca
`schedule_awb_update` / `schedule_follow_up` peste `create_proactive_job`, gata de apelat când sursa
lor există. NU facem sweeper pe `shipments` (ar fi cod mort fără date).

Arhitectură (ca dispatcher-ul/motorul): control plane (admin_conn → tenanți cu candidați) → per
tenant (tenant_conn, RLS) → sweep + `create_proactive_job`, ATOMIC pe tenant. NU trimite nimic
(P5): doar INSEREAZĂ joburi; motorul + dispatcher-ul fac restul. Un tenant stricat nu oprește
restul (P6).

    (rulat periodic de mini-scheduler-ul intern, src/jobs/scheduler.py)
"""

from __future__ import annotations

import logging
from typing import Any

from src.config import Settings, get_settings
from src.db.connection import admin_conn, tenant_conn
from src.db.queries.proactive import (
    business_ids_with_abandoned_carts,
    business_ids_with_restocks,
    create_proactive_job,
    find_abandoned_carts,
    find_restocked_subscriptions,
    mark_subscription_notified,
)

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Sweeper-e per-tenant (cod pur, testabil cu un conn fake). Întorc nr. jobs create.
# --------------------------------------------------------------------------- #


async def sweep_abandoned_cart(
    conn, business_id: str, *, older_than_seconds: int, max_age_seconds: int, limit: int
) -> int:
    """Creează un job `abandoned_cart` per coș abandonat eligibil (idempotent pe dedupe_key)."""
    carts = await find_abandoned_carts(
        conn,
        business_id,
        older_than_seconds=older_than_seconds,
        max_age_seconds=max_age_seconds,
        limit=limit,
    )
    created = 0
    for cart in carts:
        if not cart["conversation_id"]:
            continue  # fără conversație nu putem ruta (de regulă imposibil: coșul vine din conv)
        new_id = await create_proactive_job(
            conn,
            business_id,
            contact_id=cart["contact_id"],
            conversation_id=cart["conversation_id"],
            kind="abandoned_cart",
            dedupe_key=f"abandoned_cart:{cart['id']}",
        )
        if new_id is not None:
            created += 1
    return created


async def sweep_back_in_stock(conn, business_id: str, *, limit: int) -> int:
    """Creează un job `back_in_stock` per abonament al cărui produs e iar pe stoc, apoi marchează
    abonamentul notificat (iese din candidați; re-subscribe îl re-armează). Fără dedupe_key:
    `notified_at` e garda, ca re-armarea să poată produce o nouă notificare."""
    subs = await find_restocked_subscriptions(conn, business_id, limit=limit)
    created = 0
    for sub in subs:
        if not sub["conversation_id"]:
            # fără conversație nu putem ruta; marcăm notificat ca să nu re-scanăm la infinit
            await mark_subscription_notified(conn, business_id, sub["id"])
            continue
        new_id = await create_proactive_job(
            conn,
            business_id,
            contact_id=sub["contact_id"],
            conversation_id=sub["conversation_id"],
            kind="back_in_stock",
            payload={"product_id": sub["product_id"]},
        )
        await mark_subscription_notified(conn, business_id, sub["id"])
        if new_id is not None:
            created += 1
    return created


# --------------------------------------------------------------------------- #
# Orchestrare: control plane (admin) → per tenant (RLS), per sursă. Un tenant
# stricat e logat și sărit (P6) — nu oprește restul.
# --------------------------------------------------------------------------- #


async def _run_per_tenant(business_ids: list[str], sweeper, label: str) -> int:
    """Rulează `sweeper(conn, business_id)` în câte o TX per tenant. Întoarce totalul de jobs.
    `tenant_conn` ia singur pool-ul bot_runtime (RLS)."""
    total = 0
    for business_id in business_ids:
        try:
            async with tenant_conn(business_id) as conn:
                async with conn.transaction():
                    total += await sweeper(conn, business_id)
        except Exception:  # noqa: BLE001 — un tenant stricat nu oprește restul (P6)
            log.exception("initiators %s eșuat la tenantul %s", label, business_id)
    return total


async def run_initiators(pool, *, settings: Settings | None = None) -> dict[str, int]:
    """Un ciclu de inițiere: descoperă tenanții cu candidați (admin) → sweep per tenant (RLS).
    Întoarce câte jobs s-au creat pe fiecare sursă. Nu trimite nimic (P5)."""
    s = settings or get_settings()
    batch = s.proactive_initiators_batch

    async with admin_conn(pool) as conn:
        cart_tenants = await business_ids_with_abandoned_carts(
            conn,
            older_than_seconds=s.abandoned_cart_after_seconds,
            max_age_seconds=s.abandoned_cart_max_age_seconds,
        )
        restock_tenants = await business_ids_with_restocks(conn)

    async def _abandoned(conn, business_id: str) -> int:
        return await sweep_abandoned_cart(
            conn,
            business_id,
            older_than_seconds=s.abandoned_cart_after_seconds,
            max_age_seconds=s.abandoned_cart_max_age_seconds,
            limit=batch,
        )

    async def _restock(conn, business_id: str) -> int:
        return await sweep_back_in_stock(conn, business_id, limit=batch)

    return {
        "abandoned_cart": await _run_per_tenant(cart_tenants, _abandoned, "abandoned_cart"),
        "back_in_stock": await _run_per_tenant(restock_tenants, _restock, "back_in_stock"),
    }


# --------------------------------------------------------------------------- #
# Inițiatori pe EVENIMENT (nu sweeper) — seam-uri gata de apelat când sursa apare.
# Apelate în tranzacția caller-ului (ex. webhook orders pt AWB), conn tenant-scoped.
# --------------------------------------------------------------------------- #


async def schedule_awb_update(
    conn,
    business_id: str,
    *,
    contact_id: str,
    conversation_id: str,
    order_id: str,
    awb: str | None = None,
    carrier: str | None = None,
    scheduled_at: Any = None,
) -> str | None:
    """Programează un job `awb_update` la expediere (de apelat de webhook-ul de comenzi — TODO).
    Idempotent pe `awb_update:<order_id>` (o singură notificare de AWB per comandă)."""
    payload: dict[str, Any] = {"order_id": order_id}
    if awb:
        payload["awb"] = awb
    if carrier:
        payload["carrier"] = carrier
    return await create_proactive_job(
        conn,
        business_id,
        contact_id=contact_id,
        conversation_id=conversation_id,
        kind="awb_update",
        payload=payload,
        scheduled_at=scheduled_at,
        dedupe_key=f"awb_update:{order_id}",
    )


async def schedule_follow_up(
    conn,
    business_id: str,
    *,
    contact_id: str,
    conversation_id: str,
    body: str,
    scheduled_at: Any,
    variables: dict[str, str] | None = None,
    dedupe_key: str | None = None,
) -> str | None:
    """Programează un `follow_up` ad-hoc (re-engage). `body` = textul liber (în fereastră);
    `dedupe_key` opțional dacă vrei să eviți dubluri pe o cheie logică (ex. campaign:contact)."""
    payload: dict[str, Any] = {"body": body}
    if variables:
        payload["variables"] = variables
    return await create_proactive_job(
        conn,
        business_id,
        contact_id=contact_id,
        conversation_id=conversation_id,
        kind="follow_up",
        payload=payload,
        scheduled_at=scheduled_at,
        dedupe_key=dedupe_key,
    )
