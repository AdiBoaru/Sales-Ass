"""Stagiul 4 — Cache semantic (G5b). Răspunde din cache la query-uri repetate,
ÎNAINTE de triaj/agent → early-exit fără apel LLM de generare.

Două straturi (precision-first):
  • L1 exact: canonical_hash → O(1), zero false-positive.
  • L2 semantic: embed(canonical) → HNSW cosine, auto-accept DOAR la ≥ τ_high.

Tiere de volatilitate (canonical.classify_volatility):
  • `static` (FAQ/generic) — servit/scris în G5b-1.
  • `dynamic` (recomandări de produs) — servit în G5b-2 cu price-check self-healing:
    înainte de a servi un hit dynamic re-validăm prețul curent al produselor din
    `retrieval_signature` + `data_version`-ul businessului; orice diferență → entry
    învechit, evict lazy + tratează ca MISS (pipeline-ul regenerează cu preț proaspăt).
  • `realtime` (comandă/personal) — bypass (răspuns specific userului, niciodată cache).

Folosește DOAR `embed()` (embeddings), nu generare (principiul 2). Câmpuri TurnContext
scrise: `ctx.reply`, `ctx.from_cache`.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from src.agent.prompt_builder import prompt_vnext_effective
from src.cache.canonical import canonicalize, classify_volatility
from src.config import get_settings
from src.db.queries.businesses import get_data_version
from src.db.queries.semantic_cache import (
    current_prices,
    delete_entry,
    exact_lookup,
    semantic_lookup,
    touch_hit,
)
from src.models import TurnContext
from src.safety.policy import SafetyPolicy

if TYPE_CHECKING:
    from src.worker.runner import PipelineDeps

log = logging.getLogger(__name__)

# Toleranță de comparare a prețurilor: bani (2 zecimale) — un sub-cent nu regenerează.
_PRICE_EPS = 0.005


async def _is_fresh_dynamic(ctx: TurnContext, deps: PipelineDeps, entry: dict[str, Any]) -> bool:
    """Price-check self-healing pe un candidat de hit dynamic. True = poate fi servit;
    False = învechit (semnătură coruptă, data_version diferit, sau orice preț schimbat)."""
    sig = entry.get("retrieval_signature")
    if not sig:  # semnătură goală/None pe un entry dynamic = corupt → evict
        return False
    if entry.get("data_version") != await get_data_version(deps.conn, ctx.business.id):
        return False
    pids = [s["product_id"] for s in sig]
    current = await current_prices(deps.conn, ctx.business.id, pids)
    for s in sig:
        cur = current.get(s["product_id"])
        if cur is None or abs(cur - float(s["price"])) > _PRICE_EPS:
            return False
    return True


async def _serve(
    ctx: TurnContext,
    deps: PipelineDeps,
    entry: dict[str, Any],
    volatility: str,
    *,
    layer: str,
    similarity: float | None = None,
) -> bool:
    """Servește un candidat de hit. Pe `dynamic` aplică price-check ÎNAINTE: dacă e
    învechit → evict lazy + emit `stale_evict` + întoarce False (tratat ca miss).
    Pe hit valid: setează reply + from_cache, touch_hit, emit `cache_lookup`."""
    if volatility == "dynamic" and not await _is_fresh_dynamic(ctx, deps, entry):
        await delete_entry(deps.conn, ctx.business.id, entry["id"])
        ctx.emit("cache_lookup", layer="stale_evict", volatility=volatility)
        return False
    await touch_hit(deps.conn, ctx.business.id, entry["id"])
    ctx.from_cache = True
    ctx.set_reply(entry["answer"])
    props: dict[str, Any] = {"layer": layer, "volatility": volatility}
    if similarity is not None:
        props["similarity"] = round(similarity, 4)
    ctx.emit("cache_lookup", **props)
    return True


async def cache_stage(ctx: TurnContext, deps: PipelineDeps) -> None:
    settings = get_settings()
    if not settings.cache_enabled:
        return
    if ctx.route is not None:
        return  # upstream determinist (alias/clarify_resume) a rutat deja → nu deflectăm (P3)
    body = (ctx.message.body or "").strip()
    if not body:
        return

    volatility = classify_volatility(body)
    if volatility in ("realtime", "contextual"):
        # realtime: comandă/personal → răspuns specific userului. contextual: refinare
        # relativă la setul afișat („mai ieftin") → un hit din cache-ul partajat ar servi
        # răspunsul altui client (alt baseline). Ambele bypass: niciodată din cache, lasă
        # turul la agent (acolo `cheaper_intent` tratează „mai ieftin" determinist).
        ctx.emit("cache_bypass", volatility=volatility)
        return

    # NX-173 (P0): context de siguranță declarat → BYPASS, aceeași logică ca `contextual`, dar
    # miza e siguranța, nu relevanța. Cache-ul e stagiul 4: rulează ÎNAINTE de triaj/agent, deci un
    # hit face early-exit peste TOT gate-ul de contraindicații. Găsit live: „sunt însărcinată, ce
    # cremă antirid pot folosi?" era servit din `semantic_cache` cu `route=None` — un răspuns
    # compus într-un tur ANTERIOR (posibil dinaintea gate-ului), refolosit la nesfârșit pentru toți
    # clienții cu aceeași frază. Un răspuns de siguranță e relativ la CLIENT, nu la query.
    # (Scrierea e blocată separat: `safety/compose.enforce` pune `cacheable=False`.)
    if SafetyPolicy.for_turn(ctx).contexts:
        ctx.emit("cache_bypass", volatility="safety_context")
        return

    canonical, canonical_hash = canonicalize(body)
    if not canonical:
        return

    # NX-181: namespace de cache pe versiunea de prompt (v1 vs vNext), determinat ÎNAINTE de lookup.
    # Prompt vNext ON compune răspunsuri diferite → nu servi/scrie pe intrări v1 (și invers). OFF →
    # 'v1' (neschimbat). Flag EFECTIV per business (single source: prompt_vnext_effective).
    prompt_version = "vnext" if prompt_vnext_effective(ctx.business) else "v1"

    # Cache-ul e o OPTIMIZARE — orice eroare (migrare neaplicată, DB, embed) →
    # degradează la „miss", NU rupe turul (principiul 6).
    try:
        # L1 exact (O(1), zero false-positive).
        hit = await exact_lookup(
            deps.conn,
            ctx.business.id,
            ctx.language,
            canonical_hash,
            volatility_class=volatility,
            prompt_version=prompt_version,
        )
        if hit is not None and await _serve(ctx, deps, hit, volatility, layer="exact"):
            return

        # L2 semantic (paraphrase). Fără LLM → nu putem embed → miss grațios.
        if deps.llm is None:
            ctx.emit("cache_lookup", layer="miss", volatility=volatility)
            return
        embedding = (await deps.llm.embed([canonical]))[0]
        cand = await semantic_lookup(
            deps.conn,
            ctx.business.id,
            ctx.language,
            embedding,
            volatility_class=volatility,
            embedding_model=settings.model_embed,
            prompt_version=prompt_version,
        )
        similarity = float(cand["similarity"]) if cand else 0.0
        if (
            cand is not None
            and similarity >= settings.cache_tau_high
            and await _serve(ctx, deps, cand, volatility, layer="semantic", similarity=similarity)
        ):
            return
        # sub prag SAU evict pe price-check → miss (gray-zone verify = faza 2).
        ctx.emit(
            "cache_lookup", layer="miss", similarity=round(similarity, 4), volatility=volatility
        )
    except Exception as e:  # noqa: BLE001 — cache best-effort: orice eroare → miss
        log.warning("cache: lookup eșuat (%s) → miss", type(e).__name__)
