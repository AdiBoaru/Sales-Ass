"""Query-uri pe `semantic_cache` — stratul gratuit 4 (G5b).

Două straturi de lookup (vezi docs/semantic-cache-design.md §2):
  • L1 exact: `(business_id, locale, canonical_hash)` → O(1), zero false-positive.
  • L2 semantic: embed → HNSW cosine, filtru `business_id + locale + neexpirat`.

Write-back-ul (gated) face upsert idempotent pe `(business_id, locale, canonical_hash)`.
RLS pe `bot_runtime` (003) e plasa: lookup fără filtru de tenant → 0 rânduri, nu
datele altui client (NX-50/04). `conn` trebuie să fie tenant-scoped (tenant_conn).

G5b-1 servește/scrie tierul `static`; G5b-2 deblochează `dynamic` (recomandări de
produs) cu invalidare: `retrieval_signature` (snapshot de preț) + `data_version`.
Lookup-urile parametrizate pe `volatility_class` returnează provenance-ul pentru
price-check (NULL pentru entry-urile static).
"""

import json
from typing import Any

import asyncpg

# Prețul efectiv (min variantă, fallback la products) — ACELAȘI ca în search_products
# (catalog._EFFECTIVE_PRICE). Price-check-ul trebuie să vadă exact prețul oferit clientului.
_EFFECTIVE_PRICE = "coalesce(vp.price, p.sale_price, p.price)"


def _vec(embedding: list[float]) -> str:
    """list[float] → literalul pgvector `[a,b,c]` (ca în search_products_semantic)."""
    return "[" + ",".join(f"{x:.7f}" for x in embedding) + "]"


def _row(row: asyncpg.Record | None) -> dict[str, Any] | None:
    """Record → dict, cu `retrieval_signature` parsat din jsonb (asyncpg îl dă text)."""
    if row is None:
        return None
    d = dict(row)
    sig = d.get("retrieval_signature")
    if isinstance(sig, str):
        d["retrieval_signature"] = json.loads(sig)
    return d


async def exact_lookup(
    conn: asyncpg.Connection,
    business_id: str,
    locale: str,
    canonical_hash: str,
    *,
    volatility_class: str = "static",
    prompt_version: str = "v1",
) -> dict[str, Any] | None:
    """L1 exact: entry neexpirat din clasa cerută pentru hash-ul canonic. None la miss.
    Întoarce și `retrieval_signature`+`data_version` (provenance pt price-check dynamic).

    NX-124a: L1 NU filtrează pe `embedding_model` — match pe hash-ul canonic (cheia de precizie),
    iar `answer`-ul servit e TEXT, independent de model (vectorul stocat nu e citit pe un hit L1).
    Filtrul de model trăiește DOAR pe calea cosine (`semantic_lookup`), unde contează spațiul."""
    row = await conn.fetchrow(
        """
        select id::text as id, answer, retrieval_signature, data_version
        from semantic_cache
        where business_id = $1
          and locale = $2
          and canonical_hash = $3
          and volatility_class = $4
          and prompt_version = $5
          and expires_at > now()
        limit 1
        """,
        business_id,
        locale,
        canonical_hash,
        volatility_class,
        prompt_version,
    )
    return _row(row)


