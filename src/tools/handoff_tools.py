"""Tool-uri de handoff — escaladare la operator uman (NX-123 / NX-82).

`request_human` = tool de AGENT: când modelul detectează că nu mai poate ajuta (frustrare,
cerere explicită de om, caz în afara scopului), escaladează → `set_handoff` (botul tace turul
următor, omul preia) + notificare operator. OPT-IN per business (vezi `enabled_tools`): doar
tenanții cu operator îl oferă. `notify_operator` = POST best-effort spre webhook, FĂRĂ PII.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import httpx
from pydantic import BaseModel, Field

from src.config import get_settings
from src.db.queries.conversations import set_handoff
from src.tools.base import ToolResult, register

if TYPE_CHECKING:
    from src.models import TurnContext
    from src.worker.runner import PipelineDeps

log = logging.getLogger(__name__)


async def notify_operator(ctx: TurnContext, reason: str) -> None:
    """Anunță operatorul de un handoff, best-effort (NU rupe turul, P6). POST spre
    `operator_alert_webhook` (gol → no-op). Payload FĂRĂ PII (P12): slug business +
    conversation_id + motiv — NICIODATĂ telefon/nume/corp mesaj. Client httpx dedicat
    (handoff = rar; nu merită un client persistent în deps)."""
    webhook = get_settings().operator_alert_webhook
    if not webhook:
        return
    try:
        async with httpx.AsyncClient(timeout=5.0) as http:
            await http.post(
                webhook,
                json={
                    "business": ctx.business.slug,
                    "conversation_id": ctx.conversation_id,
                    "reason": reason,
                },
            )
    except Exception as e:  # noqa: BLE001 — webhook best-effort (operatorul are și dashboard)
        log.warning("notify_operator eșuat (%s) — ignorat", type(e).__name__)


class RequestHumanArgs(BaseModel):
    reason: str = Field(min_length=1, max_length=200)


@register("request_human")
async def request_human_tool(
    ctx: TurnContext, deps: PipelineDeps, args: dict[str, Any]
) -> ToolResult:
    """Escaladează conversația la un operator uman (NX-82). Setează `handoff_until` (botul tace
    turul următor) + notifică operatorul. `reason` (din model) ajunge la operator, NU în event
    (P12: eventul folosește un token fix `agent_request`, fără fragmente de conversație)."""
    a = RequestHumanArgs(**args)
    await set_handoff(
        deps.conn,
        ctx.business.id,
        ctx.conversation_id,
        window_minutes=get_settings().handoff_window_minutes,
        risk_flag="agent_request",
    )
    ctx.emit("handoff_requested", reason="agent_request", source="agent")
    await notify_operator(ctx, a.reason)
    return ToolResult(
        ok=True,
        llm_view=(
            "Am anunțat un coleg uman care preia conversația. Spune-i clientului, pe scurt și "
            "prietenos, că revine cineva în cel mai scurt timp."
        ),
    )
