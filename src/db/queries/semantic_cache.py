"""Query-uri pe `semantic_cache` — stratul gratuit 4 (G5b).

Două straturi de lookup (vezi docs/semantic-cache-design.md §2):
  • L1 exact: `(business_id, locale, canonical_hash)` → O(1), zero false-positive.
  • L2 semantic: embed → HNSW cosine, filtru `business_id + locale + neexpirat`.

Write-back-ul (gated) face upsert idempotent pe `(business_id, locale, canonical_hash)`.
RLS pe `bot_runtime` (003) e plasa: lookup fără filtru de tenant → 0 rânduri, nu
datele altui client (NX-50/04). `conn` trebuie să fie tenant-scoped (tenant_conn).

G5b-1 servește/scrie DOAR `volatility_class = 'static'`; `dynamic` vine cu G5b-2.
"""

from typing import Any

import asyncpg


def _vec(embedding: list[float]) -> str:
    """list[float] → literalul pgvector `[a,b,c]` (ca în search_products_semantic)."""
    return "[" + ",".join(f"{x:.7f}" for x in embedding) + "]"


async def exact_lookup(
    conn: asyncpg.Connection,
    business_id: str,
    locale: str,
    canonical_hash: str,
) -> dict[str, Any] | None:
    """L1 exact: entry static neexpirat pentru hash-ul canonic. None la miss."""
    row = await conn.fetchrow(
        """
        select id::text as id, answer
        from semantic_cache
        where business_id = $1
          and locale = $2
          and canonical_hash = $3
          and volatility_class = 'static'
          and expires_at > now()
        limit 1
        """,
        business_id,
        locale,
        canonical_hash,
    )
    return dict(row) if row else None


async def semantic_lookup(
    conn: asyncpg.Connection,
    business_id: str,
    locale: str,
    embedding: list[float],
) -> dict[str, Any] | None:
    """L2 semantic: cel mai apropiat entry static (cosine). Întoarce
    `{id, answer, similarity}` sau None. Caller-ul aplică pragul τ_high."""
    row = await conn.fetchrow(
        """
        select id::text as id, answer,
               1 - (embedding <=> $3::vector) as similarity
        from semantic_cache
        where business_id = $1
          and locale = $2
          and volatility_class = 'static'
          and expires_at > now()
        order by embedding <=> $3::vector
        limit 1
        """,
        business_id,
        locale,
        _vec(embedding),
    )
    return dict(row) if row else None


async def touch_hit(conn: asyncpg.Connection, business_id: str, entry_id: str) -> None:
    """Marchează un hit: hit_count+1, last_hit_at=now() (LRU/LFU + analytics)."""
    await conn.execute(
        """
        update semantic_cache
           set hit_count = hit_count + 1, last_hit_at = now()
         where business_id = $1 and id = $2
        """,
        business_id,
        entry_id,
    )


async def upsert_entry(
    conn: asyncpg.Connection,
    business_id: str,
    locale: str,
    *,
    canonical_str: str,
    canonical_hash: str,
    embedding: list[float],
    answer: str,
    volatility_class: str,
    embedding_model: str,
    quality_score: float,
    ttl_days: int,
) -> None:
    """Write-back idempotent pe `(business_id, locale, canonical_hash)`. Reîmprospătează
    answer+embedding+expires_at dacă entry-ul exista (paraphrase nou pe același canonic)."""
    await conn.execute(
        """
        insert into semantic_cache
            (business_id, locale, query_norm, canonical_hash, embedding, answer,
             volatility_class, embedding_model, quality_score,
             expires_at)
        values
            ($1, $2, $3, $4, $5::vector, $6, $7, $8, $9,
             now() + make_interval(days => $10))
        on conflict (business_id, locale, canonical_hash) do update
            set answer = excluded.answer,
                embedding = excluded.embedding,
                quality_score = excluded.quality_score,
                expires_at = excluded.expires_at
        """,
        business_id,
        locale,
        canonical_str,
        canonical_hash,
        _vec(embedding),
        answer,
        volatility_class,
        embedding_model,
        quality_score,
        ttl_days,
    )
