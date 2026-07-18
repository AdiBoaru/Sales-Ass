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
import re
from typing import TYPE_CHECKING

from src.cache.canonical import canonicalize
from src.config import get_settings
from src.db.queries.faqs import semantic_lookup, semantic_topk
from src.knowledge.faq_rerank import FaqCandidate, FaqDecision, rerank
from src.models import TurnContext

if TYPE_CHECKING:
    from src.worker.runner import PipelineDeps

log = logging.getLogger(__name__)

# Întrebare CLARĂ de politică/logistică (livrare/plată/retur/garanție), chiar AMESTECATĂ cu interes
# de produs („rituals suna bine, aveti livrare?"). Partea de produs „diluează" embedding-ul
# → similaritatea la FAQ-ul de livrare cade sub faq_tau_high (măsurat ~0.56) și întrebarea NU se
# aprindea niciodată, iar agentul re-recomanda (bug „copy-paste"). Regexul = precizie → permite prag
# FAQ RELAXAT (faq_tau_policy) DOAR pe aceste mesaje. Matchat pe textul CANONIC (fără diacritice, ca
# embedding-ul). Generic (livrare/plată/retur/garanție = politici standard de magazin, nu vertical).
_POLICY_RE = re.compile(
    r"\blivrar\w*|\bin\s+cat\s+timp\b|\bcand\s+ajung\w*|\bcate\s+zile\b|\bcat\s+dureaz\w*"
    r"|\bcurier\w*|\btransport\w*|\bexped\w*|\bramburs\w*|\bretur\w*|\bgarant\w*|\beasybox\b"
    r"|\bcat\s+cost\w*\s+livr|\bmetode\s+de\s+plat\w*",
    re.IGNORECASE,
)


# NX-175: fraza scurtă care introduce chips-urile de clarificare. Chips-urile SUNT întrebările
# candidate → clientul apasă una, iar textul ei re-interoghează la ~0.99 pe acel FAQ (auto-resolve).
_CLARIFY_LEAD = {
    "ro": "Ca să-ți dau răspunsul potrivit, la care te referi?",
    "en": "So I give you the right answer, which do you mean?",
    "hu": "Hogy a megfelelő választ adjam, melyikre gondolsz?",
}


def _clarify_lead(language: str) -> str:
    return _CLARIFY_LEAD.get(language) or _CLARIFY_LEAD["ro"]


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
        canon = canonicalize(body)[0]
        emb = (await deps.llm.embed([canon]))[0]
        model = s.model_embed  # NX-124a: filtrăm pe modelul curent (vectori de alt model = zgomot)
        # NX-175: top-k + rerank (calificatori + marjă) în loc de top-1 orb. Kill-switch OFF →
        # cădem pe top-1 (comportamentul de dinainte), byte-identic pt back-compat.
        clarify: FaqDecision | None = None
        if s.faq_rerank_enabled:
            cands = await semantic_topk(
                deps.conn, ctx.business.id, ctx.language, emb, embedding_model=model, k=s.faq_topk
            )
            decision = rerank(
                canon,
                [
                    FaqCandidate(c["id"], c["question"], c["answer"], float(c["similarity"]))
                    for c in cands
                ],
            )
            if decision.action == "miss":
                hit = None
            elif decision.action == "clarify":
                # NX-175 (fix review Codex): clarify NU ocolește pragul de servire. Marja mică
                # semnalează AMBIGUITATE, dar dacă nici măcar candidatul TOP nu trece `tau`
                # (mai jos), niciun FAQ nu e relevant → miss (lăsăm triajul/agentul). Altfel două
                # FAQ-uri irelevante dar apropiate (ex. 0.70/0.685 < 0.78) ar intercepta ORICE
                # mesaj cu o clarificare falsă, înainte de triaj. `hit` = candidatul TOP pt pragul
                # de jos (`confidence` = cosine ORIGINAL top); servirea clarify-ului se decide DUPĂ.
                top_id, top_q = decision.clarify_options[0]
                hit = {
                    "id": top_id,
                    "question": top_q,
                    "answer": "",
                    "similarity": decision.confidence,
                }
                clarify = decision
            else:  # serve — rerank a ales FAQ-ul; confidence = cosine ORIGINAL pt pragul de mai jos
                hit = {
                    "id": decision.faq_id,
                    "question": decision.question,
                    "answer": decision.answer,
                    "similarity": decision.confidence,
                }
        else:
            hit = await semantic_lookup(
                deps.conn, ctx.business.id, ctx.language, emb, embedding_model=model
            )
        sim = float(hit["similarity"]) if hit else 0.0
        # Prag RELAXAT pe întrebări de politică/livrare (regex = precizie): mesajele mixte
        # („rituals suna bine, aveti livrare?") diluează embedding-ul sub faq_tau_high → altfel
        # întrebarea de livrare pică la agent, care re-recomandă (bug „copy-paste"). Vezi config.
        # NX-138 (R7): relaxarea se aplică DOAR dacă FAQ-ul potrivit e el ÎNSUȘI de politică
        # (întrebarea lui match-uiește regexul). Altfel pragul jos „salva" un FAQ de CONSULTANȚĂ
        # produs pe un mesaj mixt produs+livrare, deflectând cererea de produs (bug live). Un mesaj
        # de politică pe un FAQ de politică = intenția #171, păstrată. Kill-switch fail-open.
        msg_is_policy = _POLICY_RE.search(canon) is not None
        faq_is_policy = (
            hit is not None and _POLICY_RE.search(canonicalize(hit["question"])[0]) is not None
        )
        is_policy = msg_is_policy and (faq_is_policy or not s.faq_policy_gate_on_faq_kind)
        tau = s.faq_tau_policy if is_policy else s.faq_tau_high
        if hit is not None and sim >= tau:
            if clarify is not None:
                # Ambiguitate reală ȘI candidatul top e RELEVANT (>= tau) → NU ghicim: cerem
                # alegerea cu chips = întrebările candidate. Un chip apăsat re-interoghează la
                # ~0.99 pe acel FAQ → auto-rezolvă (fără resume). Necacheabil (specific turului).
                ctx.set_reply(_clarify_lead(ctx.language), cacheable=False)
                ctx.reply.suggestions = [q for _, q in clarify.clarify_options]
                ctx.emit(
                    "faq_clarify",
                    options=[fid for fid, _ in clarify.clarify_options],
                    ranking=clarify.ranking,
                )
                return
            # Cacheable DOAR la hit de încredere mare (tau_high). Hit relaxat pe mesaj MIXT →
            # cacheable=False: query-ul mixt ar otrăvi semantic_cache pt alte mesaje similare.
            ctx.set_reply(hit["answer"], cacheable=(sim >= s.faq_tau_high))
            ctx.emit("faq_hit", faq_id=hit["id"], similarity=round(sim, 4), policy=is_policy)
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
