"""Tool de cunoștințe (NX-74) — `faq_lookup`: un fapt de business pe ruta SALES.

Pe ruta de vânzare agentul poate avea nevoie de o regulă de business („pot plăti ramburs?",
„cât e livrarea?") în mijlocul unei recomandări. `faq_lookup` refolosește ACELAȘI query ca
stratul gratuit (`faqs.semantic_lookup`); `business_id`/`locale` din `ctx`, NU din args (P7,
P11). FAQ-ul nu produce produse → `products=[]`, doar `llm_view` (textul faptului) pentru model.

Pragul de tool e mai relaxat decât pragul de strat gratuit (agentul parafrazează oricum
răspunsul în context → un match aproximativ e util). Doar `embed()`, niciodată generare (P2).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from src.cache.canonical import canonicalize
from src.config import get_settings
from src.db.queries.faqs import semantic_lookup, semantic_topk
from src.knowledge.faq_rerank import FaqCandidate, rerank
from src.tools.base import ToolResult, register

if TYPE_CHECKING:
    from src.models import TurnContext
    from src.worker.runner import PipelineDeps


class FaqArgs(BaseModel):
    query: str = Field(min_length=1, max_length=400)


@register("faq_lookup")
async def faq_lookup_tool(ctx: TurnContext, deps: PipelineDeps, args: dict[str, Any]) -> ToolResult:
    """Caută cel mai apropiat fapt de business din `faqs` (cosine, scoped pe contact-ul de
    business + locale). Miss/fără LLM → `llm_view` neutru (NU inventa o regulă)."""
    a = FaqArgs(**args)
    if deps.llm is None:
        return ToolResult(ok=False, error="no_llm", llm_view="FAQ indisponibil.")
    s = get_settings()
    canon = canonicalize(a.query)[0]  # NX-124a: paritate cu seed FAQ
    emb = (await deps.llm.embed([canon]))[0]
    # NX-175: același rerank ca stagiul gratuit (calificatori) → tool-ul nu mai poate întoarce
    # excepția „produs desfăcut" pe o întrebare generică de retur. Tool-ul NU clarifică (agentul
    # gestionează dialogul) — la ambiguitate/miss întoarce vederea neutră. Kill-switch → top-1.
    if s.faq_rerank_enabled:
        cands = await semantic_topk(
            deps.conn,
            ctx.business.id,
            ctx.language,
            emb,
            embedding_model=s.model_embed,
            k=s.faq_topk,
        )
        decision = rerank(
            canon,
            [
                FaqCandidate(c["id"], c["question"], c["answer"], float(c["similarity"]))
                for c in cands
            ],
        )
        hit = (
            {"answer": decision.answer, "similarity": decision.confidence}
            if decision.action == "serve"
            else None
        )
    else:
        hit = await semantic_lookup(
            deps.conn, ctx.business.id, ctx.language, emb, embedding_model=s.model_embed
        )
    if hit is None or float(hit["similarity"]) < s.faq_tau_tool:
        return ToolResult(
            ok=True,
            llm_view=(
                "Nu am un răspuns în baza de cunoștințe; spune-i clientului că verifici cu "
                "un coleg dacă insistă."
            ),
        )
    return ToolResult(ok=True, llm_view=hit["answer"])
