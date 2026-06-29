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

from src.channels.base import Capability, ChannelSenderRegistry
from src.channels.telegram.client import TelegramClient
from src.channels.web.sender import WebSender
from src.config import get_settings
from src.db.connection import admin_conn, close_pool, get_bot_pool, get_pool, tenant_conn
from src.db.queries.analytics import insert_events
from src.db.queries.messages import set_message_provider_id
from src.db.queries.outbox import (
    business_ids_with_due_outbox,
    claim_due,
    mark_failed,
    mark_sent,
)
from src.meta_client import MetaClient
from src.models import Event
from src.redis_bus import close_redis, get_redis

log = logging.getLogger(__name__)


_DELIVERED = {
    "rich": "rich",
    "carousel": "carousel",
    "products": "cards",
    "edit": "edit",
    "template": "template",
}


def _requested_render(payload: dict, ptype: str | None) -> str:
    """Randarea CERUTĂ de pipeline (taxonomie aliniată cu `_DELIVERED` ca să nu raportăm un
    carousel→carousel reușit drept degradare). rich > carousel > cards > text. IZI-compare:
    `comparison` se cere ca `rich` (web îl livrează prin `send_rich`; canalele text → degradare).
    `template` (proactiv, PL-1) e propria cerere → degradarea template→text e vizibilă în
    render_path."""
    if ptype == "template":
        return "template"
    if payload.get("rich") or payload.get("comparison"):
        return "rich"
    if not payload.get("products"):
        return "text"
    if ptype == "carousel":
        return "carousel"
    if ptype == "products":
        return "cards"
    return "text"


async def _emit_render_path(
    conn,
    business_id: str,
    channel_kind: str,
    payload: dict,
    ptype: str | None,
    branch: str,
    conversation_id: str | None = None,
) -> None:
    """NX-127 (P10): face VIZIBILĂ degradarea de randare (rich/cards → text) per canal — înainte
    pica TĂCUT (cardurile/chips dispăreau). Emite `render_path {channel_kind, requested, delivered}`
    DOAR când cerut ≠ livrat (zero overhead pe calea fericită). Best-effort (livrarea a reușit)."""
    requested = _requested_render(payload, ptype)
    delivered = _DELIVERED.get(branch, "text")
    if requested == delivered:
        return
    try:
        await insert_events(
            conn,
            business_id,
            [
                Event(
                    "render_path",
                    {"channel_kind": channel_kind, "requested": requested, "delivered": delivered},
                )
            ],
            conversation_id=conversation_id,
        )
    except Exception as e:  # noqa: BLE001 — observabilitate best-effort (livrarea a reușit)
        log.warning("render_path emit eșuat (%s)", type(e).__name__)


