"""Query-uri pe catalog (read pentru bot).

Principiul 7: fiecare query are EXPLICIT `where business_id = $1` (mecanism
primar). RLS (rolul bot_runtime + app.business_id din tenant_conn) e plasa.

search_products aici e versiunea cu FILTRE SQL (categorie, brand, preț, text).
Ranking-ul SEMANTIC (embedding <=> query pe subsetul filtrat) se adaugă după ce
există product_embeddings (job de embed). Vezi schema_reference.md.
"""

import json
from typing import Any

import asyncpg

from src.config import get_settings

# Prețul REAL e pe variantă (vezi T037): fiecare produs are variante cu sale_price
# propriu, de obicei mai mic decât products.price. Sursăm min(variant) cu fallback
# la products. Validatorul de preț trebuie să vadă același preț ca clientul.
_EFFECTIVE_PRICE = "coalesce(vp.price, p.sale_price, p.price)"

# Rating „shrunk" (Bayesian) — un 5.0 cu 1 recenzie NU mai îngroapă un 4.6 cu 200 (cold-start).
# Prior C≈30 spre media 4.0: (n*rating + C*4.0)/(n + C). Pur SQL, `review_count` deja selectat.
_SHRUNK_RATING = (
    "((coalesce(p.review_count, 0) * coalesce(p.rating, 0) + 30 * 4.0)"
    " / (coalesce(p.review_count, 0) + 30))"
)

# Moduri de sortare (allowlist → zero injection; sort_mode e structural, nu param bindabil).
_VALID_SORT = frozenset({"relevance", "price_asc", "price_desc", "rating_desc"})

# NX-118: array compact de variante (cap 12, cele mai ieftine) hidratat pe read path → validatorul
# vede prețurile per-variantă reale (50ml vs 100ml) și modelul etichetele/SKU. Neutru de vertical
# (nuanțe beauty / mărimi fashion / fitment auto — `label` vine din DB). `vp` (scalarul min) rămâne
# (îl folosesc _EFFECTIVE_PRICE + sortarea). Fragment partajat de `_SELECT`/`_DETAIL_SELECT` (DRY).
# Perf: rulează pe tot pool-ul de fuziune (ca lateralele `vp`/`img` existente), dar e un index-scan
# ieftin pe idx_variants_product(product_id), ≤16 rânduri — îl ținem și pe `_SELECT` ca validatorul
# să aibă prețurile per-variantă pe ORICE cale (search/detail), robust la dedup.
_VARIANTS_AGG = """
    left join lateral (
        select jsonb_agg(
            jsonb_build_object(
                'id', v.id::text,
                'variant_id', v.id::text,
                'label', v.label,
                'sku', v.sku,
                'price', coalesce(v.sale_price, v.price)::float8,
                'list_price',
                    (case when v.sale_price is not null and v.sale_price < v.price
                          then v.price end)::float8,
                'stock', v.stock,
                'color_hex', v.color_hex,
                'attributes', coalesce(v.attributes, '{}'::jsonb),
                'shade', v.attributes->>'shade',
                'undertone', v.attributes->>'undertone',
                'depth', v.attributes->>'depth',
                'net_content_value', v.net_content_value::float8,
                'net_content_unit', v.net_content_unit,
                'price_per_unit', v.price_per_unit::float8,
                'gtin', v.gtin,
                'image_url', v.image_url
            ) order by coalesce(v.sale_price, v.price) asc
        ) as variants
        from (
            select * from product_variants
            where product_id = p.id and business_id = p.business_id
            order by coalesce(sale_price, price) asc
            limit 16
        ) v
    ) vr on true
"""


def _row_to_product(r: asyncpg.Record) -> dict[str, Any]:
    """`dict(r)` + decodează jsonb (NX-118). asyncpg întoarce jsonb ca STR (fără codec) →
    `json.loads`: `variants` → `list[dict]` (NULL → `[]`); `attributes` → `dict` (NULL → `{}`,
    pentru fațetele de comparație, Tier 2). Orice altă coloană intactă."""
    d = dict(r)
    if "variants" in d:
        v = d["variants"]
        if isinstance(v, str):
            try:
                d["variants"] = json.loads(v)
            except (ValueError, TypeError):
                d["variants"] = []
        elif v is None:
            d["variants"] = []
    if "attributes" in d:
        a = d["attributes"]
        if isinstance(a, str):
            try:
                d["attributes"] = json.loads(a)
            except (ValueError, TypeError):
                d["attributes"] = {}
        elif a is None:
            d["attributes"] = {}
    # NX-169: graf PDP (168e-2) — sections (json_agg → str) + badges (text[] → list). NULL → [].
    if "sections" in d:
        s = d["sections"]
        if isinstance(s, str):
            try:
                d["sections"] = json.loads(s)
            except (ValueError, TypeError):
                d["sections"] = []
        elif s is None:
            d["sections"] = []
    if "badges" in d and d["badges"] is None:
        d["badges"] = []
    if "ingredients_db" in d and d["ingredients_db"] is None:
        d["ingredients_db"] = []
    if "reviews_list" in d:
        rv = d["reviews_list"]
        if isinstance(rv, str):
            try:
                d["reviews_list"] = json.loads(rv)
            except (ValueError, TypeError):
                d["reviews_list"] = []
        elif rv is None:
            d["reviews_list"] = []
    return d


