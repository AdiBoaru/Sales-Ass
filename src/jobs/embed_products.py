"""Job: embed produse → `product_embeddings` (search semantic).

Incremental: re-embed DOAR când textul s-a schimbat (content_hash). Re-rulare la
zi = $0. Textul embed-uit = nume + brand + descriere (ai_summary îmbogățit) +
concerns → retrieval pe nevoie reală, nu doar cuvinte.

Rulează ca ADMIN (scrie în catalog; bot_runtime n-are voie) prin `admin_conn`.
Folosește adaptorul unic OpenAI (`src.agent.llm`).

    python -m src.jobs.embed_products              # embed cele schimbate/lipsă
    python -m src.jobs.embed_products --force       # re-embed tot
    python -m src.jobs.embed_products --limit 5     # doar primele N (test)
"""

import argparse
import asyncio
import hashlib
import logging

from src.agent.llm import get_llm
from src.db.connection import admin_conn, close_pool, get_pool

log = logging.getLogger(__name__)
BATCH = 128


def _embed_text(row: dict) -> str:
    """Textul reprezentativ al produsului pentru embedding."""
    parts = [row["name"]]
    if row["brand"]:
        parts.append(row["brand"])
    if row["ai_summary"]:
        parts.append(row["ai_summary"])
    concerns = row["concerns"] or []
    if concerns:
        parts.append("Potrivit pentru: " + ", ".join(concerns))
    return " | ".join(parts)


def _content_hash(text: str, model: str) -> str:
    return hashlib.sha256(f"{model}:{text}".encode()).hexdigest()


async def embed_pending(conn, llm, *, force: bool = False, limit: int = 0) -> int:
    """Embed produsele cu hash schimbat/lipsă. Întoarce câte au fost embed-uite."""
    model = llm.model_embed
    rows = await conn.fetch(
        """
        select p.id::text as id, p.business_id::text as business_id, p.name,
               b.name as brand, p.ai_summary,
               (select array_agg(v) from jsonb_array_elements_text(
                    case when jsonb_typeof(p.attributes->'concerns')='array'
                         then p.attributes->'concerns' else '[]'::jsonb end) v) as concerns,
               pe.content_hash as existing
        from products p
        left join brands b on b.id = p.brand_id
        left join product_embeddings pe on pe.product_id = p.id
        where p.status = 'active'
        order by p.id
        """
    )
    todo = []
    for r in rows:
        text = _embed_text(r)
        h = _content_hash(text, model)
        if force or r["existing"] != h:
            todo.append((dict(r), text, h))
    if limit:
        todo = todo[:limit]
    if not todo:
        log.info("Nimic de embed-uit — toate la zi (%d produse).", len(rows))
        return 0

    log.info("De embed-uit: %d produse (model=%s)", len(todo), model)
    done = 0
    for i in range(0, len(todo), BATCH):
        chunk = todo[i : i + BATCH]
        vectors = await llm.embed([t for _, t, _ in chunk])
        async with conn.transaction():
            for (r, _text, h), vec in zip(chunk, vectors, strict=True):
                vec_lit = "[" + ",".join(f"{x:.7f}" for x in vec) + "]"
                await conn.execute(
                    """
                    insert into product_embeddings
                        (product_id, business_id, model, embedding, content_hash, updated_at)
                    values ($1, $2, $3, $4::vector, $5, now())
                    on conflict (product_id) do update set
                        business_id = excluded.business_id, model = excluded.model,
                        embedding = excluded.embedding, content_hash = excluded.content_hash,
                        updated_at = now()
                    """,
                    r["id"],
                    r["business_id"],
                    model,
                    vec_lit,
                    h,
                )
        done += len(chunk)
        log.info("  %d/%d", done, len(todo))
    return done


async def _main() -> None:
    logging.basicConfig(level=logging.INFO)
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    llm = get_llm()
    if llm is None:
        log.error("OPENAI_API_KEY lipsește — nu pot embed-ui")
        return
    pool = await get_pool()
    try:
        async with admin_conn(pool) as conn:
            await embed_pending(conn, llm, force=args.force, limit=args.limit)
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(_main())
