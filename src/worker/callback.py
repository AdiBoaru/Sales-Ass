"""Handler pentru `callback_query` (R2) — navigarea caruselului de produse.

Drum de inbound NON-LLM: o apăsare ◀/▶ e UI deterministă, NU trece prin pipeline
(triaj/agent). Citim setul afișat din `conversations.state.displayed_products`
(persistat de Sender la trimiterea caruselului), calculăm produsul țintă și emitem
o acțiune de EDIT în `outbox` (editează cardul, nu mesaj nou). Un singur punct de
ieșire (principiul 5): editarea iese tot prin outbox → dispatcher.

No-op (fără excepție, fără mesaj nou) când: callback necunoscut, set lipsă din
state (card expirat) sau index în afara limitelor.
"""

import logging
import re

import asyncpg

from src.db.queries.analytics import insert_events
from src.db.queries.contacts import get_or_create_contact
from src.db.queries.conversations import get_or_create_conversation
from src.db.queries.outbox import enqueue_outbox
from src.models import BusinessConfig, Event

log = logging.getLogger(__name__)

_NAV_RE = re.compile(r"^car:nav:(\d+)$")


def parse_nav(data: str | None) -> int | None:
    """`car:nav:{idx}` → idx (int), altfel None. Index pozitiv (clamp la capete
    se face la afișare, butoanele din afara limitelor nici nu apar)."""
    m = _NAV_RE.match(data or "")
    return int(m.group(1)) if m else None


async def handle_callback(
    conn: asyncpg.Connection,
    business: BusinessConfig,
    channel_id: str,
    event: dict,
) -> str | None:
    """Procesează un callback de navigare. Întoarce outbox_id sau None (no-op)."""
    idx = parse_nav(event.get("data"))
    if idx is None:
        log.info("callback necunoscut: %r — ignorat", event.get("data"))
        return None

    chat_id = event["sender_external_id"]
    contact = await get_or_create_contact(
        conn, business.id, event.get("channel_kind", "telegram"), chat_id
    )
    conv = await get_or_create_conversation(
        conn, business.id, contact.id, channel_id, locale=business.default_locale
    )

    products = (conv["state"] or {}).get("displayed_products") or []
    if not 0 <= idx < len(products):
        log.info("callback car:nav:%s în afara setului (%d produse) — no-op", idx, len(products))
        return None

    payload = {
        "type": "edit_media",
        "to": chat_id,
        "card_message_id": event["card_message_id"],
        "products": products,
        "index": idx,
    }
    # idempotency = callback.id: o apăsare = un edit; re-livrarea Telegram nu dublează.
    outbox_id = await enqueue_outbox(
        conn, business.id, conv["id"], f"cb:{event['provider_msg_id']}", payload
    )
    try:
        await insert_events(
            conn,
            business.id,
            [
                Event(
                    type="carousel_navigated",
                    properties={
                        "to_idx": idx,
                        "total": len(products),
                        "product_id": products[idx].get("product_id"),
                    },
                )
            ],
            conversation_id=conv["id"],
            contact_id=contact.id,
        )
    except Exception:  # noqa: BLE001 — analytics best-effort, navigarea continuă
        log.exception("persistarea carousel_navigated a eșuat (navigarea continuă)")

    log.info("carusel: conv=%s nav→%d/%d outbox=%s", conv["id"], idx, len(products), outbox_id)
    return outbox_id