def _order_clause(sort_mode: str, *, qvec_ph: str | None = None) -> str:
    """`ORDER BY` pe mod de sortare + tie-break determinist `p.id` (omoară ordonarea instabilă pe
    egalități → cache + golden stabile). Filter-then-sort: constrângerile dure stau în WHERE, AICI
    doar sortăm. Kill-switch `SEARCH_SORT_MODE_ENABLED=False` → `ORDER BY`-ul vechi (byte-identic).
    Pe calea semantică (`qvec_ph`): `relevance` = cosine; price/rating = sort explicit pe subsetul
    deja filtrat semantic (NB: sub HNSW = cel-mai-ieftin-din-recall, nu global — vezi ARCH §P3)."""
    if not get_settings().search_sort_mode_enabled:
        # Kill-switch: revert EXACT — pe semantic = cosine (qvec_ph), pe SQL = rating desc.
        if qvec_ph is not None:
            return f" order by pe.embedding <=> {qvec_ph}::vector"
        return f" order by p.rating desc, {_EFFECTIVE_PRICE} asc"
    mode = sort_mode if sort_mode in _VALID_SORT else "relevance"
    if mode == "price_asc":
        return f" order by {_EFFECTIVE_PRICE} asc, {_SHRUNK_RATING} desc, p.id"
    if mode == "price_desc":
        return f" order by {_EFFECTIVE_PRICE} desc, {_SHRUNK_RATING} desc, p.id"
    if mode == "rating_desc":
        return f" order by {_SHRUNK_RATING} desc, {_EFFECTIVE_PRICE} asc, p.id"
    # relevance
    if qvec_ph is not None:
        return f" order by pe.embedding <=> {qvec_ph}::vector, p.id"
    return f" order by {_SHRUNK_RATING} desc, {_EFFECTIVE_PRICE} asc, p.id"


