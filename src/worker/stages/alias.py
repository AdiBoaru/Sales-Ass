"""Stagiul 4 — Strat gratuit alias (NX-73). Match EXACT al frazei normalizate în
`intent_aliases` (`status='approved'`), ÎNAINTE de cache + triaj → early-exit FĂRĂ niciun apel
LLM (nici embed). Stratul CEL MAI IEFTIN: lookup pe index B-tree, zero token.

`phrase_norm` = `canonicalize(body)` — REFOLOSIT din `cache/canonical.py`, aceeași normalizare cu
care NX-93 va scrie candidații (altfel match-ul exact ratează). La hit, după `target_kind`:
  • `faq`   → fetch răspunsul FAQ în `ctx.language` → `ctx.reply` (early-exit). FAQ lipsă în limba
    curentă → miss grațios (P11).
  • `route` → `ctx.route = RouteDecision(route)` → scurtcircuitează triajul (un nano economisit).
  • `product` / `category` → `ctx.route = SALES` (+ `category_key` dacă avem slug) → spre agent.

Owner scurtcircuit pe `ctx.route`: alias NU rulează dacă `ctx.route` e DEJA setat (clarify_resume
NX-130 a rutat turul) → un singur scriitor pe tur (P3). Un hit `route`/`product`/`category` setează
`ctx.route` (fără reply); cache + FAQ + triaj îl RESPECTĂ (`if ctx.route is not None: return`) →
preempt-ate, agentul servește. Un hit `faq` setează reply → early-exit direct la Sender.

Best-effort ca cache/FAQ: orice eroare (migrare neaplicată, DB jos) → miss, NU rupe turul (P6).
ZERO LLM (cod pur). P12: emit DOAR hit/target_kind/route/reason, NICIODATĂ `phrase_norm` (poate
conține fragmente de mesaj).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from src.cache.canonical import canonicalize
from src.config import get_settings
from src.db.queries.aliases import get_faq_answer, lookup_alias
from src.models import Route, RouteDecision, TurnContext

if TYPE_CHECKING:
    from src.worker.runner import PipelineDeps

log = logging.getLogger(__name__)


def _coerce_route(value: str | None) -> Route | None:
    """`target_value` (string) → `Route`, sau `None` dacă invalid (alias prost configurat)."""
    try:
        return Route(value) if value else None
    except ValueError:
        return None


async def alias_stage(ctx: TurnContext, deps: PipelineDeps) -> None:
    settings = get_settings()
    if not settings.alias_enabled:
        return
    if ctx.route is not None:
        return  # clarify_resume (NX-130) a rutat deja acest tur → nu suprascriem (P3)
    body = (ctx.message.body or "").strip()
    if not body:
        return
    phrase_norm, _ = canonicalize(body)
    if not phrase_norm:
        return
    try:
        alias = await lookup_alias(deps.conn, ctx.business.id, phrase_norm)
        if alias is None:
            ctx.emit("alias_lookup", hit=False)
            return
        kind = alias["target_kind"]
        if kind == "faq":
            answer = await get_faq_answer(
                deps.conn, ctx.business.id, alias["target_id"], ctx.language
            )
            if answer is None:  # FAQ lipsă în limba curentă → miss grațios (P11)
                ctx.emit("alias_lookup", hit=False, target_kind=kind, reason="faq_locale_miss")
                return
            ctx.set_reply(answer)  # cacheabil (răspuns static; G5b îl prinde la paraphrase)
            ctx.emit("alias_lookup", hit=True, target_kind=kind)
        elif kind == "route":
            route = _coerce_route(alias.get("target_value"))
            if route is None:
                ctx.emit("alias_lookup", hit=False, target_kind=kind, reason="bad_route")
                return
            ctx.route = RouteDecision(route=route)
            ctx.emit("alias_lookup", hit=True, target_kind=kind, route=route.value)
        else:  # product | category → rutare sales (+ category_key dacă avem slug)
            ctx.route = RouteDecision(route=Route.SALES, category_key=alias.get("target_value"))
            ctx.emit("alias_lookup", hit=True, target_kind=kind)
    except Exception as e:  # noqa: BLE001 — strat gratuit best-effort → miss, nu rupe turul (P6)
        log.warning("alias: lookup eșuat (%s) → miss", type(e).__name__)
