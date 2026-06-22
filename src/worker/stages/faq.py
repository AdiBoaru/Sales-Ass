"""Stagiul 4 — Strat gratuit FAQ (NX-74). Răspunde la întrebări de cunoștințe
(retur / livrare / garanție / plată / facturare) din `faqs`, ÎNAINTE de triaj/agent
→ early-exit fără apel LLM de generare.

Stagiu separat de `cache.py` (separarea proprietarilor): `cache_stage` deține
`semantic_cache`, `faq_stage` deține `faqs`. Plasare DUPĂ cache (cel mai ieftin,
O(1) pe exact) și ÎNAINTE de triaj — ordinea: cache → FAQ (un embed) → triaj.

Folosește DOAR `embed()` (embeddings), niciodată generare (principiul 2). Best-effort
ca G5b: orice eroare (migrare neaplicată, DB, embed) → degradare la „miss", NU rupe
turul (principiul 6). Câmpuri TurnContext scrise: `ctx.reply` (via set_reply) +
`ctx.events` (via emit). NU scrie route/retrieval/from_cache — nu e proprietarul lor.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from src.cache.canonical import canonicalize
from src.config import get_settings
from src.db.queries.faqs import semantic_lookup
from src.models import TurnContext

if TYPE_CHECKING:
    from src.worker.runner import PipelineDeps

log = logging.getLogger(__name__)


async def faq_stage(ctx: TurnContext, deps: PipelineDeps) -> None:
    s = get_settings()
    if not s.faq_enabled:
        return
    if ctx.route is not None:
        return  # upstream determinist (alias/clarify_resume) a rutat deja → nu deflectăm (P3)
    body = (ctx.message.body or "").strip()
    if not body or deps.llm is None:  # fără LLM nu putem embed → miss grațios
        return
    try:
        # NX-124a: embed pe `canonicalize(body)` (fără diacritice + punctuație) — paritate cu seed
        # FAQ (care embed-uiește tot canonical) → „cat e livrarea" matchează „Cât costă livrarea?".
        emb = (await deps.llm.embed([canonicalize(body)[0]]))[0]
        model = s.model_embed  # NX-124a: filtrăm pe modelul curent (vectori de alt model = zgomot)
        hit = await semantic_lookup(
            deps.conn, ctx.business.id, ctx.language, emb, embedding_model=model
        )
        sim = float(hit["similarity"]) if hit else 0.0
        if hit is not None and sim >= s.faq_tau_high:
            # Răspuns static reutilizabil → cacheable (G5b îl poate prinde data viitoare).
            ctx.set_reply(hit["answer"])
            ctx.emit("faq_hit", faq_id=hit["id"], similarity=round(sim, 4))
            return
        # NX-124a: fallback de locale (gated) — user pe o limbă fără cunoștințe seedate, dar
        # `default_locale` le are. Prag STRICT (precision-first; NU traducem, servim cunoștința
        # existentă unui user care a scris în limba aia dar conv.locale diferă). P6: mai bine
        # cunoștința corectă din altă limbă decât deflecție 0%.
        default_locale = ctx.business.default_locale
        if s.faq_locale_fallback_enabled and default_locale and ctx.language != default_locale:
            fb = await semantic_lookup(
                deps.conn, ctx.business.id, default_locale, emb, embedding_model=model
            )
            fb_sim = float(fb["similarity"]) if fb else 0.0
            if fb is not None and fb_sim >= s.faq_fallback_tau:
                # cacheable=False: răspunsul e în `default_locale`, nu în `ctx.language` — cache-uit
                # ar fi scris de write-back sub locale-ul userului → otrăvire cross-locale (viitorul
                # user pe acea limbă ar primi răspuns din altă limbă, fără pragul strict).
                ctx.set_reply(fb["answer"], cacheable=False)
                ctx.emit(
                    "faq_hit", faq_id=fb["id"], similarity=round(fb_sim, 4), locale_fallback=True
                )
                return
            # Nici fallback-ul nu prinde → semnal că trebuie seedate cunoștințe în limba userului.
            ctx.emit("locale_unserved", locale=ctx.language, similarity=round(sim, 4))
            return
        ctx.emit("faq_lookup", layer="miss", similarity=round(sim, 4))
    except Exception as e:  # noqa: BLE001 — FAQ best-effort: orice eroare → miss
        log.warning("faq: lookup eșuat (%s) → miss", type(e).__name__)
