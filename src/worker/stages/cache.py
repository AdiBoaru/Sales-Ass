"""Stagiul 4 — Cache de răspunsuri (G5b). Răspunde din cache la query-uri repetate,
ÎNAINTE de agent → early-exit fără apel de model.

Un singur strat: potrivire EXACTĂ pe `canonical_hash` (O(1), zero false-positive). Stratul
semantic (embed + cosine) a fost scos odată cu embeddings (2026-09-24): pe 30 de zile servise
0 răspunsuri din 92 de încercări, iar singurul hit al perioadei fusese EXACT. Costa în schimb un
apel de rețea pe fiecare tur.

Tiere de volatilitate (canonical.classify_volatility):
  • `static` (FAQ/generic) — servit/scris în G5b-1.
  • `dynamic` (recomandări de produs) — servit în G5b-2 cu price-check self-healing:
    înainte de a servi un hit dynamic re-validăm prețul curent al produselor din
    `retrieval_signature` + `data_version`-ul businessului; orice diferență → entry
    învechit, evict lazy + tratează ca MISS (pipeline-ul regenerează cu preț proaspăt).
  • `realtime` (comandă/personal) — bypass (răspuns specific userului, niciodată cache).

Niciun apel de model. Câmpuri TurnContext scrise: `ctx.reply`, `ctx.from_cache`.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from src.cache.canonical import canonicalize, classify_volatility
from src.cache.version import cache_prompt_version
from src.config import get_settings
from src.db.queries.businesses import get_data_version
from src.db.queries.semantic_cache import (
    current_prices,
    delete_entry,
    exact_lookup,
    touch_hit,
)
from src.models import TurnContext
from src.safety.policy import SafetyPolicy

if TYPE_CHECKING:
    from src.worker.runner import PipelineDeps

log = logging.getLogger(__name__)

# NX-239: contractul de fast-path (citit de `control_plane`): un hit de cache acoperă O singură
# obligație de răspuns — cache-ul semantic NU early-exit-ează un mesaj mixt sub single-brain
# (răspunsul cache-uit al altei fraze nu poate dovedi acoperirea obligațiilor de acum).
FAST_PATH_COVERS: tuple[str, ...] = ("question_0",)

# Toleranță de comparare a prețurilor: bani (2 zecimale) — un sub-cent nu regenerează.
_PRICE_EPS = 0.005


async def _is_fresh_dynamic(ctx: TurnContext, conn: Any, entry: dict[str, Any]) -> bool:
    """Price-check self-healing pe un candidat de hit dynamic. True = poate fi servit;
    False = învechit (semnătură coruptă, data_version diferit, sau orice preț schimbat).

    NX-231: primește `conn` (nu `deps`) fiindcă rulează ÎNĂUNTRUL checkout-ului deschis de
    apelant — price-check + evict + touch sunt aceeași operație, nu trei checkout-uri."""
    sig = entry.get("retrieval_signature")
    if not sig:  # semnătură goală/None pe un entry dynamic = corupt → evict
        return False
    if entry.get("data_version") != await get_data_version(conn, ctx.business.id):
        return False
    pids = [s["product_id"] for s in sig]
    current = await current_prices(conn, ctx.business.id, pids)
    for s in sig:
        cur = current.get(s["product_id"])
        if cur is None or abs(cur - float(s["price"])) > _PRICE_EPS:
            return False
    return True


async def _serve(
    ctx: TurnContext,
    conn: Any,
    entry: dict[str, Any],
    volatility: str,
    *,
    layer: str,
) -> bool:
    """Servește un candidat de hit. Pe `dynamic` aplică price-check ÎNAINTE: dacă e
    învechit → evict lazy + emit `stale_evict` + întoarce False (tratat ca miss).
    Pe hit valid: setează reply + from_cache, touch_hit, emit `cache_lookup`."""
    if volatility == "dynamic" and not await _is_fresh_dynamic(ctx, conn, entry):
        await delete_entry(conn, ctx.business.id, entry["id"])
        ctx.emit("cache_lookup", layer="stale_evict", volatility=volatility)
        return False
    await touch_hit(conn, ctx.business.id, entry["id"])
    ctx.from_cache = True
    ctx.set_reply(entry["answer"])
    ctx.emit("cache_lookup", layer=layer, volatility=volatility)
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

    # Cache-ul e o OPTIMIZARE — orice eroare (migrare neaplicată, DB) →
    # degradează la „miss", NU rupe turul (principiul 6).
    # NX-216: namespace-ul de prompt e dimensiune de cheie. Se determină ÎNAINTE de lookup și e
    # ACEEAȘI sursă ca la write-back (aftercare) → nu servim un răspuns compus cu alt prompt.
    prompt_version = cache_prompt_version(ctx.business)

    try:
        # Checkout scurt: lookup + price-check + touch sunt aceeași operație.
        async with deps.db("cache_exact_lookup") as conn:
            hit = await exact_lookup(
                conn,
                ctx.business.id,
                ctx.language,
                canonical_hash,
                volatility_class=volatility,
                prompt_version=prompt_version,
            )
            if hit is not None and await _serve(ctx, conn, hit, volatility, layer="exact"):
                return
        # Lipsă SAU evict pe price-check → miss.
        ctx.emit("cache_lookup", layer="miss", volatility=volatility)
    except Exception as e:  # noqa: BLE001 — cache best-effort: orice eroare → miss
        log.warning("cache: lookup eșuat (%s) → miss", type(e).__name__)
