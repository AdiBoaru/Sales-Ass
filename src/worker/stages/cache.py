"""Stagiul 4 — Cache semantic (G5b-1). Răspunde din cache la query-uri repetate,
ÎNAINTE de triaj/agent → early-exit fără apel LLM de generare.

Două straturi (precision-first, doar tierul `static`):
  • L1 exact: canonical_hash → O(1), zero false-positive.
  • L2 semantic: embed(canonical) → HNSW cosine, auto-accept DOAR la ≥ τ_high.

Query-urile `dynamic`/`realtime` sunt rutate pe lângă cache (bypass) — produsele/
prețurile vin în G5b-2 cu invalidare. Folosește DOAR `embed()` (embeddings), nu
generare (principiul 2). Câmpuri TurnContext scrise: `ctx.reply`, `ctx.from_cache`.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from src.cache.canonical import canonicalize, classify_volatility
from src.config import get_settings
from src.db.queries.semantic_cache import exact_lookup, semantic_lookup, touch_hit
from src.models import TurnContext

if TYPE_CHECKING:
    from src.worker.runner import PipelineDeps

log = logging.getLogger(__name__)


async def cache_stage(ctx: TurnContext, deps: PipelineDeps) -> None:
    settings = get_settings()
    if not settings.cache_enabled:
        return
    body = (ctx.message.body or "").strip()
    if not body:
        return

    volatility = classify_volatility(body)
    if volatility != "static":
        # dynamic/realtime → nu servim din cache în G5b-1 (precision-first).
        ctx.emit("cache_bypass", volatility=volatility)
        return

    canonical, canonical_hash = canonicalize(body)
    if not canonical:
        return

    # Cache-ul e o OPTIMIZARE — orice eroare (migrare neaplicată, DB, embed) →
    # degradează la „miss", NU rupe turul (principiul 6).
    try:
        # L1 exact (O(1), zero false-positive).
        hit = await exact_lookup(deps.conn, ctx.business.id, ctx.language, canonical_hash)
        if hit is not None:
            await touch_hit(deps.conn, ctx.business.id, hit["id"])
            ctx.from_cache = True
            ctx.set_reply(hit["answer"])
            ctx.emit("cache_lookup", layer="exact", volatility=volatility)
            return

        # L2 semantic (paraphrase). Fără LLM → nu putem embed → miss grațios.
        if deps.llm is None:
            ctx.emit("cache_lookup", layer="miss", volatility=volatility)
            return
        embedding = (await deps.llm.embed([canonical]))[0]
        cand = await semantic_lookup(deps.conn, ctx.business.id, ctx.language, embedding)
        similarity = float(cand["similarity"]) if cand else 0.0
        if cand is not None and similarity >= settings.cache_tau_high:
            await touch_hit(deps.conn, ctx.business.id, cand["id"])
            ctx.from_cache = True
            ctx.set_reply(cand["answer"])
            ctx.emit(
                "cache_lookup",
                layer="semantic",
                similarity=round(similarity, 4),
                volatility=volatility,
            )
            return
        # sub prag → miss (gray-zone verify = faza 2).
        ctx.emit(
            "cache_lookup", layer="miss", similarity=round(similarity, 4), volatility=volatility
        )
    except Exception as e:  # noqa: BLE001 — cache best-effort: orice eroare → miss
        log.warning("cache: lookup eșuat (%s) → miss", type(e).__name__)
