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
from src.db.queries.conversations import set_handoff
from src.models import TurnContext

if TYPE_CHECKING:
    from src.worker.runner import PipelineDeps

log = logging.getLogger(__name__)

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


async def gates_stage(ctx: TurnContext, deps: PipelineDeps) -> None:
    """Porțile de control (vezi docstring-ul modulului). Early-exit pe oricare."""
    # 1. kill-switch: botul e oprit pe ACEASTĂ conversație → tăcere.
    if not ctx.bot_active:
        ctx.halt_silent("bot_inactive")
        return

    # 2. handoff activ: un om a preluat până la handoff_until → tăcere.
    if ctx.handoff_until is not None and ctx.handoff_until > datetime.now(UTC):
        ctx.halt_silent("handoff_active")
        return

    # 3. risc → escaladează + UN mesaj de tranziție; turul următor va cădea pe (2).
    reason = detect_risk(ctx.message.body)
    if reason:
        await request_human(deps.conn, ctx, reason, source="risk")
        ctx.set_reply("Te conectez cu un coleg, revin imediat 🙂")
