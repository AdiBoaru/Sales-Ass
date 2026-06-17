"""Query-uri pe catalog (read pentru bot).

Principiul 7: fiecare query are EXPLICIT `where business_id = $1` (mecanism
primar). RLS (rolul bot_runtime + app.business_id din tenant_conn) e plasa.

search_products aici e versiunea cu FILTRE SQL (categorie, brand, preț, text).
Ranking-ul SEMANTIC (embedding <=> query pe subsetul filtrat) se adaugă după ce
există product_embeddings (job de embed). Vezi schema_reference.md.
"""

from typing import Any

import asyncpg

# Prețul REAL e pe variantă (vezi T037): fiecare produs are variante cu sale_price
# propriu, de obicei mai mic decât products.price. Sursăm min(variant) cu fallback
# la products. Validatorul de preț trebuie să vadă același preț ca clientul.
_EFFECTIVE_PRICE = "coalesce(vp.price, p.sale_price, p.price)"

# Câmpuri per produs (CLAUDE.md): id, name, brand, price, url, ai_summary, stock,
# availability + image (prima poză, pentru cardurile de produs — W1).
_SELECT = f"""
    select
        p.id::text                  as id,
        p.name                      as name,
        b.name                      as brand,
        {_EFFECTIVE_PRICE}::float8  as price,
        p.product_url               as url,
        p.ai_summary                as ai_summary,
        p.stock_total               as stock,
        p.availability              as availability,
        img.url                     as image,
        p.rating::float8            as rating,
        p.review_count              as review_count,
        prs.top_pros[1]             as review_pro,
        prs.top_pros                as top_pros
    from products p
    left join brands b on b.id = p.brand_id
    left join categories c on c.id = p.primary_category_id
    left join product_review_summaries prs on prs.product_id = p.id
    left join lateral (
        select min(coalesce(v.sale_price, v.price)) as price
        from product_variants v
        where v.product_id = p.id
    ) vp on true
    left join lateral (
        select pi.url from product_images pi
        where pi.product_id = p.id
        order by pi.position asc nulls last
        limit 1
    ) img on true
"""


async def search_products(
    conn: asyncpg.Connection,
    business_id: str,
    *,
    category: str | None = None,
    brand: str | None = None,
    concerns: list[str] | None = None,
    price_max: float | None = None,
    query_text: str | None = None,
    limit: int = 6,
) -> list[dict[str, Any]]:
    """Caută produse active pentru un tenant, cu filtre SQL dure.

    Toate filtrele sunt opționale și se combină cu AND. Returnează max `limit`
    produse (hard cap 6 — principiul „max 6 produse" din arhitectură), fiecare
    cu cele 8 câmpuri. `conn` trebuie să fie deja tenant-scoped (tenant_conn).

    `concerns` filtrează pe `attributes->'concerns'` (operatorul jsonb `?|` = oricare),
    paritate cu `search_products_semantic` — calea de plasă (NX-98) filtrează la fel pe nevoie.
    """
    limit = min(limit, 6)

    conds = ["p.business_id = $1", "p.status = 'active'"]
    params: list[Any] = [business_id]

    def placeholder(value: Any) -> str:
        params.append(value)
        return f"${len(params)}"

    if category:
        # match pe slug exact SAU nume (case-insensitive)
        slug_ph = placeholder(category)
        name_ph = placeholder(category)
        conds.append(f"(lower(c.slug) = lower({slug_ph}) or lower(c.name) = lower({name_ph}))")
    if brand:
        conds.append(f"b.name ilike {placeholder(f'%{brand}%')}")
    if concerns:
        conds.append(f"(p.attributes->'concerns') ?| {placeholder(concerns)}::text[]")
    if price_max is not None:
        conds.append(f"{_EFFECTIVE_PRICE} <= {placeholder(price_max)}")
    if query_text:
        conds.append(f"p.name ilike {placeholder(f'%{query_text}%')}")

    sql = (
        _SELECT
        + " where "
        + " and ".join(conds)
        + f" order by p.rating desc, {_EFFECTIVE_PRICE} asc"
        + f" limit {placeholder(limit)}"
    )

    rows = await conn.fetch(sql, *params)
    return [dict(r) for r in rows]


async def has_embeddings(conn: asyncpg.Connection, business_id: str) -> bool:
    """True dacă tenantul are măcar un `product_embedding`.

    Decide calea din `search_products_tool`: semantic (JOIN pe product_embeddings)
    doar dacă există embeddings; altfel SQL-only (NX-98). Un singur SELECT scoped
    (principiul 7); ieftin — nu merită memoizat (embeddings apar după job, nu în tur).
    """
    row = await conn.fetchrow(
        "select 1 from product_embeddings where business_id = $1 limit 1",
        business_id,
    )
    return row is not None


