"""NX-209 shadow orchestrator — deliberately not registered as an agent tool yet."""

from __future__ import annotations

from typing import Any, Mapping

from src.agent.query_spec import RuntimeQuerySpec
from src.db.queries.catalog import get_products_by_ids
from src.db.queries.search_entities import load_evidence_references, search_shadow_fts
from src.domain.facets import TypedFacet
from src.domain.search_entities import SearchEntitiesResult, build_search_entities_result


async def search_entities_shadow(
    conn: Any,
    business_id: str,
    query_spec: RuntimeQuerySpec,
    facets_by_key: Mapping[str, TypedFacet],
    *,
    locale: str = "ro",
    limit: int = 6,
) -> SearchEntitiesResult:
    """Rulează FTS shadow → hidratare catalog/evidence → Match Gate, fără side effects.

    Este o funcție internă pentru benchmark/shadow. Nu are decoratorul `register`, nu schimbă
    `TurnContext` și nu poate modifica răspunsul live înainte de gate-ul NX-209.
    """
    ids = await search_shadow_fts(
        conn,
        business_id,
        query_spec.search_text or query_spec.raw_query,
        locale=locale,
        limit=max(limit, 1),
    )
    products = await get_products_by_ids(conn, business_id, ids, limit=min(max(limit, 1), 6))
    product_ids = [str(product["id"]) for product in products]
    evidence = await load_evidence_references(conn, business_id, product_ids, locale=locale)
    return build_search_entities_result(
        products,
        query_spec.constraints,
        facets_by_key,
        evidence_by_product=evidence,
    )
