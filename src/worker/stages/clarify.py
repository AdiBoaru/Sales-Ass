"""Stagiul 4 (felia clarificare) — reluare DETERMINISTĂ a unei întrebări în așteptare.

Slot-filling fără LLM (NX-130): dacă turul anterior a pus o întrebare de clarificare
(`conversations.state.pending_question`), mesajul scurt al clientului umple slotul cerut
și rutăm determinist pe intenția de reluat — FĂRĂ să mai chemăm triajul (nano). Răspunsul
la o întrebare pe care NOI am pus-o nu mai costă un apel LLM (P2) și nu mai e re-clasificat
izolat (ex. „200 lei" → nu „ambiguu", ci „bugetul pentru căutarea de produse").

Rulează ÎNTRE `language_stage` și `greeting_stage`: răspunsul scurt NU trebuie tratat ca
salut, ca query de cache, nici re-triat de la zero. No-op dacă nu există slot în așteptare
sau mesajul e gol (P6 — pipeline-ul continuă normal).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from src.models import Route, RouteDecision, TurnContext

if TYPE_CHECKING:
    from src.worker.runner import PipelineDeps

log = logging.getLogger(__name__)


async def clarify_resume_stage(ctx: TurnContext, deps: PipelineDeps) -> None:
    """Consumă `pending_question`: umple `constraints[field]` cu răspunsul brut și setează
    `ctx.route` pe `resume_route` (triajul devine no-op prin gardă pe `ctx.route`). NU setează
    reply — lasă agentul (sales) să răspundă cu slotul acum cunoscut. `pending_question` se
    curăță la writeback (reply non-clarify → slot None)."""
    pq = ctx.state.pending_question
    if not isinstance(pq, dict):
        return  # nimic în așteptare (sau state corupt) → mai departe în pipeline
    # O POZĂ (NX-76: gates îi injectează o descriere ca body) NU e răspuns la un slot de TEXT —
    # e o re-orientare către produs. Nu consuma slotul cu textul derivat din imagine; lasă
    # triajul/agentul să caute pe descriere, iar slotul rămâne în așteptare pentru un răspuns text.
    if ctx.message.content_type == "image":
        return
    answer = (ctx.message.body or "").strip()
    if not answer:
        return  # body gol (ex. media fără descriere) → nu consumăm slotul; rămâne pt data viitoare

    # 1. mesajul curent umple slotul cerut → memorie scurtă citită de context_blocks (state_block).
    field = pq.get("field") or "intent"
    ctx.state.constraints[field] = answer
    # NX-112: marchează slotul ca „deja întrebat" (semnal anti-loop citit de context_blocks/NX-116).
    # Dedup + cap 8 (P4). Mutația pe ctx.state e persistată de processor (merge canonic, P3).
    if field not in ctx.state.asked_intents:
        ctx.state.asked_intents.append(field)
        ctx.state.asked_intents[:] = ctx.state.asked_intents[-8:]
    # 2. rutăm determinist pe intenția de reluat — fără triaj. `route` deja setat → triajul no-op.
    resume = pq.get("resume_route") or Route.SALES.value
    try:
        ctx.route = RouteDecision(route=Route(resume))
    except ValueError:
        ctx.route = RouteDecision(route=Route.SALES)  # rută veche/invalidă → default sales
    ctx.emit("clarify_resumed", field=field)  # FĂRĂ `answer` (P12 — poate fi PII)