async def semantic_lookup(
    conn: asyncpg.Connection,
    business_id: str,
    locale: str,
    embedding: list[float],
    *,
    volatility_class: str = "static",
    embedding_model: str,
    prompt_version: str = "v1",
) -> dict[str, Any] | None:
    """L2 semantic: cel mai apropiat entry din clasa cerută (cosine). Întoarce
    `{id, answer, similarity, retrieval_signature, data_version}` sau None. Caller-ul
    aplică pragul τ_high (și, pe dynamic, price-check-ul).

    NX-124a: filtru OBLIGATORIU pe `embedding_model` — ordonarea cosine pe vectori din alt model
    (dim/spațiu diferit) e zgomot. Un upgrade de embeddings nu mai amestecă spațiile (P11)."""
    row = await conn.fetchrow(
        """
        select id::text as id, answer, retrieval_signature, data_version,
               1 - (embedding <=> $3::vector) as similarity
        from semantic_cache
        where business_id = $1
          and locale = $2
          and volatility_class = $4
          and embedding_model = $5
          and prompt_version = $6
          and expires_at > now()
        order by embedding <=> $3::vector
        limit 1
        """,
        business_id,
        locale,
        _vec(embedding),
        volatility_class,
        embedding_model,
        prompt_version,
    )
    return _row(row)


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
    ttl_days: int = 0,
    ttl_minutes: int = 0,
    retrieval_signature: list[dict[str, Any]] | None = None,
    data_version: int | None = None,
    prompt_version: str = "v1",
) -> None:
    """Write-back idempotent pe `(business_id, locale, canonical_hash)`. Reîmprospătează
    answer+embedding+clasă+provenance+expires_at dacă entry-ul exista (paraphrase nou pe
    același canonic). TTL = days (static, 7z) SAU minutes (dynamic, backstop scurt);
    `retrieval_signature`/`data_version` se setează DOAR pentru tierul dynamic (G5b-2)."""
    await conn.execute(
        """
        insert into semantic_cache
            (business_id, locale, query_norm, canonical_hash, embedding, answer,
             volatility_class, embedding_model, quality_score,
             retrieval_signature, data_version, prompt_version, expires_at)
        values
            ($1, $2, $3, $4, $5::vector, $6, $7, $8, $9, $10::jsonb, $11, $12,
             now() + make_interval(days => $13, mins => $14))
        on conflict (business_id, locale, canonical_hash, prompt_version) do update
            set answer = excluded.answer,
                embedding = excluded.embedding,
                embedding_model = excluded.embedding_model,
                quality_score = excluded.quality_score,
                volatility_class = excluded.volatility_class,
                retrieval_signature = excluded.retrieval_signature,
                data_version = excluded.data_version,
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
        json.dumps(retrieval_signature) if retrieval_signature is not None else None,
        data_version,
        prompt_version,
        ttl_days,
        ttl_minutes,
    )


# --- Invalidare dynamic (G5b-2) ---------------------------------------------


async def current_prices(
    conn: asyncpg.Connection,
    business_id: str,
    product_ids: list[str],
) -> dict[str, float]:
    """Prețul curent (min variantă, ca în search_products) pe id-urile date.
    `{product_id: price}` — doar produsele ACTIVE care încă există. Folosit de
    price-check-ul self-healing: orice id lipsă SAU preț diferit → entry învechit."""
    if not product_ids:
        return {}
    rows = await conn.fetch(
        f"""
        select p.id::text as product_id, {_EFFECTIVE_PRICE}::float8 as price
        from products p
        left join lateral (
            select min(coalesce(v.sale_price, v.price)) as price
            from product_variants v
            where v.product_id = p.id
        ) vp on true
        where p.business_id = $1
          and p.status = 'active'
          and p.id = any($2::uuid[])
        """,
        business_id,
        product_ids,
    )
    return {r["product_id"]: float(r["price"]) for r in rows if r["price"] is not None}


async def delete_entry(conn: asyncpg.Connection, business_id: str, entry_id: str) -> None:
    """Purjă lazy a unui entry învechit (price-check/version mismatch)."""
    await conn.execute(
        "delete from semantic_cache where business_id = $1 and id = $2",
        business_id,
        entry_id,
    )


async def purge_business(conn: asyncpg.Connection, business_id: str) -> int:
    """Șterge tot cache-ul unui business (offboarding / reset). Întoarce nr. de rânduri."""
    res = await conn.execute("delete from semantic_cache where business_id = $1", business_id)
    return int(res.split()[-1]) if res else 0


async def purge_by_product(
    conn: asyncpg.Connection,
    business_id: str,
    product_id: str,
) -> int:
    """Șterge entry-urile al căror `retrieval_signature` conține product_id (ex. scos din
    stoc). `@>` jsonb: array-ul conține un element cu acest product_id. Întoarce nr. rânduri."""
    res = await conn.execute(
        "delete from semantic_cache where business_id = $1 and retrieval_signature @> $2::jsonb",
        business_id,
        json.dumps([{"product_id": product_id}]),
    )
    return int(res.split()[-1]) if res else 0
