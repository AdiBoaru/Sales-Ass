"""Query-uri pe `faqs` — stratul gratuit 4 (NX-74).

Un singur lookup semantic: cel mai apropiat FAQ ACTIV (cosine) filtrat pe
`business_id + locale`. Tiparul oglindește `semantic_cache.semantic_lookup`
(aceeași convenție `_vec()`, același filtru de tenant + locale).

Stratul de date NU apelează LLM — primește vectorul gata calculat (principiul 2).
`conn` e DEJA tenant-scoped (tenant_conn). RLS pe `bot_runtime` (003) e plasa: un
query fără filtru de tenant → 0 rânduri, nu datele altui client (principiul 7).
`locale = $2` în WHERE — un hit FAQ în limba greșită e un BUG (principiul 11).
"""

from typing import Any

import asyncpg


def _vec(embedding: list[float]) -> str:
    """list[float] → literalul pgvector `[a,b,c]` (ca în semantic_cache / search_products)."""
    return "[" + ",".join(f"{x:.7f}" for x in embedding) + "]"


async def semantic_topk(
    conn: asyncpg.Connection,
    business_id: str,
    locale: str,
    embedding: list[float],
    *,
    embedding_model: str,
    k: int = 5,
) -> list[dict[str, Any]]:
    """Cei mai apropiați K candidați FAQ activi (cosine) pe `(business_id, locale)`, în ordine
    descrescătoare de similaritate. `[]` la 0 rânduri.

    NX-175: top-1 orb rata cazul în care întrebarea generică e mai aproape de o EXCEPȚIE decât de
    procedura generală (măsurat: marjă 0.026). Reranking-ul (calificatori + marjă) are nevoie de
    candidați, nu de un singur rând. `semantic_lookup` (mai jos) rămâne wrapper thin pt back-compat.

    `embedding is not null` exclude rândurile ne-embed-uite (seed parțial). Filtru pe
    `embedding_model` — vectori din alt model nu se compară cosine (P11)."""
    rows = await conn.fetch(
        """
        select id::text as id, question, answer,
               1 - (embedding <=> $3::vector) as similarity
        from faqs
        where business_id = $1
          and locale = $2
          and is_active = true
          and embedding is not null
          and embedding_model = $4
        order by embedding <=> $3::vector
        limit $5
        """,
        business_id,
        locale,
        _vec(embedding),
        embedding_model,
        k,
    )
    return [dict(r) for r in rows]


async def semantic_lookup(
    conn: asyncpg.Connection,
    business_id: str,
    locale: str,
    embedding: list[float],
    *,
    embedding_model: str,
    limit: int = 1,  # noqa: ARG001 — wrapper thin peste `semantic_topk` (back-compat)
) -> dict[str, Any] | None:
    """Cel mai apropiat FAQ activ (cosine). Wrapper back-compat peste `semantic_topk` — codul nou
    (rerank NX-175) folosește direct top-k; ăsta rămâne pt apelanții care vor doar top-1."""
    rows = await semantic_topk(
        conn, business_id, locale, embedding, embedding_model=embedding_model, k=1
    )
    return rows[0] if rows else None
