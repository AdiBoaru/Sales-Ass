"""Stagiul 3 — Gates. Decide DETERMINIST dacă botul are voie să răspundă.

Primul stagiu real de control, înaintea oricărui LLM (principiul 2). Trei porți,
în ordine, fiecare cu early-exit:
  1. bot_active=False  → tăcere (kill-switch per conversație; omul scrie din inbox)
  2. handoff activ     → tăcere (un om a preluat până la handoff_until)
  3. risc (pattern)    → request_human + UN mesaj de tranziție, apoi botul tace

AGNOSTIC de canal: gate-ul decide doar „răspunde botul?". CUM arată handoff-ul
(tăcere pe WhatsApp/TG vs agent live pe webchat) e treaba marginilor, nu a
gate-ului — aici doar setăm starea (`handoff_until`/`risk_flags`) și emitem
`handoff_requested`. Tăcerea intenționată (`ctx.halt_silent`) e singura excepție
documentată de la principiul 6.

Câmpuri TurnContext scrise aici: `ctx.halt` (via halt_silent) și `ctx.reply` (risc).
"""

from __future__ import annotations

import logging
import unicodedata
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import asyncpg

from src.config import get_settings
from src.db.queries.contacts import block_contact
from src.db.queries.conversations import set_handoff
from src.models import TurnContext
from src.worker.limits import rate_limit_count

if TYPE_CHECKING:
    from src.worker.runner import PipelineDeps

log = logging.getLogger(__name__)

# Răspuns neutru la un mesaj flagged (NX-15): ne-cache-uit, fără a premia abuzul cu un om.
NEUTRAL_MSG = (
    "Hai să păstrăm conversația respectuoasă, te rog 🙂 "
    "Cu ce te pot ajuta legat de produse sau comenzi?"
)

# Fereastra contorului de flag-uri (secunde) — 24h, ca pragul de blocklist să fie pe zi.
_FLAG_WINDOW_S = 24 * 60 * 60

# Mesaj de throttle (G2c): trimis O SINGURĂ dată la depășirea pragului de rate limit.
THROTTLE_MSG = (
    "Primesc multe mesaje deodată 🙂 Îți răspund imediat, mai trimite-mi în câteva secunde."
)

# Pattern-uri de risc (RO, normalizate fără diacritice/uppercase). Determinist, NU LLM.
# Extensibil per-business din settings = follow-up.
RISK_PATTERNS: dict[str, list[str]] = {
    "human_request": [
        "vreau sa vorbesc cu un om",
        "vorbesc cu un om",
        "cu un operator",
        "operator uman",
        "agent uman",
        "om real",
        "persoana reala",
    ],
    "legal_complaint": [
        "avocat",
        "anaf",
        "protectia consumatorului",
        "reclamatie",
        "instanta",
        "te dau in judecata",
        "in judecata",
    ],
}


def _norm(text: str) -> str:
    """Lowercase + fără diacritice (NFKD) → match robust pe „să"/„SA"/„sa"."""
    decomposed = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def detect_risk(text: str | None) -> str | None:
    """Întoarce motivul de escaladare (primul găsit) sau None. Pur, fără LLM."""
    if not text:
        return None
    norm = _norm(text)
    for reason, phrases in RISK_PATTERNS.items():
        if any(phrase in norm for phrase in phrases):
            return reason
    return None


async def request_human(
    conn: asyncpg.Connection,
    ctx: TurnContext,
    reason: str,
    *,
    source: str = "risk",
    assigned_user_id: str | None = None,
) -> None:
    """Escaladează la om: setează fereastra de handoff + risk_flag, emite evenimentul.

    `assigned_user_id` e un CÂRLIG (web-ready): G5a nu auto-asignează — îl umple
    consola de agent (task de margine). Partea activă acum = `handoff_until` +
    `risk_flags` + `handoff_requested` (channel-agnostic)."""
    window = get_settings().handoff_window_minutes
    await set_handoff(
        conn,
        ctx.business.id,
        ctx.conversation_id,
        window_minutes=window,
        risk_flag=reason,
        assigned_user_id=assigned_user_id,
    )
    ctx.emit("handoff_requested", reason=reason, source=source)