def choose_render(payload: dict, ptype: str | None, caps: frozenset[Capability]) -> str:
    """NX-115 — rutare table-driven PURĂ pe capabilități. Întoarce ramura de randat:
    'rich' | 'edit' | 'edit_unsupported' | 'carousel' | 'products' | 'text'. Degradează
    mereu spre 'text' (P6, niciodată tăcere). `edit_media` NU degradează la text — e o
    navigare UI, nu conținut nou → 'edit_unsupported' (dead) dacă lipsește EDIT.

    `Reply.offer` (NX-114) e deja aplatizat în payload['text'] (floor); randarea nativă pe
    canale cu OFFER e follow-up (NX-127/CTA WhatsApp) → nicio ramură dedicată azi.

    IZI-compare: `comparison` se randează prin `send_rich` DOAR pe canale cu COMPARISON (web);
    altundeva cade pe floor-ul aplatizat (tabelul ca text) prin degradarea normală spre 'text'."""
    if payload.get("comparison") and Capability.COMPARISON in caps:
        return "rich"
    if payload.get("rich") and Capability.RICH in caps:
        return "rich"
    if ptype == "template":
        # Proactiv în afara ferestrei 24h (PL-1): doar canalele cu TEMPLATE (WhatsApp). Altundeva
        # degradăm grațios la text (floor = `payload['text']`, textul randat) — P6, fără tăcere.
        return "template" if Capability.TEMPLATE in caps else "text"
    if ptype == "edit_media":
        return "edit" if Capability.EDIT in caps else "edit_unsupported"
    if ptype == "carousel" and payload.get("products") and Capability.CAROUSEL in caps:
        return "carousel"
    if ptype in ("carousel", "products") and payload.get("products") and Capability.CARDS in caps:
        return "products"
    return "text"


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
        "template",
    ):
        # text/products/carousel/edit_media/template; interactive/typing → follow-up.
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

    # NX-115: matrice de capabilități în loc de scară `hasattr`. Ramura aleasă pur (testabil)
    # apoi executată; degradare grațioasă spre text (P6). edit_media fără EDIT = dead (UI, nu text).
    caps = getattr(sender, "capabilities", frozenset())
    branch = choose_render(payload, ptype, caps)
    if branch == "edit_unsupported":
        log.warning("outbox %s: edit_media pe canal fără EDIT (%s)", row["id"], channel_kind)
        await mark_failed(conn, business_id, row["id"], 999, "edit_media nesuportat")
        return "dead"

    try:
        account_id = row["channel_account_id"]
        if branch == "template":
            # Proactiv în afara ferestrei 24h (PL-1): template Meta aprobat (poarta NX-71 a validat
            # consent + status). Trimitem name/language/params (NU textul randat — Meta randează).
            provider_id = await sender.send_template(
                account_id,
                payload["to"],
                payload["template_name"],
                payload["language"],
                payload.get("params") or [],
            )
        elif branch == "rich":
            # Recomandare bogată (model iZi): intro + carduri + pick + chips. Canalele fără RICH
            # au degradat deja la 'text' în choose_render (aplatizare — payload['text']).
            provider_id = await sender.send_rich(account_id, payload["to"], payload)
        elif branch == "edit":
            # navigare carusel (R2): editează cardul existent (canal cu EDIT).
            provider_id = await sender.edit_message_media(
                account_id,
                payload["to"],
                payload["card_message_id"],
                payload["products"],
                payload["index"],
            )
        elif branch == "carousel":
            provider_id = await sender.send_carousel_card(
                account_id, payload["to"], payload["products"], 0
            )
        elif branch == "products":
            # listă compactă cu butoane-link (carusel nesuportat de canal, dar are CARDS).
            provider_id = await sender.send_products(
                account_id, payload["to"], payload["text"], payload["products"]
            )
        else:
            # text, sau carusel/products pe un canal fără CARDS/CAROUSEL → lead-in ca text
            # (conține deja recomandarea + offer-ul aplatizat — degradare grațioasă, P6).
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
    await _emit_render_path(
        conn, business_id, channel_kind, payload, ptype, branch, row.get("conversation_id")
    )
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


def build_registry(http: httpx.AsyncClient, settings, redis=None) -> ChannelSenderRegistry:
    """Construiește registrul de sender-e din config. Canalele fără credențiale
    nu se înregistrează (rândurile lor → 'dead' cu log explicit). `redis` (NX-20) e necesar
    pt WebSender (publish SSE); fără el / web dezactivat → canalul webchat nu se înregistrează."""
    registry = ChannelSenderRegistry()
    if settings.meta_access_token:
        registry.register("whatsapp", MetaClient(http, settings.meta_access_token))
    if settings.telegram_bot_token:
        registry.register("telegram", TelegramClient(http, settings.telegram_bot_token))
    if settings.web_enabled and redis is not None:
        registry.register(
            "webchat",
            WebSender(
                redis,
                backlog_size=settings.web_backlog_size,
                backlog_ttl_s=settings.web_backlog_ttl_s,
            ),
        )
    return registry


async def _main() -> None:
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # nu loga URL-uri cu token
    settings = get_settings()
    pool = await get_pool()  # admin (control plane: business_ids_with_due_outbox)
    await get_bot_pool()  # eager: parolă bot_runtime greșită → crapă la boot
    redis = await get_redis() if settings.web_enabled else None  # NX-20: WebSender publică pe SSE
    async with httpx.AsyncClient(timeout=15.0) as http:
        registry = build_registry(http, settings, redis)
        try:
            await run_dispatcher(pool, registry)
        finally:
            if redis is not None:
                await close_redis()
            await close_pool()


if __name__ == "__main__":
    asyncio.run(_main())
