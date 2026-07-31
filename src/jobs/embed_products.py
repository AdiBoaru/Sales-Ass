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
import json
import logging

from src.agent.llm import get_llm
from src.db.connection import admin_conn, close_pool, get_pool

log = logging.getLogger(__name__)
BATCH = 128


_EMBED_FACETS = (
    ("concerns", "Potrivit pentru"),
    ("suitable_for", "Pentru"),
    ("texture", "Textură"),
    ("finish", "Finish"),
    ("coverage", "Acoperire"),
    ("key_ingredients", "Ingrediente"),
    ("key_benefit", "Beneficiu"),
)


def _embed_text(row: dict) -> str:
    """NX-170: doc de embedding DETERMINIST din faptele canonice (v3): name + brand + categorie +
    ai_summary + concerns/suitable_for/texture/finish/coverage/key_ingredients/key_benefit.
    `not_recommended_for` NU intră (excludere STRUCTURATĂ, nu embedding pozitiv)."""
    a = row.get("attributes")
    if isinstance(a, str):
        try:
            a = json.loads(a)
        except (ValueError, TypeError):
            a = {}
    a = a if isinstance(a, dict) else {}
    parts = [row["name"]]
    for key in ("brand", "category", "ai_summary"):
        if row.get(key):
            parts.append(str(row[key]))
    for key, label in _EMBED_FACETS:
        v = a.get(key)
        if isinstance(v, list) and v:
            parts.append(f"{label}: " + ", ".join(str(x) for x in v))
        elif isinstance(v, str) and v:
            parts.append(f"{label}: {v}")
    return " | ".join(parts)


def _content_hash(text: str, model: str) -> str:
    return hashlib.sha256(f"{model}:{text}".encode()).hexdigest()


async def embed_pending(conn, llm, *, force: bool = False, limit: int = 0) -> int:
    """Embed produsele cu hash schimbat/lipsă. Întoarce câte au fost embed-uite."""
    model = llm.model_embed
    # NX-171d: embeddings versionate (PK compus product_id, doc_type, model). Doc-ul de produs =
    # doc_type 'product'; join-ul filtrează doc_type + model activ ca `existing` să fie hash-ul
    # rândului CORECT (nu al altui model/doc_type → altfel re-embed spurios sau skip greșit).
    # `pe.business_id = p.business_id` (P7): un rând embedding cu business_id greșit NU trebuie să
    # facă jobul să sară re-embed-ul (altfel read-path-ul, filtrat corect pe business, n-ar vedea
    # produsul niciodată — embedding fantomă al altui tenant).
    rows = await conn.fetch(
        """
        select p.id::text as id, p.business_id::text as business_id, p.name,
               b.name as brand, cat.name as category, p.ai_summary,
               p.attributes as attributes,
               pe.content_hash as existing
        from products p
        left join brands b on b.id = p.brand_id
        left join categories cat on cat.id = p.primary_category_id
        left join product_embeddings pe
               on pe.product_id = p.id and pe.business_id = p.business_id
              and pe.doc_type = 'product' and pe.model = $1
        where p.status = 'active'
        order by p.id
        """,
        model,
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
                        (product_id, business_id, model, doc_type, embedding, content_hash,
                         updated_at)
                    values ($1, $2, $3, 'product', $4::vector, $5, now())
                    on conflict (product_id, doc_type, model) do update set
                        business_id = excluded.business_id,
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


async def embed_shadow_pending(
    conn,
    llm,
    business_id: str,
    *,
    force: bool = False,
    limit: int = 0,
) -> int:
    """Embed NX-207 search documents alongside the live product embeddings.

    **Cursa document → embedding e închisă** (review #251, runda 3). Între citirea textului și
    scrierea vectorului stă `await llm.embed()` — un apel de rețea de ordinul secundelor. Dacă
    jobul de documente rescrie documentul în fereastra aia, salvam vectorul textului VECHI.

    Închisă cu concurență optimistă: `doc_hash` e versiunea documentului la momentul citirii, iar
    scrierea e un `INSERT ... SELECT` condiționat de faptul că documentul are ÎNCĂ acel hash. Dacă
    s-a schimbat, `SELECT` nu întoarce niciun rând → nu se scrie nimic. Eșec în siguranță: rularea
    următoare re-embedează, pentru că hash-ul tot nu se va potrivi."""
    model = llm.model_embed
    rows = await conn.fetch(
        """
        select d.product_id::text as id,
               d.business_id::text as business_id,
               d.positive_search_document as text,
               d.content_hash as doc_hash,
               pe.content_hash as existing
        from product_search_documents d
        join products p on p.id = d.product_id and p.business_id = d.business_id
        left join product_embeddings pe on pe.product_id = d.product_id
          and pe.business_id = d.business_id
          and pe.doc_type = 'search_document_v1'
          and pe.model = $1
        where p.status = 'active' and d.business_id = $2 and d.document_version = $3
        order by d.product_id
        """,
        model,
        business_id,
        1,
    )

    todo = []
    for row in rows:
        content_hash = _content_hash(row["text"], model)
        if force or row["existing"] != content_hash:
            todo.append((row, content_hash))

    if limit:
        todo = todo[:limit]

    total = 0
    for start in range(0, len(todo), BATCH):
        chunk = todo[start : start + BATCH]
        vectors = await llm.embed([row["text"] for row, _ in chunk])
        async with conn.transaction():
            for (row, content_hash), vector in zip(chunk, vectors, strict=True):
                literal = "[" + ",".join(f"{value:.7f}" for value in vector) + "]"
                # `insert ... select ... where d.content_hash = $6`: scrierea EXISTĂ doar dacă
                # documentul e încă cel pe care l-am trimis la embedding. Altfel `select` e gol și
                # nu se scrie nimic — un vector pentru un text care nu mai există ar fi mai rău
                # decât lipsa lui, pentru că `content_hash` l-ar declara valid.
                status = await conn.execute(
                    """
                    insert into product_embeddings
                      (product_id, business_id, model, doc_type, embedding, content_hash,
                       updated_at)
                    select $1::uuid, $2::uuid, $3, 'search_document_v1', $4::vector, $5, now()
                    from product_search_documents d
                    where d.product_id = $1::uuid and d.business_id = $2::uuid
                      and d.document_version = 1 and d.content_hash = $6
                    on conflict (product_id, doc_type, model) do update set
                      business_id = excluded.business_id,
                      embedding = excluded.embedding,
                      content_hash = excluded.content_hash,
                      updated_at = now()
                    """,
                    row["id"],
                    row["business_id"],
                    model,
                    literal,
                    content_hash,
                    row["doc_hash"],
                )
                if status and status.endswith(" 0"):
                    log.info(
                        "embed shadow: sărit, documentul s-a schimbat în timpul embeddingului "
                        "(product_id=%s)",
                        row["id"],
                    )
                else:
                    total += 1

    return total


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
