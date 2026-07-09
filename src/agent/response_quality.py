"""NX-159 felia 1 — telemetrie de CALITATE a formei răspunsului (observator GLOBAL post-reply).

Corecția arhitecturală (vs. „check_completeness(ctx, plan)"): măsurarea NU poate atârna de
`ResponsePlan` — acela există DOAR pe calea agent/sales. Ar rata exact căile care produc răspunsuri
proaste: `simple`/nano („Da."), cache, FAQ, no-results, welcome, clarify. Deci telemetria pleacă din
RUNNER, singurul punct prin care trec TOATE căile terminale (`pipeline_early_exit`).

Module PUR: primește `TurnContext` (reply/route/retrieval deja setate) + numele stagiului de
early-exit, întoarce dict-uri de properties pentru `ctx.emit`. Zero I/O, zero LLM, zero scriere.
P12: `response_shape` = DOAR forma (lungimi, booleeni, rută, stagiu) — ZERO text de reply, ZERO PII.
`completeness_gap` = doar `intent` + CHEILE lipsă (nu conținut). Runner-ul le emite (P10).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from src.models import Route

if TYPE_CHECKING:
    from src.models import TurnContext

# Sub acest prag (caractere, text strip-uit) un răspuns e „scurt" — semnalul clasic „Da." / „Ok.".
SHORT_REPLY_CHARS = 20


def reply_shape(ctx: TurnContext, stage: str) -> dict[str, Any]:
    """Forma răspunsului servit, derivată 100% determinist din `ctx.reply` — pt `response_shape`.
    P12: DOAR metadate de formă; niciun fragment de text, niciun câmp cu PII. `ctx.halt` (tăcere
    intenționată) n-are reply → caller-ul NU cheamă asta pe halt."""
    r = ctx.reply
    text = (r.text or "") if r is not None else ""
    n = len(text.strip())
    route = ctx.route.route.value if ctx.route and ctx.route.route else None
    return {
        "chars": n,
        "under_20": n < SHORT_REPLY_CHARS,
        "has_question": "?" in text,
        "has_products": bool(r and r.products),
        "has_suggestions": bool(r and r.suggestions),
        "is_rich": bool(r and r.rich is not None),
        "is_comparison": bool(r and r.comparison is not None),
        "has_offer": bool(r and r.offer is not None),
        "is_clarify": bool(r and r.pending_question is not None),
        "route": route,
        "stage": stage,
        "from_cache": bool(getattr(ctx, "from_cache", False)),
    }


def _has_next_step(r) -> bool:
    """„Următor pas" = reply-ul lasă clientului o cale de continuare: o întrebare, chips de
    sugestie, o ofertă/CTA sau un slot de clarificare deschis. Absența TUTUROR = fundătură."""
    text = r.text or ""
    return bool(
        "?" in text or r.suggestions or r.offer is not None or r.pending_question is not None
    )


def completeness_gaps(ctx: TurnContext) -> list[str]:
    """CHEILE de completitudine lipsă, derivate determinist din `ctx` — pentru `completeness_gap`.
    DOAR unde are sens (sales/order/clarify); niciun LLM, nicio re-validare de grounding (aia e la
    validator, NX-142). Lista goală → caller-ul NU emite event. Chei posibile:
      • `next_step`   — sales cu produse, dar fără nicio cale de continuare
      • `alternative` — sales fără produse (no-result), fără alternativă oferită
      • `question`    — clarify fără „?" (o clarificare care nu întreabă nimic)
      • `asked_field` — order fără date de comandă și fără să ceară nr comandă / login
    """
    r = ctx.reply
    if r is None:
        return []
    route = ctx.route.route if ctx.route else None
    gaps: list[str] = []

    if route == Route.SALES:
        has_products = bool(r.products) or bool(r.rich is not None)
        if has_products or bool(r.comparison is not None):
            if not _has_next_step(r):
                gaps.append("next_step")
        elif not _has_next_step(r):
            # sales fără produse = no-result / clarificare de vânzare → trebuie o cale înainte.
            gaps.append("alternative")
    elif route == Route.CLARIFY:
        if "?" not in (r.text or "") and not r.suggestions:
            gaps.append("question")
    elif route == Route.ORDER:
        # order „complet" = fie a raportat date (produse/ofertă), fie cere explicit câmpul lipsă.
        reported = bool(r.products) or bool(r.offer is not None)
        if not reported and not _has_next_step(r):
            gaps.append("asked_field")

    return gaps
