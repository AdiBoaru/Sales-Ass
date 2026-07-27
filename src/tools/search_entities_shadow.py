"""NX-209 shadow orchestrator — deliberately not registered as an agent tool yet."""

from __future__ import annotations

from typing import Any, Mapping

from src.agent.query_spec import RuntimeQuerySpec
from src.db.queries.catalog import get_products_by_ids, has_embeddings, search_products_semantic
from src.db.queries.fusion import fuse_candidates
from src.db.queries.search_entities import load_evidence_references, search_shadow_fts
from src.domain.facets import TypedFacet
from src.domain.search_entities import SearchEntitiesResult, build_search_entities_result


async def search_entities_shadow(
    conn: Any,
    business_id: str,
    query_spec: RuntimeQuerySpec,
    facets_by_key: Mapping[str, TypedFacet],
    *,
    llm: Any | None = None,
    locale: str = "ro",
    limit: int = 6,
) -> SearchEntitiesResult:
    """Rulează FTS shadow → hidratare catalog/evidence → Match Gate, fără side effects.

    Este o funcție internă pentru benchmark/shadow. Nu are decoratorul `register`, nu schimbă
    `TurnContext` și nu poate modifica răspunsul live înainte de gate-ul NX-209.
    """
    query_text = query_spec.search_text or query_spec.raw_query
    ids = await search_shadow_fts(
        conn,
        business_id,
        query_text,
        locale=locale,
        limit=max(limit, 1),
    )
    products = await get_products_by_ids(conn, business_id, ids, limit=min(max(limit, 1), 6))

    semantic_products: list[dict] = []
    if llm is not None:
        try:
            if await has_embeddings(conn, business_id, embedding_doc_type="search_document_v1"):
                query_embedding = (await llm.embed([query_text]))[0]
                semantic_products = await search_products_semantic(
                    conn,
                    business_id,
                    query_embedding,
                    pool=min(max(limit, 1), 6),
                    embedding_doc_type="search_document_v1",
                )
        except Exception:  # noqa: BLE001 — FTS shadow rămâne disponibil la eșec semantic
            semantic_products = []
    if semantic_products:
        products = fuse_candidates(products, semantic_products, sort_mode="relevance")[:limit]

    product_ids = [str(product["id"]) for product in products]
    evidence = await load_evidence_references(conn, business_id, product_ids, locale=locale)
    return build_search_entities_result(
        products,
        query_spec.constraints,
        facets_by_key,
        evidence_by_product=evidence,
    )
