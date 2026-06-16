"""Stagiul 3 (gates) — detecție de limbă (G5c). Refină `ctx.language` din mesaj.

Rulează DUPĂ Gates și ÎNAINTE de Cache: `ctx.language` corect e premisa straturilor
locale-keyed (semantic_cache, faqs, triaj) — principiul 11. Cod PUR determinist
(`detect_language`), ZERO LLM (principiul 2). Nu setează `reply`/`halt` — doar refină
limba și o persistă pe conversație.

Owner `ctx.language`: processorul SEEDează din `conversations.locale`/default;
`language_stage` e DETECTORUL per-tur care o poate REFINA + persista.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from src.db.queries.conversations import set_conversation_locale
from src.lang.detect import detect_language
from src.models import TurnContext

if TYPE_CHECKING:
    from src.worker.runner import PipelineDeps

log = logging.getLogger(__name__)


async def language_stage(ctx: TurnContext, deps: PipelineDeps) -> None:
    supported = ctx.business.supported_locales
    if len(supported) <= 1:
        # tenant mono-lingv → nimic de detectat (zero apel DB).
        return

    detected = detect_language(ctx.message.body, supported)
    if detected is None or detected == ctx.language:
        # fără semnal clar SAU deja pe limba potrivită → păstrăm (precision-first).
        return

    prev = ctx.language
    ctx.language = detected  # owner: language_stage
    ctx.emit("language_detected", **{"from": prev, "to": detected})
    # Persistă → limba „se lipește" pentru tururile următoare. Best-effort: un eșec
    # nu rupe turul (limba detectată rămâne pe ctx pentru ACEST tur).
    try:
        await set_conversation_locale(deps.conn, ctx.business.id, ctx.conversation_id, detected)
    except Exception as e:  # noqa: BLE001 — persist best-effort
        log.warning("language: persist locale eșuat (%s)", type(e).__name__)
