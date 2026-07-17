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
from src.db.queries.catalog import get_products_by_ids
from src.db.queries.contacts import get_or_create_contact
from src.db.queries.conversations import get_or_create_conversation
from src.db.queries.outbox import enqueue_outbox
from src.models import BusinessConfig, Event
from src.safety.policy import SafetyPolicy

log = logging.getLogger(__name__)

_NAV_RE = re.compile(r"^car:nav:(\d+)$")


def parse_nav(data: str | None) -> int | None:
    """`car:nav:{idx}` → idx (int), altfel None. Index pozitiv (clamp la capete
    se face la afișare, butoanele din afara limitelor nici nu apar)."""
    m = _NAV_RE.match(data or "")
    return int(m.group(1)) if m else None


async def _safe_products(
    conn: asyncpg.Connection, business: BusinessConfig, conv: dict, products: list[dict]
) -> list[dict]:
    """NX-173: scoate din setul caruselului produsele contraindicate pentru contextul PERSISTAT.

    Ref-urile din state n-au `attributes` (P8: doar id/nume/preț) → hidratăm din catalog ca să
    judecăm pe fapte („LumaDerm Renew Ser" nu-și trădează retinalul în nume). Costul apare DOAR
    când conversația chiar are un context de siguranță activ — pentru restul, zero query în plus.

    Eșec de hidratare pe un context ACTIV → întoarcem [] (no-op la navigare): fail-CLOSED, ca
    peste tot. Un carusel care nu se mișcă e un bug de UX; unul care arată retinol unei gravide e
    bug-ul pe care îl reparăm."""
    policy = SafetyPolicy.from_state(conv.get("state") or {})
    if not policy.contexts or not products:
        return products
    ids = [str(p.get("product_id")) for p in products if p.get("product_id")]
    try:
        hydrated = await get_products_by_ids(conn, business.id, ids, limit=len(ids))
    except Exception:  # noqa: BLE001
        log.exception("carusel: hidratare eșuată pe context de siguranță — no-op (fail-closed)")
        return []
    blocked = set(policy.evaluate(hydrated, purpose="carousel").blocked_ids)
    if not blocked:
        return products
    kept = [p for p in products if str(p.get("product_id")) not in blocked]
    log.info(
        "carusel: %d produs(e) blocate de policy (context=%s)",
        len(blocked),
        sorted(policy.contexts),
    )
    return kept


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
    # NX-173 (P0): caruselul e un drum de inbound care NU trece prin pipeline → nici prin runner,
    # nici prin `safety_compose.enforce`, nici prin vreun gate de tool. Setul vine direct din state,
    # care poate fi VECHI (afișat înainte de declararea sarcinii), importat, sau rămas murdar dacă
    # prune-ul a picat o dată. Fără gate aici, un `car:nav:0` reexpune produsul blocat (review
    # Codex #229). Contextul îl luăm din `state.safety` — un callback n-are mesaj de analizat, deci
    # persistarea contextului e singura sursă.
    products = await _safe_products(conn, business, conv, products)
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
