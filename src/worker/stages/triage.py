"""Stagiul 5 — Triaj (GPT-5.4-nano). Primul touchpoint LLM al pipeline-ului.

Clasifică mesajul în: simple | sales | order | handoff | clarify. Output JSON
validat cu Pydantic. `category_key` e validat contra `categories` din DB — dacă
nano inventează o categorie, o aruncăm (principiul: incertitudinea = CLARIFY, nu
recovery). Pentru `simple`/`clarify`, nano compune și răspunsul → early exit.

Degradare grațioasă (principiul 6): fără LLM (cheie lipsă) sau la orice eroare/
JSON invalid, stagiul nu setează nimic și lasă pipeline-ul să continue (echo
fallback până la agentul real, G4).

LLM se apelează DOAR prin adaptorul `src.agent.llm` (principiul 2).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from pydantic import BaseModel, ValidationError

from src.db.queries.catalog import list_category_slugs
from src.models import Route, RouteDecision, TurnContext
from src.worker.context import context_blocks, conversation_transcript

if TYPE_CHECKING:
    from src.worker.runner import PipelineDeps

log = logging.getLogger(__name__)

_SYSTEM = """Ești modulul de TRIAJ al unui asistent de vânzări pentru un magazin online.
Primești un mesaj de la client și îl clasifici. Răspunzi DOAR cu JSON, fără text în plus.

Rute posibile (câmpul "route"):
- "simple"  : salut, mulțumiri, întrebare generală scurtă pe care o poți răspunde direct.
- "sales"   : caută/întreabă despre produse, recomandări, prețuri, comparații.
- "order"   : întrebări despre o comandă existentă, livrare, AWB, retur.
- "handoff" : cere explicit un operator uman, reclamație serioasă, caz sensibil.
- "clarify" : mesaj ambiguu/incomplet — nu e clar ce vrea.

Format JSON de răspuns:
{"route": "<una din cele 5>", "category_key": <slug din lista dată sau null>,
 "missing_field": <ce lipsește, pt clarify, sau null>, "reply": <text sau null>}

Reguli:
- "category_key": DOAR pentru route="sales", și DOAR un slug EXACT din lista
  primită; altfel null.
- "reply": DOAR pentru "simple" (răspuns scurt, prietenos, în limba clientului)
  și "clarify" (o întrebare scurtă de clarificare). Pentru restul rutelor: null.
- Dacă mesajul e un FOLLOW-UP scurt (ex. „mai ieftin", „da", „și pentru păr?"),
  folosește conversația de mai sus ca să-l clasifici corect (de obicei continuă
  „sales"), NU „clarify".
- Nu inventa produse, prețuri sau categorii."""


class TriageOut(BaseModel):
    """Contractul de output al triajului (validare strictă a JSON-ului de la nano)."""

    route: Route
    category_key: str | None = None
    missing_field: str | None = None
    reply: str | None = None


async def triage_stage(ctx: TurnContext, deps: PipelineDeps) -> None:
    """Clasifică turul cu nano și scrie `ctx.route` (+ reply pentru simple/clarify)."""
    if ctx.route is not None:
        return  # NX-130: clarify_resume a setat deja ruta determinist → triajul e no-op (P3)
    if deps.llm is None:
        return  # fără cheie OpenAI → lăsăm echo fallback (degradare grațioasă)
    body = (ctx.message.body or "").strip()
    if not body:
        return

    categories = await list_category_slugs(deps.conn, ctx.business.id)
    transcript = conversation_transcript(ctx.history)
    history_block = f"Conversație până acum:\n{transcript}\n\n" if transcript else ""
    context = context_blocks(ctx)
    context_block = f"{context}\n\n" if context else ""
    user = (
        f"Limba clientului: {ctx.language}\n"
        f"{context_block}"
        f"{history_block}"
        f"Mesaj client NOU: {body}\n"
        f"Categorii valide (slug): {', '.join(categories) or '(niciuna)'}"
    )

    try:
        raw = await deps.llm.classify_json(_SYSTEM, user)
        out = TriageOut(**raw)
    except (ValidationError, ValueError, KeyError) as e:
        log.warning("triaj: output invalid (%s) → fallback", type(e).__name__)
        return
    except Exception as e:  # noqa: BLE001 — eroare de API/rețea → nu blochează turul
        log.warning("triaj: apel LLM eșuat (%s) → fallback", type(e).__name__)
        return

    # category_key inventat (în afara listei) → îl aruncăm (nu rutăm pe ghicit).
    category_key = out.category_key if out.category_key in categories else None
    ctx.route = RouteDecision(
        route=out.route,
        category_key=category_key,
        missing_field=out.missing_field,
    )
    ctx.emit("intent_detected", route=out.route.value, category=category_key)

    # simple / clarify: nano a compus răspunsul → early exit la Sender.
    # simple = răspuns static reutilizabil (cacheabil); clarify = specific contextului.
    if out.route == Route.SIMPLE and out.reply:
        ctx.set_reply(out.reply)
    elif out.route == Route.CLARIFY and out.reply:
        # NX-130: persistă slotul cerut → turul următor îl reia determinist (clarify_resume_stage),
        # fără să re-cheme triajul pe răspunsul scurt al clientului (ex. „200 lei").
        ctx.set_clarify(
            out.reply, field=out.missing_field or "intent", resume_route=Route.SALES.value
        )
