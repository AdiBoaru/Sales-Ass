"""Tool de cunoștințe (NX-74) — `faq_lookup`: regulile magazinului, pe ruta de vânzare.

Pe drumul unei recomandări agentul poate avea nevoie de o regulă de business („pot plăti
ramburs?", „cât e livrarea?"). Unealta aduce întrebările frecvente ACTIVE ale tenantului, pe
locale, iar MODELUL alege răspunsul care se potrivește. Nu există potrivire pe vectori:
embeddings au fost scoase din proiect (2026-09-24), iar pe 30 de zile căutarea pe vectori
servise 0 răspunsuri din 102. Setul are 20 de intrări pe `sole-ro`, deci încape întreg.

`business_id`/`locale` vin din `ctx`, NU din argumente (P7, P11). Argumentul `query` rămâne în
schemă: modelul își numește întrebarea, iar numele ei apare în telemetria uneltei, nu în cerere.
FAQ-ul nu produce produse → `products=[]`, doar `llm_view`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from src.config import get_settings
from src.db.queries.faqs import list_active
from src.tools.base import ToolResult, register

if TYPE_CHECKING:
    from src.models import TurnContext
    from src.worker.runner import PipelineDeps

#: Plafoanele vederii (P4). Setul real are 20 de intrări și ~4.000 de caractere; plafonul e acolo
#: ca un tenant cu sute de FAQ să nu umple contextul, nu ca să taie setul de azi.
MAX_FAQS = 40
MAX_VIEW_CHARS = 8_000

_HEADER = (
    "Regulile magazinului, ca întrebări frecvente. Folosește DOAR răspunsul care se potrivește cu "
    "întrebarea clientului și redă-l fidel. Dacă niciunul nu se potrivește, spune că nu ai "
    "informația, nu o compune din ce știi tu."
)
_EMPTY = (
    "Nu am un răspuns în baza de cunoștințe; spune-i clientului că nu ai informația, fără să "
    "inventezi o regulă."
)


class FaqArgs(BaseModel):
    query: str = Field(min_length=1, max_length=400)


def render_view(rows: list[dict[str, Any]]) -> str:
    """Setul de FAQ → textul uneltei. PUR. Intrările care nu mai încap în plafon se lasă afară
    ÎNTREGI (niciodată un răspuns tăiat la mijloc: o regulă pe jumătate e o regulă falsă)."""
    parts = [_HEADER, ""]
    used = len(_HEADER)
    for i, r in enumerate(rows, 1):
        entry = f"{i}. Întrebare: {r['question'].strip()}\n   Răspuns: {r['answer'].strip()}"
        if used + len(entry) > MAX_VIEW_CHARS:
            break
        parts.append(entry)
        used += len(entry)
    return "\n".join(parts)


@register("faq_lookup")
async def faq_lookup_tool(ctx: TurnContext, deps: PipelineDeps, args: dict[str, Any]) -> ToolResult:
    """Setul de FAQ al tenantului pe limba turului. Setul gol pe limba turului cade pe
    `default_locale` DOAR sub `FAQ_LOCALE_FALLBACK_ENABLED` (o regulă în altă limbă e mai bună
    decât niciuna, dar e o decizie a tenantului, nu implicită)."""
    FaqArgs(**args)
    async with deps.db("faq_list") as conn:
        rows = await list_active(conn, ctx.business.id, ctx.language, limit=MAX_FAQS)
        default_locale = getattr(ctx.business, "default_locale", None)
        if (
            not rows
            and get_settings().faq_locale_fallback_enabled
            and default_locale
            and default_locale != ctx.language
        ):
            rows = await list_active(conn, ctx.business.id, default_locale, limit=MAX_FAQS)
    if not rows:
        return ToolResult(ok=True, llm_view=_EMPTY)
    return ToolResult(ok=True, llm_view=render_view(rows))