# Detalii bogate per produs (tool-uri G7): câmpurile de bază + rezumatul de recenzii (D3).
_DETAIL_SELECT = f"""
    select
        p.id::text                  as id,
        p.name                      as name,
        b.name                      as brand,
        {_EFFECTIVE_PRICE}::float8  as price,
        p.product_url               as url,
        p.ai_summary                as ai_summary,
        p.stock_total               as stock,
        p.availability              as availability,
        img.url                     as image,
        p.rating::float8            as rating,
        prs.summary                 as review_summary,
        prs.top_pros                as top_pros,
        prs.top_cons                as top_cons,
        prs.sentiment::float8       as sentiment
    from products p
    left join brands b on b.id = p.brand_id
    left join product_review_summaries prs on prs.product_id = p.id
    left join lateral (
        select min(coalesce(v.sale_price, v.price)) as price
        from product_variants v
        where v.product_id = p.id
    ) vp on true
    left join lateral (
        select pi.url from product_images pi
        where pi.product_id = p.id
        order by pi.position asc nulls last
        limit 1
    ) img on true
"""


async def get_products_by_ids(
    conn: asyncpg.Connection,
    business_id: str,
    product_ids: list[str],
    *,
    limit: int = 6,
) -> list[dict[str, Any]]:
    """Produse active după id (tool-uri get_product_details / compare_products), cu detalii
    bogate (rating + rezumat recenzii D3). `business_id = $1` (izolare; RLS plasa). Max
    `limit` (hard cap 6). Ordinea NU e garantată — caller-ul o poate re-mapa pe id."""
    if not product_ids:
        return []
    limit = min(limit, 6)
    rows = await conn.fetch(
        _DETAIL_SELECT
        + " where p.business_id = $1 and p.status = 'active' and p.id = any($2::uuid[])"
        + " limit $3",
        business_id,
        product_ids[:limit],
        limit,
    )
    return [dict(r) for r in rows]


async def list_category_slugs(conn: asyncpg.Connection, business_id: str) -> list[str]:
    """Slug-urile categoriilor active ale tenantului — pentru groundarea triajului.

    Triaj-ul (nano) primește lista asta și alege `category_key` din ea; orice
    valoare inventată în afara listei e respinsă în cod (→ category_key None /
    CLARIFY). `conn` trebuie să fie deja tenant-scoped (tenant_conn)."""
    rows = await conn.fetch(
        "select slug from categories where business_id = $1 order by slug",
        business_id,
    )
    return [r["slug"] for r in rows]


async def search_products_semantic(
    conn: asyncpg.Connection,
    business_id: str,
    query_embedding: list[float],
    *,
    price_max: float | None = None,
    concerns: list[str] | None = None,
    category: str | None = None,
    limit: int = 6,
) -> list[dict[str, Any]]:
    """Căutare HIBRIDĂ: filtre SQL dure (preț/categorie/concerns) + ranking semantic
    (cosine pe `product_embeddings`). `query_embedding` = vectorul mesajului (calculat
    de tool/agent prin adaptor — stratul de date NU apelează LLM). Max 6 produse, cele
    8 câmpuri. `conn` trebuie să fie deja tenant-scoped (tenant_conn).

    `concerns` filtrează pe `attributes->'concerns'` (operatorul jsonb `?|` = oricare).
    Ordinea = distanța cosine (cel mai apropiat primul)."""
    limit = min(limit, 6)
    qvec = "[" + ",".join(f"{x:.7f}" for x in query_embedding) + "]"

    conds = ["p.business_id = $1", "p.status = 'active'"]
    params: list[Any] = [business_id]

    def placeholder(value: Any) -> str:
        params.append(value)
        return f"${len(params)}"

    qvec_ph = placeholder(qvec)  # vectorul de query
    if price_max is not None:
        conds.append(f"{_EFFECTIVE_PRICE} <= {placeholder(price_max)}")
    if category:
        slug_ph = placeholder(category)
        name_ph = placeholder(category)
        conds.append(f"(lower(c.slug) = lower({slug_ph}) or lower(c.name) = lower({name_ph}))")
    if concerns:
        conds.append(f"(p.attributes->'concerns') ?| {placeholder(concerns)}::text[]")

    sql = (
        _SELECT
        + " join product_embeddings pe on pe.product_id = p.id"
        + " where "
        + " and ".join(conds)
        + f" order by pe.embedding <=> {qvec_ph}::vector"
        + f" limit {placeholder(limit)}"
    )
    rows = await conn.fetch(sql, *params)
    return [dict(r) for r in rows]