async def _record_flag_and_maybe_block(ctx: TurnContext, deps: PipelineDeps) -> None:
    """Contor de flag-uri în Redis (analytics e append-only fără SELECT din runtime).
    La ≥ prag într-o fereastră de 24h → abuse blocklist. Best-effort: orice eșec se
    loghează, NU rupe răspunsul neutru (deja setat)."""
    if deps.redis is None:
        return
    key = f"modflags:{ctx.business.id}:{ctx.contact.id}"
    try:
        count = await deps.redis.incr(key)
        if count == 1:
            await deps.redis.expire(key, _FLAG_WINDOW_S)
        if count >= get_settings().moderation_block_threshold:
            await block_contact(deps.conn, ctx.business.id, ctx.contact.id)
            ctx.emit("contact_blocked", flag_count=count)
    except Exception as e:  # noqa: BLE001 — contorul e best-effort, răspunsul neutru rămâne
        log.warning("moderation: contor/blocklist eșuat (%s)", type(e).__name__)


async def _rate_limited(ctx: TurnContext, deps: PipelineDeps) -> bool:
    """Rate limit per contact (G2c). True ⇒ peste prag: a setat throttle (la depășire) sau
    a tăcut (deja peste) → early-exit. Fail-OPEN: Redis jos / dezactivat → False (nu blochează).
    Rulează ÎNAINTE de moderation (check Redis ieftin înaintea apelului de moderation API)."""
    settings = get_settings()
    if not settings.rate_limit_enabled or deps.redis is None:
        return False
    try:
        count = await rate_limit_count(
            deps.redis, ctx.business.id, ctx.contact.id, settings.rate_limit_window_seconds
        )
    except Exception as e:  # noqa: BLE001 — guard Redis jos → fail-open
        log.warning("rate limit: contor eșuat (%s) → fail-open", type(e).__name__)
        return False
    if count <= settings.rate_limit_max:
        return False
    ctx.emit("rate_limited", count=count)
    if count == settings.rate_limit_max + 1:
        # tocmai a depășit → un singur mesaj de throttle (apoi tăcere pe restul burst-ului).
        ctx.set_reply(THROTTLE_MSG, cacheable=False)
    else:
        ctx.halt_silent("rate_limited")
    return True


async def _moderation_blocked(ctx: TurnContext, deps: PipelineDeps) -> bool:
    """Poarta de moderare (NX-15). True ⇒ flagged: a setat răspunsul neutru → early-exit.

    Fail-OPEN: fără cheie / API jos → False (mesajul trece normal). Indisponibilitatea
    moderării NU trebuie să tacă tot traficul; e best-effort safety, nu o poartă dură."""
    settings = get_settings()
    if not settings.moderation_enabled or deps.llm is None:
        return False
    body = (ctx.message.body or "").strip()
    if not body:
        return False
    try:
        res = await deps.llm.moderate(body)
    except Exception as e:  # noqa: BLE001 — fail-open
        log.warning("moderation: apel eșuat (%s) → fail-open", type(e).__name__)
        return False
    if not res.flagged:
        return False
    # Flagged: NICIODATĂ corpul în analytics (principiul 12) — doar categoriile.
    ctx.emit("message_moderated", categories=res.categories)
    await _record_flag_and_maybe_block(ctx, deps)
    ctx.set_reply(NEUTRAL_MSG, cacheable=False)
    return True


async def gates_stage(ctx: TurnContext, deps: PipelineDeps) -> None:
    """Porțile de control (vezi docstring-ul modulului). Early-exit pe oricare."""
    # 1. kill-switch: botul e oprit pe ACEASTĂ conversație → tăcere.
    if not ctx.bot_active:
        ctx.halt_silent("bot_inactive")
        return

    # 2. abuse blocklist (NX-15): contact blocat → tăcere (ca handoff).
    if ctx.contact.is_blocked:
        ctx.halt_silent("contact_blocked")
        return

    # 3. handoff activ: un om a preluat până la handoff_until → tăcere.
    if ctx.handoff_until is not None and ctx.handoff_until > datetime.now(UTC):
        ctx.halt_silent("handoff_active")
        return

    # 4. rate limit (G2c): prea multe mesaje/fereastră → throttle (ieftin, înaintea moderării).
    if await _rate_limited(ctx, deps):
        return

    # 5. moderare (NX-15): mesaj toxic → răspuns neutru, NU ajunge la triaj/agent.
    #    Înaintea riscului: abuzul primește răspuns neutru, nu escaladare la om.
    if await _moderation_blocked(ctx, deps):
        return

    # 6. risc → escaladează + UN mesaj de tranziție; turul următor va cădea pe (3).
    reason = detect_risk(ctx.message.body)
    if reason:
        await request_human(deps.conn, ctx, reason, source="risk")
        ctx.set_reply("Te conectez cu un coleg, revin imediat 🙂")
