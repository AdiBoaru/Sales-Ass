"""NX-209 — queryuri shadow pentru search documents; nu sunt conectate la tool-ul live."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from src.domain.identifier_resolution import IdentifierCandidate
from src.domain.search_entities import EvidenceReference


async def search_shadow_fts(
    conn: Any,
    business_id: str,
    query_text: str,
    *,
    locale: str = "ro",
    document_version: int = 1,
    limit: int = 50,
) -> list[str]:
    """Product IDs ordonate FTS; tenant/locale/document_version sunt toate predicate explicite.

    Normalizarea e `'simple'` + `ro_unaccent` — ACEEAȘI ca la indexare (`build_search_documents`) și
    ca pe calea lexicală live. 033 e explicit pe de ce: `unaccent()` nu e instalată în baza asta, și
    normalizarea trebuie să fie identică pe ambele capete, altfel potrivirea pur și simplu nu se
    produce."""
    if not query_text.strip():
        return []
    rows = await conn.fetch(
        """
        select d.product_id::text as product_id,
               ts_rank_cd(d.fts_document, websearch_to_tsquery('simple', ro_unaccent($2))) as rank
        from product_search_documents d
        join products p on p.id = d.product_id and p.business_id = d.business_id
        where d.business_id = $1
          and d.locale = $3
          and d.document_version = $4
          and p.status = 'active'
          and d.fts_document @@ websearch_to_tsquery('simple', ro_unaccent($2))
        order by rank desc, d.product_id
        limit $5
        """,
        business_id,
        query_text,
        locale,
        document_version,
        min(max(limit, 1), 100),
    )
    return [str(row["product_id"]) for row in rows]


async def load_evidence_references(
    conn: Any,
    business_id: str,
    product_ids: list[str],
    *,
    locale: str = "ro",
) -> dict[str, tuple[EvidenceReference, ...]]:
    """Hidratează batch referințele evidence, strict pentru produsele și tenantul cerute."""
    if not product_ids:
        return {}
    rows = await conn.fetch(
        """
        select id::text as evidence_id, product_id::text as product_id, role
        from product_evidence_chunks
        where business_id = $1 and product_id = any($2::uuid[]) and locale = $3
        order by product_id, role, id
        """,
        business_id,
        product_ids,
        locale,
    )
    grouped: dict[str, list[EvidenceReference]] = defaultdict(list)
    for row in rows:
        product_id = str(row["product_id"])
        grouped[product_id].append(
            EvidenceReference(
                evidence_id=str(row["evidence_id"]),
                product_id=product_id,
                role=str(row["role"]),
            )
        )
    return {product_id: tuple(refs) for product_id, refs in grouped.items()}


async def load_identifier_candidates(conn: Any, business_id: str) -> list[IdentifierCandidate]:
    """Nume + SKU + numai aliasuri product aprobate, toate strict din tenantul cerut."""
    rows = await conn.fetch(
        """
        select p.id::text as product_id,
               p.name,
               coalesce(
                 array_agg(distinct v.sku) filter (where v.sku is not null and v.sku <> ''),
                 '{}'::text[]
               ) as skus,
               coalesce(
                 array_agg(distinct ia.phrase_norm) filter (where ia.phrase_norm is not null),
                 '{}'::text[]
               ) as aliases
        from products p
        left join product_variants v on v.product_id = p.id and v.business_id = p.business_id
        left join intent_aliases ia on ia.target_id = p.id
          and ia.business_id = p.business_id
          and ia.target_kind = 'product'
          and ia.status = 'approved'
        where p.business_id = $1 and p.status = 'active'
        group by p.id, p.name
        order by p.id
        """,
        business_id,
    )
    return [
        IdentifierCandidate(
            product_id=str(row["product_id"]),
            name=str(row["name"]),
            skus=tuple(row["skus"] or ()),
            aliases=tuple(row["aliases"] or ()),
        )
        for row in rows
    ]
