"""Dispatcher — outbox → Meta. Singurul care trimite efectiv (principiul 5).

Sender-ul scrie în `outbox` (stagiul 9); dispatcher-ul citește, trimite la Meta
și marchează rezultatul. Rulează ca proces separat de worker.

Flux:
  1. control plane (admin_conn): ce tenanți au rânduri scadente
  2. per tenant (tenant_conn, RLS): claim_due (FOR UPDATE SKIP LOCKED) → trimite
     fiecare rând → mark_sent (+ leagă wamid pe mesaj) sau mark_failed (backoff)

Idempotență & self-healing: claim împinge next_attempt_at (visibility timeout) →
un dispatcher mort între claim și mark nu pierde rândul (redevine scadent).
La epuizarea încercărilor, rândul devine 'dead' (vizibil, nu pierdut tăcut).
"""

import asyncio
import logging

import httpx

from src.channels.base import ChannelSenderRegistry
from src.channels.telegram.client import TelegramClient
from src.config import get_settings
from src.db.connection import admin_conn, close_pool, get_bot_pool, get_pool, tenant_conn
from src.db.queries.messages import set_message_provider_id
from src.db.queries.outbox import (
    business_ids_with_due_outbox,
    claim_due,
    mark_failed,
    mark_sent,
)
from src.meta_client import MetaClient

log = logging.getLogger(__name__)


async def dispatch_row(conn, business_id: str, registry: ChannelSenderRegistry, row: dict) -> str:
    """Trimite un singur rând de outbox prin canalul lui. Întoarce statusul rezultat.

    Alege transportul din registru după `channel_kind` (NX-60). `conn` e
    tenant-scoped pe `business_id`. Succesul (mark_sent + leagă provider_msg_id pe
    mesajul outbound) e tranzacțional ca să nu rămână outbox 'sent' cu mesaj fără
    id. Eșecul → mark_failed (backoff sau 'dead')."""
    payload = row["payload"]
    outbox_kind = row.get("kind", "message")
    ptype = payload.get("type")
    if outbox_kind not in ("message", "text") or ptype not in (
        "text",
        "products",
        "carousel",
        "edit_media",
    ):
        # text/products/carousel/edit_media; template/interactive/typing → follow-up.
        log.warning(
            "outbox %s: kind/type nesuportat (%s/%s) — marcat dead",
            row["id"],
            outbox_kind,
            ptype,
        )
        await mark_failed(conn, business_id, row["id"], 999, "tip nesuportat de dispatcher")
        return "dead"

    channel_kind = row["channel_kind"]
    sender = registry.get(channel_kind)
    if sender is None:
        log.warning("outbox %s: niciun sender pentru channel_kind=%s", row["id"], channel_kind)
        await mark_failed(conn, business_id, row["id"], 999, f"canal nesuportat: {channel_kind}")
        return "dead"

    try:
        account_id = row["channel_account_id"]
        if ptype == "edit_media":
            # navigare carusel (R2): editează cardul existent. Doar pe canale cu suport.
            if not hasattr(sender, "edit_message_media"):
                await mark_failed(conn, business_id, row["id"], 999, "edit_media nesuportat")
                return "dead"
            provider_id = await sender.edit_message_media(
                account_id,
                payload["to"],
                payload["card_message_id"],
                payload["products"],
                payload["index"],
            )
        elif (
            ptype == "carousel"
            and payload.get("products")
            and hasattr(sender, "send_carousel_card")
        ):
            provider_id = await sender.send_carousel_card(
                account_id, payload["to"], payload["products"], 0
            )
        elif (
            ptype in ("carousel", "products")
            and payload.get("products")
            and hasattr(sender, "send_products")
        ):
            # fallback W1: listă compactă cu butoane-link (carusel nesuportat de canal).
            provider_id = await sender.send_products(
                account_id, payload["to"], payload["text"], payload["products"]
            )
        else:
            # text, sau carusel/products pe un canal fără suport → lead-in ca text
            # (conține deja recomandarea — degradare grațioasă, principiul 6).
            provider_id = await sender.send_text(account_id, payload["to"], payload["text"])
    except Exception as e:  # noqa: BLE001 — orice eroare de transport/HTTP → retry
        status = await mark_failed(conn, business_id, row["id"], row["attempts"], str(e)[:500])
        log.warning("outbox %s: trimitere eșuată (%s) → %s", row["id"], type(e).__name__, status)
        return status

    async with conn.transaction():
        await mark_sent(conn, business_id, row["id"], sent_message_id=payload.get("message_id"))
        if payload.get("message_id"):
            await set_message_provider_id(conn, business_id, payload["message_id"], provider_id)
    log.info("outbox %s trimis pe %s (provider_msg_id=%s)", row["id"], channel_kind, provider_id)
    return "sent"


async def dispatch_due(pool, registry: ChannelSenderRegistry, *, batch: int = 10) -> int:
    """Un ciclu: revendică și trimite rândurile scadente, per tenant. Întoarce
    numărul de rânduri tratate."""
    async with admin_conn(pool) as conn:
        business_ids = await business_ids_with_due_outbox(conn)

    handled = 0
    for business_id in business_ids:
        async with tenant_conn(business_id) as conn:
            rows = await claim_due(conn, business_id, limit=batch)
            for row in rows:
                try:
                    await dispatch_row(conn, business_id, registry, row)
                except Exception:  # noqa: BLE001 — un rând stricat nu oprește restul
                    log.exception("eroare neașteptată la dispatch outbox %s", row["id"])
                handled += 1
    return handled


async def run_dispatcher(pool, registry: ChannelSenderRegistry, *, idle_sleep: float = 2.0) -> None:
    """Bucla principală a dispatcher-ului (rulează până la anulare)."""
    log.info("dispatcher pornit (canale: %s)", registry.kinds())
    while True:
        handled = await dispatch_due(pool, registry)
        if handled == 0:
            await asyncio.sleep(idle_sleep)


def build_registry(http: httpx.AsyncClient, settings) -> ChannelSenderRegistry:
    """Construiește registrul de sender-e din config. Canalele fără credențiale
    nu se înregistrează (rândurile lor → 'dead' cu log explicit)."""
    registry = ChannelSenderRegistry()
    if settings.meta_access_token:
        registry.register("whatsapp", MetaClient(http, settings.meta_access_token))
    if settings.telegram_bot_token:
        registry.register("telegram", TelegramClient(http, settings.telegram_bot_token))
    return registry


async def _main() -> None:
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # nu loga URL-uri cu token
    settings = get_settings()
    pool = await get_pool()  # admin (control plane: business_ids_with_due_outbox)
    await get_bot_pool()  # eager: parolă bot_runtime greșită → crapă la boot
    async with httpx.AsyncClient(timeout=15.0) as http:
        registry = build_registry(http, settings)
        try:
            await run_dispatcher(pool, registry)
        finally:
            await close_pool()


if __name__ == "__main__":
    asyncio.run(_main())
