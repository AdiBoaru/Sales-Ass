"""NX-209 shadow orchestrator — deliberately not registered as an agent tool yet."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Mapping

from src.agent.query_spec import RuntimeQuerySpec
from src.db.queries.catalog import get_products_by_ids, has_embeddings, search_products_semantic
from src.db.queries.fusion import fuse_candidates
from src.db.queries.search_entities import (
    load_evidence_references,
    load_identifier_candidates,
    search_shadow_fts,
)
from src.domain.facets import TypedFacet
from src.domain.identifier_resolution import resolve_identifier
from src.domain.rerank_policy import decide_adaptive_rerank
from src.domain.search_entities import SearchEntitiesResult, build_search_entities_result
from src.safety.external_data import external_query_text
from src.tools.reason_codes import annotate as annotate_reasons

log = logging.getLogger(__name__)

#: Câţi candidaţi lexicali cerem înainte de fuziune/rerank. Hidratarea rămâne plafonată la
#: `limit` (contractul `get_products_by_ids`, care apără bugetul de context al agentului), dar
#: `search_shadow_fts` întoarce DOAR id-uri — deci un bazin mai mare e aproape gratis şi e singura
#: cale prin care decizia de rerank vede o listă reală de candidaţi. Cu bazinul egal cu `limit`,
#: „rerank" ar fi însemnat reordonarea a şase elemente deja selectate.
FTS_POOL = 30


async def _hydrate_shadow_products(
    conn: Any, business_id: str, product_ids: list[str], *, pool: int
) -> list[dict]:
    """Hydrate the shadow pool in batches without widening the live helper cap.

    get_products_by_ids intentionally caps each call at six products for agent context. The
    offline shadow still needs the larger lexical/vector pool before fusion, so it batches the
    same tenant-scoped query and preserves retrieval order.
    """
    selected = product_ids[: max(pool, 1)]
    products: list[dict] = []
    for start in range(0, len(selected), 6):
        products.extend(
            await get_products_by_ids(
                conn,
                business_id,
                selected[start : start + 6],
                limit=6,
                respect_content_status=True,
            )
        )
    return products


async def _load_shadow_semantic_products(
    conn: Any,
    business_id: str,
    query_text: str | None,
    llm: Any | None,
    limit: int,
    degradations: list[str],
) -> list[dict]:
    """Calea semantică e degradabilă: orice eşec lasă FTS-ul disponibil.

    Degradarea se ÎNREGISTREAZĂ. Înainte, un `except` fără log întorcea `[]`, iar rezultatul arăta
    identic cu „n-a găsit nimic semantic" — deci un strat mort era indistinct de unul care
    funcţionează şi nu are ce returna. Exact aşa au stat straturile gratuite moarte în producţie.
    """
    if llm is None:
        degradations.append("semantic_skipped_no_llm")
        return []
    if not query_text:
        # `external_query_text` a întors None → textul conţine un identificator de persoană.
        # Codul e fix şi nu conţine nimic din query (P12).
        degradations.append("semantic_blocked_pii")
        return []
    try:
        if not await has_embeddings(conn, business_id, embedding_doc_type="search_document_v1"):
            degradations.append("semantic_skipped_no_shadow_embeddings")
            return []
        query_embedding = (await llm.embed([query_text]))[0]
        return await search_products_semantic(
            conn,
            business_id,
            query_embedding,
            pool=FTS_POOL,
            embedding_doc_type="search_document_v1",
        )
    except Exception:  # noqa: BLE001 — FTS shadow rămâne disponibil la eşec semantic
        log.warning("search_entities_shadow: calea semantică a eşuat (fallback FTS)", exc_info=True)
        degradations.append("semantic_failed")
        return []


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

    Este o funcţie internă pentru benchmark/shadow. Nu are decoratorul `register`, nu schimbă
    `TurnContext` şi nu poate modifica răspunsul live înainte de gate-ul NX-209.
    """
    query_text = query_spec.search_text or query_spec.raw_query
    identifier = resolve_identifier(query_text, await load_identifier_candidates(conn, business_id))
    concerns = [
        constraint.value
        for constraint in query_spec.constraints
        if constraint.facet in {"concern", "concerns"}
        and constraint.op in {"eq", "contains"}
        and isinstance(constraint.value, str)
    ]

    # Iniţializate explicit: înainte, `ids` şi `semantic_products` existau doar pe câte o ramură şi
    # se salvau reciproc prin complementaritatea a două condiţii. Corect azi, `NameError` în ziua
    # în care apare un al patrulea status.
    ids: list[str] = []
    semantic_products: list[dict] = []
    degradations: list[str] = []

    if identifier.status == "resolve":
        # Identificator exact: nici FTS, nici semantic, nici rerank. Cine cere un SKU anume nu vrea
        # „ceva asemănător".
        ids = [identifier.product_id] if identifier.product_id else []
    elif identifier.status == "clarify":
        ids = list(identifier.candidate_ids)
    else:
        ids, semantic_products = await asyncio.gather(
            search_shadow_fts(conn, business_id, query_text, locale=locale, limit=FTS_POOL),
            _load_shadow_semantic_products(
                conn,
                business_id,
                # `detect_on=raw_query` e SEMNAL de detecţie, nu sursă de export: funcţia întoarce
                # ori `normalized_query`, ori None. E necesar fiindcă `normalized_query` vine deja
                # lowercase, iar majuscula e exact ce separă „sunt Ion Popescu" de „sunt cu ten
                # gras" — fără el, tiparul n-ar detecta niciodată nimic în producţie.
                external_query_text(query_spec.normalized_query, detect_on=query_spec.raw_query),
                llm,
                limit,
                degradations,
            ),
        )

    # `respect_content_status=True`: asta E o cale de DISCOVERY (serveşte produse nevăzute), iar
    # toate celelalte căi de discovery filtrează pe `published` (NX-171c). Fără el, shadow-ul putea
    # scoate la suprafaţă produse `draft` — şi ar fi comparat, în benchmark, un univers de candidaţi
    # mai mare decât cel al căii live.
    products = await _hydrate_shadow_products(conn, business_id, ids, pool=FTS_POOL)

    if semantic_products:
        products = fuse_candidates(products, semantic_products, sort_mode="relevance")

    # Apply hard exclusions and soft warnings before the output cap so rejected pool entries do
    # not consume a slot that a valid candidate could fill.
    products = annotate_reasons(products, concerns=concerns)

    # The larger pool is for ranking and coverage only; the shadow contract mirrors the live
    # context budget at its boundary.
    products = products[: min(max(limit, 1), 6)]

    rerank_decision = decide_adaptive_rerank(
        identifier_status=identifier.status,
        constraints=query_spec.constraints,
        lexical_ids=ids,
        semantic_ids=[str(product["id"]) for product in semantic_products],
    )
    product_ids = [str(product["id"]) for product in products]
    evidence = await load_evidence_references(conn, business_id, product_ids, locale=locale)
    return build_search_entities_result(
        products,
        query_spec.constraints,
        facets_by_key,
        evidence_by_product=evidence,
        identifier_status=identifier.status,
        refinement_required=identifier.status == "clarify",
        rerank_decision=rerank_decision,
        degradations=degradations,
    )