def _content_status_pred(business_id_ph: str = "$1") -> str | None:
    """NX-171c: predicat quality-gate pentru read-path (discovery). Întoarce `None` (fără filtru)
    când kill-switch-ul GLOBAL e OFF. Altfel, filtru PER-TENANT: arată doar 'published' DACĂ
    tenantul a optat (`businesses.settings->>'content_status_filter'`), altfel catalog integral
    (fără outage). Sub-query-ul scalar e NECORELAT (constant $1) → evaluat O DATĂ ca initplan, zero
    cost per-rând. NULL/absent setting → `false` → filtru inactiv. `business_id_ph` = placeholder-ul
    lui business_id în query-ul apelant (mereu `$1` în funcțiile de catalog)."""
    if not get_settings().content_status_filter_enabled:
        return None
    return (
        "(p.content_status = 'published' or coalesce("
        "(select (settings->>'content_status_filter')::boolean from businesses "
        f"where id = {business_id_ph}), false) = false)"
    )


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
        prs.top_pros                as top_pros,
        (p.sale_price is not null and p.sale_price < p.price) as on_sale,
        -- IZI-anchor: preț ORIGINAL (tăiat), DOAR la reducere reală; altfel NULL → cardul nu
        -- afișează „de la X" fals pe o variantă mai mică. `price` rămâne efectivul curent.
        (case when p.sale_price is not null and p.sale_price < p.price then p.price end)::float8
                                    as list_price,
        p.attributes->'concerns'    as concerns,
        p.attributes                as attributes,
        vr.variants                 as variants
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
{_VARIANTS_AGG}
"""


def _feature_clause(facet_keys: tuple[str, ...], values: list[str], placeholder: Any) -> str:
    """Tier 2b p2: condiție SQL „produsul ARE una din valorile cerute", în UNIUNEA atributelor-array
    din `facet_keys` (ex. key_ingredients), cu match NORMALIZAT (lower + strip diacritice RO, ca
    `normalize`) → „niacinamida"/„niacinamidă" se potrivesc. Chei PARAMETRIZATE (safe). `values`
    deja normalizate de caller."""
    arrays = []
    for k in facet_keys:
        kp = placeholder(k)
        arrays.append(
            f"(case when jsonb_typeof(p.attributes->{kp})='array' "
            f"then p.attributes->{kp} else '[]'::jsonb end)"
        )
    union = " || ".join(arrays)
    return (
        f"exists (select 1 from jsonb_array_elements_text({union}) fe "
        f"where translate(lower(fe), 'ăâîșț', 'aaist') = any({placeholder(values)}::text[]))"
    )


def _variant_label_clause(label: str, placeholder: Any) -> str:
    """NX-135: produsul are o VARIANTĂ cu eticheta cerută (nuanță/mărime) — filtru DUR pentru
    fallback-ul gradat („alte game care CHIAR au Warm Beige"). Match NORMALIZAT (lower + strip
    diacritice RO, ca `_feature_clause`/`normalize`) + substring → „warm beige" prinde „Warm Beige".
    Corelat pe produsul din SELECT (`v.product_id = p.id`); scope moștenit din `business_id`."""
    lp = placeholder(label)
    return (
        "exists (select 1 from product_variants v where v.product_id = p.id "
        f"and translate(lower(v.label), 'ăâîșț', 'aaist') "
        f"like '%' || translate(lower({lp}), 'ăâîșț', 'aaist') || '%')"
    )


def _category_clause(category: str, placeholder: Any) -> str:
    """NX-167 (A): predicatul de categorie, ca UN SINGUR `exists(...)` INLINE (se adaugă la `conds`
    ca orice altă condiție — fără CTE, fără restructurarea query-urilor).

    Cu `SEARCH_CATEGORY_TREE_ENABLED`: produsul e „în categorie" dacă ORICARE din categoriile lui
    — `primary_category_id` SAU orice `product_category_map` — e categoria CERUTĂ sau un DESCENDENT
    al ei (materialized path `categories.path`). Repară „cerere pe părinte (machiaj) ratează copiii
    (fond-de-ten)". `reqc`/`sub`/`m` NU se leagă de aliasurile din SELECT (`c`/`p`) → fără
    coliziune; corelat pe `p.business_id`/`p.primary_category_id`/`p.id` (scope P7).

    Fără flag (OFF): match exact pe slug/nume al `primary_category_id` — BYTE-IDENTIC cu vechiul cod
    (două placeholder-e, ca înainte). `category` = slug SAU nume."""
    if not get_settings().search_category_tree_enabled:
        slug_ph = placeholder(category)
        name_ph = placeholder(category)
        return f"(lower(c.slug) = lower({slug_ph}) or lower(c.name) = lower({name_ph}))"
    cat_ph = placeholder(category)  # un singur placeholder, reutilizat de 2 ori (aceeași valoare)
    return (
        "exists (select 1 from categories reqc "
        "join categories sub on sub.business_id = reqc.business_id "
        "and (sub.id = reqc.id or sub.path like reqc.path || '/%') "
        "where reqc.business_id = p.business_id "
        f"and (lower(reqc.slug) = lower({cat_ph}) or lower(reqc.name) = lower({cat_ph})) "
        "and (sub.id = p.primary_category_id or exists (select 1 from product_category_map m "
        "where m.product_id = p.id and m.category_id = sub.id)))"
    )


async def search_products(
    conn: asyncpg.Connection,
    business_id: str,
    *,
    category: str | None = None,
    brand: str | None = None,
    concerns: list[str] | None = None,
    features: list[str] | None = None,
    searchable_facets: tuple[str, ...] = (),
    price_max: float | None = None,
    query_text: str | None = None,
    sort_mode: str = "relevance",
    in_stock_only: bool = False,
    limit: int = 6,
) -> list[dict[str, Any]]:
    """Caută produse active pentru un tenant, cu filtre SQL dure + mod de sortare explicit.

    Toate filtrele sunt opționale și se combină cu AND. Returnează max `limit`
    produse (hard cap 6 — principiul „max 6 produse" din arhitectură), fiecare
    cu cele 8 câmpuri. `conn` trebuie să fie deja tenant-scoped (tenant_conn).

    `sort_mode` (filter-then-sort): `price_asc` pt preț („cel mai ieftin"), `rating_desc`
    pt „cel mai bun", altfel `relevance`. `in_stock_only` = filtru DUR pe disponibilitate (doar
    cerut explicit). `concerns` filtrează pe `attributes->'concerns'` (`?|` = oricare).
    """
    limit = min(limit, 6)

    conds = ["p.business_id = $1", "p.status = 'active'"]
    params: list[Any] = [business_id]

    def placeholder(value: Any) -> str:
        params.append(value)
        return f"${len(params)}"

    if category:
        # NX-167 (A): match pe arbore (primary + product_category_map + descendenți) sau, cu flag
        # OFF, exact pe slug/nume al primary_category_id (byte-identic cu vechiul cod).
        conds.append(_category_clause(category, placeholder))
    if brand:
        conds.append(f"b.name ilike {placeholder(f'%{brand}%')}")
    if concerns:
        conds.append(f"(p.attributes->'concerns') ?| {placeholder(concerns)}::text[]")
    if features and searchable_facets:
        conds.append(_feature_clause(searchable_facets, features, placeholder))
    if price_max is not None:
        conds.append(f"{_EFFECTIVE_PRICE} <= {placeholder(price_max)}")
    if in_stock_only:
        conds.append("p.availability in ('in_stock', 'low_stock')")
    if query_text:
        conds.append(f"p.name ilike {placeholder(f'%{query_text}%')}")
    if cs := _content_status_pred():  # NX-171c: doar 'published' (per-tenant, gated)
        conds.append(cs)

    sql = (
        _SELECT
        + " where "
        + " and ".join(conds)
        + _order_clause(sort_mode)
        + f" limit {placeholder(limit)}"
    )

    rows = await conn.fetch(sql, *params)
    return [_row_to_product(r) for r in rows]


async def search_products_lexical(
    conn: asyncpg.Connection,
    business_id: str,
    query_text: str,
    *,
    category: str | None = None,
    brand: str | None = None,
    concerns: list[str] | None = None,
    features: list[str] | None = None,
    searchable_facets: tuple[str, ...] = (),
    variant_label: str | None = None,
    price_max: float | None = None,
    sort_mode: str = "relevance",
    in_stock_only: bool = False,
    pool: int = 50,
) -> list[dict[str, Any]]:
    """Lexical REAL (NX-113a) — înlocuiește `p.name ILIKE '%q%'`. Match pe FTS
    (`websearch_to_tsquery('simple', $q)` pe `search_tsv`) SAU pe `pg_trgm` similarity pe nume
    (typo / SKU / cod-piesă — esențial pe HVAC/auto, generic). ACELEAȘI filtre dure ca
    `search_products` (paritate). Întoarce ~`pool` rânduri; pe `relevance` ordinea = rang lexical
    (`ts_rank_cd + similarity`), deci POZIȚIA în listă = rangul pt RRF (NX-113b). Pe sort explicit
    (price/rating) delegă `_order_clause`. Config `'simple'` = limbă-agnostic (P11). `conn`
    tenant-scoped (P7: `business_id = $1`)."""
    conds = ["p.business_id = $1", "p.status = 'active'"]
    params: list[Any] = [business_id]

    def placeholder(value: Any) -> str:
        params.append(value)
        return f"${len(params)}"

    q_ph = placeholder(query_text)  # un singur placeholder, reutilizat în match + rank
    # Match lexical: FTS (frază naturală) SAU trgm (typo/SKU). Query gol/stopwords → tsquery gol
    # (nu prinde nimic) → cade pe trgm; niciun SQL invalid.
    conds.append(f"(p.search_tsv @@ websearch_to_tsquery('simple', {q_ph}) or p.name % {q_ph})")
    if category:
        conds.append(_category_clause(category, placeholder))  # NX-167 (A): match pe arbore
    if brand:
        conds.append(f"b.name ilike {placeholder(f'%{brand}%')}")
    if concerns:
        conds.append(f"(p.attributes->'concerns') ?| {placeholder(concerns)}::text[]")
    if features and searchable_facets:
        conds.append(_feature_clause(searchable_facets, features, placeholder))
    if variant_label:  # NX-135: filtru DUR pe eticheta de variantă (fallback gradat)
        conds.append(_variant_label_clause(variant_label, placeholder))
    if price_max is not None:
        conds.append(f"{_EFFECTIVE_PRICE} <= {placeholder(price_max)}")
    if in_stock_only:
        conds.append("p.availability in ('in_stock', 'low_stock')")
    if cs := _content_status_pred():  # NX-171c: doar 'published' (per-tenant, gated)
        conds.append(cs)

    if sort_mode == "relevance":
        rank = (
            f"ts_rank_cd(p.search_tsv, websearch_to_tsquery('simple', {q_ph}))"
            f" + similarity(p.name, {q_ph})"
        )
        order = f" order by ({rank}) desc, p.id"
    else:
        order = _order_clause(sort_mode)  # price/rating explicit → sort pe subsetul lexical filtrat

    sql = _SELECT + " where " + " and ".join(conds) + order + f" limit {placeholder(pool)}"
    rows = await conn.fetch(sql, *params)
    return [_row_to_product(r) for r in rows]


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
        p.review_count              as review_count,
        p.attributes                as attributes,
        -- IZI-anchor: preț original (tăiat) DOAR la reducere reală (vezi _SELECT); altfel NULL.
        (case when p.sale_price is not null and p.sale_price < p.price then p.price end)::float8
                                    as list_price,
        prs.summary                 as review_summary,
        prs.top_pros                as top_pros,
        prs.top_cons                as top_cons,
        prs.sentiment::float8       as sentiment,
        sec.sections                as sections,
        bdg.badges                  as badges,
        ing.names                   as ingredients_db,
        rvw.items                   as reviews_list,
        vr.variants                 as variants
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
    -- NX-168e-2 graf PDP, consumat de _detail_view (NX-169): secțiuni + badge-uri de trust.
    left join lateral (
        select json_agg(
                   json_build_object('kind', s.kind, 'title', s.title, 'body', s.body)
                   order by s.position
               ) as sections
        from product_sections s where s.product_id = p.id
    ) sec on true
    left join lateral (
        select array_agg(pb.label order by pb.label) as badges
        from product_badges pb where pb.product_id = p.id
    ) bdg on true
    -- NX-169: consumă tabelul NORMALIZAT de ingrediente (168e-2) — INCI cheie din product_ingr.
    left join lateral (
        select array_agg(i.name order by pi.position) as names
        from product_ingredients pi
        join ingredients i on i.id = pi.ingredient_id
        where pi.product_id = p.id and pi.is_key
    ) ing on true
    -- NX-169: consumă recenziile INDIVIDUALE (168e-2) — top 2 după rating, corelate pe tenant.
    left join lateral (
        select json_agg(
                   json_build_object('author', r.author, 'rating', r.rating, 'body', r.body)
                   order by r.rating desc
               ) as items
        from (
            select author, rating, body from reviews
            where product_id = p.id and business_id = p.business_id
            order by rating desc limit 2
        ) r
    ) rvw on true
{_VARIANTS_AGG}
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
    `limit` (hard cap 6). Ordinea ÎN care s-au cerut id-urile e PĂSTRATĂ (`array_position`) —
    deixis-ul ordinal („a doua"/„compară primele două") rezolvă produsul corect."""
    if not product_ids:
        return []
    limit = min(limit, 6)
    rows = await conn.fetch(
        _DETAIL_SELECT
        + " where p.business_id = $1 and p.status = 'active' and p.id = any($2::uuid[])"
        + " order by array_position($2::uuid[], p.id)"
        + " limit $3",
        business_id,
        product_ids[:limit],
        limit,
    )
    return [_row_to_product(r) for r in rows]


async def product_category_roots(
    conn: asyncpg.Connection, business_id: str, product_ids: list[str]
) -> dict[str, str]:
    """NX-167 (C): root-branch-ul (primul segment din `categories.path`) al categoriei PRIMARE a
    fiecărui produs — pentru garda de coerență la compare (`machiaj` vs. `par` = incoerent).
    `business_id = $1` (izolare P7; RLS plasă). Produsele fără categorie/`path` sunt ABSENTE din
    dict → caller-ul e fail-open (nu blochează pe date lipsă)."""
    if not product_ids:
        return {}
    rows = await conn.fetch(
        "select p.id::text as id, split_part(c.path, '/', 1) as root "
        "from products p join categories c on c.id = p.primary_category_id "
        "where p.business_id = $1 and p.id = any($2::uuid[]) "
        "and c.path is not null and c.path <> ''",
        business_id,
        product_ids,
    )
    return {r["id"]: r["root"] for r in rows if r["root"]}


async def search_cheaper_than(
    conn: asyncpg.Connection,
    business_id: str,
    reference_ids: list[str],
    max_price_exclusive: float,
    *,
    limit: int = 6,
) -> list[dict[str, Any]]:
    """Produse active STRICT mai ieftine decât `max_price_exclusive`, în ACEEAȘI categorie ca
    produsele de referință (cele afișate), sortate pe preț crescător (P1 ARCH-product-retrieval).

    Pentru follow-up-ul „mai ieftin": ancorat pe categoria setului afișat (subquery pe id-urile lor)
    → nu aduce „cel mai ieftin gunoi" din alt raft. DOAR produse CUMPĂRABILE (în stoc) — un „cel
    mai ieftin" fără stoc e inutil. Determinist (cel mai ieftin real = rândul 1), FĂRĂ padding —
    întoarce DOAR ce e mai ieftin (1 dacă e 1). Gol = nu există nimic mai ieftin (în stoc).
    `business_id = $1` (izolare; RLS plasă). Hard cap 6."""
    if not reference_ids:
        return []
    limit = min(limit, 6)
    cs = _content_status_pred()  # NX-171c: doar 'published' (per-tenant, gated)
    sql = (
        _SELECT
        + " where p.business_id = $1 and p.status = 'active'"
        + " and p.availability in ('in_stock', 'low_stock')"
        + " and p.primary_category_id in ("
        + "   select primary_category_id from products"
        + "   where business_id = $1 and id = any($2::uuid[]) and primary_category_id is not null)"
        + " and p.id <> all($2::uuid[])"  # exclude produsele AFIȘATE: un produs în reducere nu e
        + f" and {_EFFECTIVE_PRICE} < $3"  # „mai ieftin decât el însuși" → altfel bucla pe același
        + (f" and {cs}" if cs else "")
        + f" order by {_EFFECTIVE_PRICE} asc, {_SHRUNK_RATING} desc, p.id"
        + " limit $4"
    )
    rows = await conn.fetch(sql, business_id, reference_ids, max_price_exclusive, limit)
    return [_row_to_product(r) for r in rows]


async def get_complementary_products(
    conn: asyncpg.Connection,
    business_id: str,
    anchor_id: str,
    *,
    exclude_ids: list[str] | None = None,
    limit: int = 4,
) -> list[dict[str, Any]]:
    """Produse COMPLEMENTARE produsului `anchor_id` (cross-sell „merge bine cu" / rutină, #7b).

    NX-171b: **relations-first** — citește relații EXPLICITE curate din `product_relations`
    (`complement`/`routine_next`/`accessory`), nu heuristica same-brand/concern. Doar CUMPĂRABILE
    (în stoc), excluzând ancora + coșul (`exclude_ids`), ordonate pe tip (rutina întâi) + poziția
    curată. Când ancora n-are NICIO relație (sau kill-switch `relations_first_enabled` OFF) → cade
    pe heuristica veche (`_complementary_heuristic`, byte-identic). Gol = niciun semnal (flux ok).
    `business_id = $1` (izolare; RLS plasă). Hard cap 6."""
    limit = min(limit, 6)
    exclude = list(dict.fromkeys([anchor_id, *(exclude_ids or [])]))
    if get_settings().relations_first_enabled:
        # Contract: heuristica e fallback DOAR când ancora n-are NICIO relație curată (complement/
        # rutină/accesoriu) — NU când relațiile există dar sunt neeligibile (draft/out-of-stock/în
        # coș). Altfel un produs cu rutină definită ar aluneca înapoi în heuristica same-brand când
        # pașii lui sunt temporar fără stoc. Verificăm EXISTENȚA relației separat de eligibilitate.
        if await _has_complementary_relations(conn, business_id, anchor_id):
            return await _complementary_from_relations(conn, business_id, anchor_id, exclude, limit)
    return await _complementary_heuristic(conn, business_id, anchor_id, exclude, limit)


async def _has_complementary_relations(
    conn: asyncpg.Connection, business_id: str, anchor_id: str
) -> bool:
    """Ancora are ≥1 relație de complementaritate (complement/routine_next/accessory), indiferent
    de eligibilitatea produsului-țintă? Decide relations-first vs fallback heuristic (NU eligibi-
    litatea). Același set de `kind` ca `_complementary_from_relations`. Tenant-scoped (P7)."""
    return bool(
        await conn.fetchval(
            "select exists(select 1 from product_relations where business_id=$1 and product_id=$2 "
            "and kind in ('complement', 'routine_next', 'accessory'))",
            business_id,
            anchor_id,
        )
    )


async def _complementary_from_relations(
    conn: asyncpg.Connection,
    business_id: str,
    anchor_id: str,
    exclude: list[str],
    limit: int,
) -> list[dict[str, Any]]:
    """NX-171b: complementarele din `product_relations`. Agregă la UN rând per produs-înrudit
    (`min(prioritate)` peste kind-uri: routine_next < complement < accessory) → fără duplicat când
    un produs e legat prin >1 kind. Filtre de cumpărabilitate + published (per-tenant). `$1` =
    business_id (folosit ȘI de predicatul content_status)."""
    cs = _content_status_pred()
    sql = (
        _SELECT
        + " join (select related_id,"
        + "          min(case kind when 'routine_next' then 0 when 'complement' then 1 else 2 end)"
        + "            as prio,"
        + "          min(position) as pos"
        + "        from product_relations"
        + "        where business_id = $1 and product_id = $2"
        + "          and kind in ('complement', 'routine_next', 'accessory')"
        + "        group by related_id) pr on pr.related_id = p.id"
        + " where p.business_id = $1 and p.status = 'active'"
        + " and p.availability in ('in_stock', 'low_stock')"
        + " and p.id <> all($3::uuid[])"
        + (f" and {cs}" if cs else "")
        + f" order by pr.prio, pr.pos, {_SHRUNK_RATING} desc, p.id"
        + " limit $4"
    )
    rows = await conn.fetch(sql, business_id, anchor_id, exclude, limit)
    return [_row_to_product(r) for r in rows]


async def _complementary_heuristic(
    conn: asyncpg.Connection,
    business_id: str,
    anchor_id: str,
    exclude: list[str],
    limit: int,
) -> list[dict[str, Any]]:
    """Fallback heuristic (pre-171b): produse din ACELAȘI brand SAU care împart un `concern` cu
    ancora, dar dintr-o categorie DIFERITĂ (complement, NU substitut). Same-brand întâi, apoi rating
    shrunk. Folosit când ancora n-are relații explicite sau kill-switch-ul e OFF."""
    same_brand = "(select brand_id from products where business_id = $1 and id = $2)"
    # concern-urile ancorei ca text[] (gol → '{}' → fără overlap, cade pe same-brand). `?|` oricare.
    anchor_concerns = (
        "coalesce((select array(select jsonb_array_elements_text(pa.attributes->'concerns'))"
        "          from products pa where pa.business_id = $1 and pa.id = $2), '{}')::text[]"
    )
    cs = _content_status_pred()
    sql = (
        _SELECT
        + " where p.business_id = $1 and p.status = 'active'"
        + " and p.availability in ('in_stock', 'low_stock')"
        + " and p.id <> all($3::uuid[])"  # exclude ancora + ce e în coș
        # categorie DIFERITĂ (complement, NU substitut — alt ser nu „merge bine cu" un ser):
        + " and p.primary_category_id is distinct from"
        + "     (select primary_category_id from products where business_id = $1 and id = $2)"
        + f" and (p.brand_id = {same_brand} or (p.attributes->'concerns') ?| {anchor_concerns})"
        + (f" and {cs}" if cs else "")
        + f" order by (p.brand_id = {same_brand}) desc nulls last, {_SHRUNK_RATING} desc, p.id"
        + " limit $4"
    )
    rows = await conn.fetch(sql, business_id, anchor_id, exclude, limit)
    return [_row_to_product(r) for r in rows]


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


async def sibling_categories(
    conn: asyncpg.Connection, business_id: str, slug: str, *, limit: int = 4
) -> list[str]:
    """NX-136: numele categoriilor SURORI (același `parent_id`) ale categoriei cu `slug`, pentru
    chips-urile de închidere („recomandă un gel de curățare" după o cremă). Dublu-scoped pe
    `business_id` (c1 ȘI c2, P7). `is not distinct from` tratează `parent_id` NULL = NULL → o
    categorie TOP-LEVEL primește celelalte top-level ca surori. `conn` tenant-scoped."""
    rows = await conn.fetch(
        """
        select c2.name
        from categories c1
        join categories c2
          on c2.business_id = c1.business_id
         and c2.parent_id is not distinct from c1.parent_id
         and c2.id <> c1.id
        where c1.business_id = $1 and c1.slug = $2
        order by c2.name
        limit $3
        """,
        business_id,
        slug,
        limit,
    )
    return [r["name"] for r in rows]


async def list_category_names(conn: asyncpg.Connection, business_id: str) -> list[str]:
    """Numele categoriilor TOP-LEVEL ale tenantului — pentru groundarea promptului agentului
    (NX-78, principiul 9). `order by name` → ordine deterministă (prefix de cache stabil).
    `conn` trebuie să fie deja tenant-scoped (tenant_conn)."""
    rows = await conn.fetch(
        "select name from categories where business_id = $1 and parent_id is null order by name",
        business_id,
    )
    return [r["name"] for r in rows]


async def list_routing_aliases(
    conn: asyncpg.Connection, business_id: str, *, limit: int = 20
) -> list[tuple[str, str]]:
    """Aliasele de rutare APROBATE (`(phrase_norm, target_value)`) — hint scurt în promptul
    agentului (NX-78). DOAR `status='approved'` (principiul 9: nu rutăm pe ghicit neaprobat).
    `order by phrase_norm` → deterministic (prefix de cache stabil)."""
    rows = await conn.fetch(
        "select phrase_norm, coalesce(target_value, '') as target "
        "from intent_aliases "
        "where business_id = $1 and status = 'approved' "
        "order by phrase_norm limit $2",
        business_id,
        limit,
    )
    return [(r["phrase_norm"], r["target"]) for r in rows]


async def search_products_semantic(
    conn: asyncpg.Connection,
    business_id: str,
    query_embedding: list[float],
    *,
    price_max: float | None = None,
    concerns: list[str] | None = None,
    features: list[str] | None = None,
    searchable_facets: tuple[str, ...] = (),
    variant_label: str | None = None,
    category: str | None = None,
    brand: str | None = None,
    sort_mode: str = "relevance",
    in_stock_only: bool = False,
    limit: int = 6,
    pool: int | None = None,
) -> list[dict[str, Any]]:
    """Căutare HIBRIDĂ: filtre SQL dure (preț/categorie/brand/concerns/stoc) + ranking.
    `query_embedding` = vectorul mesajului (calculat de tool/agent prin adaptor — stratul de date
    NU apelează LLM). `conn` trebuie tenant-scoped (tenant_conn).

    `sort_mode`: `relevance` = cosine (cel mai apropiat primul); `price_asc`/`rating_desc` = sort
    explicit pe subsetul filtrat semantic. `concerns` filtrează pe `attributes->'concerns'`.

    `pool` (NX-113b): când e dat, întoarce ~`pool` candidați pentru fuziunea RRF (nu doar 6);
    poziția în listă = rangul vectorial. Lipsă (`None`) → comportament compat (max 6).

    NX-113c: `query_embedding` se trimite ca `list[float]` DIRECT (codecul pgvector din pool îl
    encodează) — fără literalul text de ~15KB inline pe hot path. SELECT-ul expune și
    `cosine_distance` (distanța vectorială a rândului) ca semnal de calitate (`top_cosine_distance`
    în emit)."""
    sql_limit = pool if pool is not None else min(limit, 6)

    conds = ["p.business_id = $1", "p.status = 'active'"]
    params: list[Any] = [business_id]

    def placeholder(value: Any) -> str:
        params.append(value)
        return f"${len(params)}"

    qvec_ph = placeholder(query_embedding)  # vectorul de query (list[float], codec pgvector)
    if price_max is not None:
        conds.append(f"{_EFFECTIVE_PRICE} <= {placeholder(price_max)}")
    if category:
        conds.append(_category_clause(category, placeholder))  # NX-167 (A): match pe arbore
    if brand:
        # Filtru DUR pe brand (la fel ca SQL-only): un brand cerut care nu există în catalog →
        # zero rezultate, NU produse semantic-apropiate de la alt brand (bug-ul „avem … Chanel").
        conds.append(f"b.name ilike {placeholder(f'%{brand}%')}")
    if concerns:
        conds.append(f"(p.attributes->'concerns') ?| {placeholder(concerns)}::text[]")
    if features and searchable_facets:
        conds.append(_feature_clause(searchable_facets, features, placeholder))
    if variant_label:  # NX-135: filtru DUR pe eticheta de variantă (fallback gradat)
        conds.append(_variant_label_clause(variant_label, placeholder))
    if in_stock_only:
        conds.append("p.availability in ('in_stock', 'low_stock')")
    if cs := _content_status_pred():  # NX-171c: doar 'published' (per-tenant, gated)
        conds.append(cs)

    # NX-171d: embeddings versionate (PK compus product_id, doc_type, model). Join-ul TREBUIE să
    # filtreze doc_type + model activ, altfel >1 rând/produs → produs duplicat în rezultate. +
    # `pe.business_id = p.business_id` (P7: un rând embedding cu business_id greșit nu scapă).
    emb_doc = placeholder("product")
    emb_model = placeholder(get_settings().model_embed)
    # Injectează coloana distanței vectoriale (cosine) în SELECT — semnal de calitate pt emit.
    cos_col = f"        (pe.embedding <=> {qvec_ph}::vector)::float8 as cosine_distance,\n"
    select_with_cos = _SELECT.replace("    select\n", "    select\n" + cos_col, 1)
    sql = (
        select_with_cos
        + " join product_embeddings pe on pe.product_id = p.id"
        + f"   and pe.business_id = p.business_id and pe.doc_type = {emb_doc}"
        + f"   and pe.model = {emb_model}"
        + " where "
        + " and ".join(conds)
        + _order_clause(sort_mode, qvec_ph=qvec_ph)
        + f" limit {placeholder(sql_limit)}"
    )
    rows = await conn.fetch(sql, *params)
    return [_row_to_product(r) for r in rows]
