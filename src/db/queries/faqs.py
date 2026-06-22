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


async def semantic_lookup(
    conn: asyncpg.Connection,
    business_id: str,
    locale: str,
    embedding: list[float],
    *,
    embedding_model: str,
    limit: int = 1,  # noqa: ARG001 — v1 întoarce mereu cel mai apropiat rând
) -> dict[str, Any] | None:
    """Cel mai apropiat FAQ activ (cosine) pe `(business_id, locale)`. None la 0 rânduri.
    Întoarce `{id, question, answer, similarity}`; caller-ul aplică pragul de similaritate.

    `embedding is not null` în WHERE exclude rândurile ne-embed-uite (seed parțial) — ele nu
    pot fi ordonate cosine oricum. NX-124a: filtru pe `embedding_model` — vectori dintr-un alt
    model (dim/spațiu diferit) nu se compară cosine cu query-ul curent (P11)."""
    row = await conn.fetchrow(
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
        limit 1
        """,
        business_id,
        locale,
        _vec(embedding),
        embedding_model,
    )
    return dict(row) if row else None
