"""Stagiul handoff (NX-123 / R5) — consumă `Route.HANDOFF` din triaj.

R5 (bug live, 2026-06-17): triajul emitea `Route.HANDOFF` (cerere explicită de om) dar niciun
stagiu nu-l consuma → cădea pe fallback-ul generic („n-am înțeles"), încălcând principiul 6.
Aici escaladăm corect: `set_handoff` (botul tace turul următor, omul preia) + notificare
operator + confirmare către client (NICIODATĂ tăcere). Rulează DUPĂ triaj, ÎNAINTE de agent
(care oricum no-op pe HANDOFF). Reutilizează `gates.request_human` (un singur owner al escaladării).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from src.config import handoff_enabled_for
from src.models import Route, RouteDecision, TurnContext
from src.tools.handoff_tools import notify_operator
from src.worker.stages.gates import request_human

if TYPE_CHECKING:
    from src.worker.runner import PipelineDeps

log = logging.getLogger(__name__)

_HANDOFF_REPLY = "Te conectez cu un coleg din echipă — îți răspunde cineva în cel mai scurt timp 🙂"


async def handoff_stage(ctx: TurnContext, deps: PipelineDeps) -> None:
    """Pe `Route.HANDOFF`: escaladează + confirmă clientului. No-op pe orice altă rută.

    Pe canale FĂRĂ handoff (web, fără operator): nu escaladăm și NU trimitem mesaj de „coleg" —
    rescriem ruta la SALES ca agentul (stagiul următor) să răspundă normal (flux normal, fără mesaj
    special; P6). Acoperă HANDOFF din triaj ȘI escaladarea din clarify (ambele ajung aici)."""
    route = ctx.route
    if route is None or route.route != Route.HANDOFF:
        return
    if ctx.reply is not None:  # un stagiu anterior a servit deja → nu suprascriem (P3)
        return
    if not handoff_enabled_for(ctx.message.channel_kind):
        ctx.route = RouteDecision(route=Route.SALES)  # agentul preia (P6), niciun mesaj de operator
        ctx.emit("handoff_suppressed", source="triage")
        return
    try:
        await request_human(deps.conn, ctx, "user_request", source="triage")
        await notify_operator(ctx, "user_request")
    except Exception as e:  # noqa: BLE001 — escaladarea eșuată NU trebuie să tacă turul (P6)
        log.warning("handoff: escaladare eșuată (%s) — răspundem oricum", type(e).__name__)
    # NICIODATĂ tăcere (P6): confirmăm că vine un om. NON-cacheabil (specific contextului).
    ctx.set_reply(_HANDOFF_REPLY, cacheable=False)
