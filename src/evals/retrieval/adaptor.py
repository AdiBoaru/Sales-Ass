"""NX-203 — adaptor între harness și retrieval-ul REAL (lexical + semantic + RRF).

Leagă `retrieve_fn` al harness-ului de calea de producție (`search_products_lexical` +
`search_products_semantic` + `fuse_candidates`), fără bucla de relaxare și fără arg-extraction —
ca să MĂSOARE retrieval-ul pur, izolat de înțelegerea query-ului (aia e NX-208). Două regimuri:

- `raw`: doar textul brut → hibrid, ZERO filtre. „Ce scoate căutarea pe fraza clientului."
- `with_constraints`: aplică `price_max` + `category` din hard_constraints. „Retrieval cu
  înțelegere perfectă a cererii" (plafonul). Diferența raw↔constrained arată cât ține de
  retrieval și cât de query understanding.

Async (DB + embed). Runner-ul pre-încarcă rezultatele, apoi hrănește harness-ul sincron.
"""

from __future__ import annotations

from typing import Any

import asyncpg

from src.agent.llm import LLMClient
from src.db.queries.catalog import (
    has_embeddings,
    search_products_lexical,
    search_products_semantic,
)
from src.db.queries.fusion import fuse_candidates

_POOL = 50  # același ca _FUSION_POOL din catalog_tools


def _pid(p: dict[str, Any]) -> str:
    return str(p.get("id") or p.get("product_id"))


def _constraints(hard: list[dict[str, Any]] | None) -> tuple[float | None, str | None]:
    """Extrage price_max + category din hard_constraints (pentru regimul `with_constraints`)."""
    price_max = category = None
    for hc in hard or []:
        if hc.get("facet") == "price" and hc.get("op") == "lte":
            price_max = float(hc["value"])
        elif hc.get("facet") == "category" and hc.get("op") == "eq":
            category = str(hc["value"])
    return price_max, category


async def retrieve_products(
    conn: asyncpg.Connection,
    llm: LLMClient | None,
    business_id: str,
    query: str,
    *,
    hard_constraints: list[dict[str, Any]] | None = None,
    apply_constraints: bool = False,
) -> list[str]:
    """Listă ordonată de product_id (cel mai relevant primul), prin calea reală hibrid + RRF.

    Degradare grațioasă: fără embeddings/LLM → lexical-only (ca în producție, P6)."""
    price_max = category = None
    if apply_constraints:
        price_max, category = _constraints(hard_constraints)

    lexical = await search_products_lexical(
        conn,
        business_id,
        query_text=query,
        price_max=price_max,
        category=category,
        pool=_POOL,
    )
    vector: list[dict[str, Any]] = []
    if llm is not None and await has_embeddings(conn, business_id):
        try:
            qvec = (await llm.embed([query]))[0]
            vector = await search_products_semantic(
                conn,
                business_id,
                qvec,
                price_max=price_max,
                category=category,
                pool=_POOL,
            )
        except Exception:  # noqa: BLE001 — embed/semantic pică → lexical-only (P6)
            vector = []

    fused = fuse_candidates(lexical, vector, sort_mode="relevance")
    return [_pid(p) for p in fused]
