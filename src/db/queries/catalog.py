"""Query-uri pe catalog (read pentru bot).

Principiul 7: fiecare query are EXPLICIT `where business_id = $1` (mecanism
primar). RLS (rolul bot_runtime + app.business_id din tenant_conn) e plasa.

search_products aici e versiunea cu FILTRE SQL (categorie, brand, preț, text).
Ranking-ul SEMANTIC (embedding <=> query pe subsetul filtrat) se adaugă după ce
există product_embeddings (job de embed). Vezi schema_reference.md.
"""

from typing import Any

import asyncpg

# 8 câmpuri per produs (CLAUDE.md): id, name, brand, price, url, ai_summary, stock, availability
_SELECT = """
    select
        p.id::text                              as id,
        p.name                                  as name,
        b.name                                  as brand,
        coalesce(p.sale_price, p.price)::float8 as price,
        p.product_url                           as url,
        p.ai_summary                            as ai_summary,
        p.stock_total                           as stock,
        p.availability                          as availability
    from products p
    left join brands b on b.id = p.brand_id
    left join categories c on c.id = p.primary_category_id
"""


async def search_products(
    conn: asyncpg.Connection,
    business_id: str,
    *,
    category: str | None = None,
    brand: str | None = None,
    price_max: float | None = None,
    query_text: str | None = None,
    limit: int = 6,
) -> list[dict[str, Any]]:
    """Caută produse active pentru un tenant, cu filtre SQL dure.

    Toate filtrele sunt opționale și se combină cu AND. Returnează max `limit`
    produse (hard cap 6 — principiul „max 6 produse" din arhitectură), fiecare
    cu cele 8 câmpuri. `conn` trebuie să fie deja tenant-scoped (tenant_conn).
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
    if price_max is not None:
        conds.append(f"coalesce(p.sale_price, p.price) <= {placeholder(price_max)}")
    if query_text:
        conds.append(f"p.name ilike {placeholder(f'%{query_text}%')}")

    sql = (
        _SELECT
        + " where "
        + " and ".join(conds)
        + " order by p.rating desc, coalesce(p.sale_price, p.price) asc"
        + f" limit {placeholder(limit)}"
    )

    rows = await conn.fetch(sql, *params)
    return [dict(r) for r in rows]
