-- sort: relevance

    select
        p.id::text                  as id,
        p.name                      as name,
        b.name                      as brand,
        coalesce(vp.price, (case when (p.sale_price is not null and p.sale_price < p.price and (p.sale_start is null or p.sale_start <= current_date) and (p.sale_end is null or p.sale_end >= current_date)) then p.sale_price else p.price end))::float8  as price,
        p.product_url               as url,
        p.ai_summary                as ai_summary,
        p.stock_total               as stock,
        p.availability              as availability,
        img.url                     as image,
        p.rating::float8            as rating,
        p.review_count              as review_count,
        prs.top_pros[1]             as review_pro,
        prs.top_pros                as top_pros,
        (p.sale_price is not null and p.sale_price < p.price and (p.sale_start is null or p.sale_start <= current_date) and (p.sale_end is null or p.sale_end >= current_date)) as on_sale,
        -- IZI-anchor: preț ORIGINAL (tăiat), DOAR la reducere reală; altfel NULL → cardul nu
        -- afișează „de la X" fals pe o variantă mai mică. `price` rămâne efectivul curent.
        (case when (p.sale_price is not null and p.sale_price < p.price and (p.sale_start is null or p.sale_start <= current_date) and (p.sale_end is null or p.sale_end >= current_date)) then p.price end)::float8
                                    as list_price,
        -- NX-299 felia 4: voucherul. Singurul semnal comercial de tip iZi pe care catalogul REAL
        -- il sustine: `sale_price < price` = 0/2.758, dar 2.123 produse au `coupon_code` +
        -- `coupon_price`. Coloanele existau de la import, scrise de `catalog/sole_source.py`,
        -- si nu le citea NIMENI din `src/`, deci pretul cu voucher nu ajungea nicaieri.
        p.coupon_code               as coupon_code,
        p.coupon_price::float8      as coupon_price,
        p.attributes->'concerns'    as concerns,
        p.attributes                as attributes,
        -- NX-240: moneda + momentul VERIFICĂRII. `currency` fiindcă o sumă fără unitate nu e o
        -- sumă (grounding-ul o marchează UNKNOWN); `synced_at` fiindcă `updated_at` spune doar
        -- când s-a atins rândul, nu când s-a confruntat cu sursa — iar CTA-urile de comerț se
        -- sprijină pe verificare, nu pe atingere.
        p.currency                  as currency,
        p.synced_at                 as synced_at,
        vr.variants                 as variants
    from products p
    left join brands b on b.id = p.brand_id
    left join categories c on c.id = p.primary_category_id
    -- P7: `business_id` EXPLICIT și pe join, nu doar pe tabela condusă. `product_review_summaries`
    -- are cheia primară pe `product_id` singur, deci join-ul „mergea" fără el — dar izolarea nu
    -- trebuie să depindă de forma unei chei primare care se poate schimba.
    left join product_review_summaries prs
           on prs.product_id = p.id and prs.business_id = p.business_id
    left join lateral (
        select min(case when (p.sale_start is null or p.sale_start <= current_date) and (p.sale_end is null or p.sale_end >= current_date) and v.sale_price is not null
                         and v.sale_price < v.price then v.sale_price else v.price end) as price
        from product_variants v
        where v.product_id = p.id and v.business_id = p.business_id
    ) vp on true
    left join lateral (
        select pi.url from product_images pi
        where pi.product_id = p.id
        order by pi.position asc nulls last
        limit 1
    ) img on true

    left join lateral (
        select jsonb_agg(
            jsonb_build_object(
                'id', v.id::text,
                'variant_id', v.id::text,
                'label', v.label,
                'sku', v.sku,
                'price', (case when v.sale_price is not null and v.sale_price < v.price and (p.sale_start is null or p.sale_start <= current_date) and (p.sale_end is null or p.sale_end >= current_date)
                               then v.sale_price else v.price end)::float8,
                'list_price',
                    (case when v.sale_price is not null and v.sale_price < v.price and (p.sale_start is null or p.sale_start <= current_date) and (p.sale_end is null or p.sale_end >= current_date) then v.price end)::float8,
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
            ) order by (case when v.sale_price is not null and v.sale_price < v.price and (p.sale_start is null or p.sale_start <= current_date) and (p.sale_end is null or p.sale_end >= current_date)
                             then v.sale_price else v.price end) asc
        ) as variants
        from (
            select * from product_variants
            where product_id = p.id and business_id = p.business_id
            order by (case when (p.sale_start is null or p.sale_start <= current_date) and (p.sale_end is null or p.sale_end >= current_date) and sale_price is not null
                           and sale_price < price then sale_price else price end) asc
            limit 16
        ) v
    ) vr on true

 where p.business_id = $1 and p.status = 'active' and p.search_tsv @@ websearch_to_tsquery('simple', ro_unaccent($2)) and exists (select 1 from categories reqc join categories sub on sub.business_id = reqc.business_id and (sub.id = reqc.id or sub.path like reqc.path || '/%') where reqc.business_id = p.business_id and (lower(reqc.slug) = any($3::text[]) or lower(reqc.name) = any($3::text[])) and (sub.id = p.primary_category_id or exists (select 1 from product_category_map m where m.product_id = p.id and m.category_id = sub.id))) and ro_unaccent(b.name) like ro_unaccent($4) and (p.attributes->'concerns') ?| $5::text[] and (case when jsonb_typeof(p.attributes->$6) = 'array' then (p.attributes->$6) ?| $7::text[] else (p.attributes->>$6) = any($7::text[]) end) and exists (select 1 from jsonb_array_elements_text((case when jsonb_typeof(p.attributes->$8)='array' then p.attributes->$8 else '[]'::jsonb end)) fe where translate(lower(fe), 'ăâîșț', 'aaist') = any($9::text[])) and exists (select 1 from product_variants v where v.product_id = p.id and translate(lower(v.label), 'ăâîșț', 'aaist') like '%' || translate(lower($10), 'ăâîșț', 'aaist') || '%') and coalesce(vp.price, (case when (p.sale_price is not null and p.sale_price < p.price and (p.sale_start is null or p.sale_start <= current_date) and (p.sale_end is null or p.sale_end >= current_date)) then p.sale_price else p.price end)) <= $11 and p.availability in ('in_stock', 'low_stock') and (p.content_status = 'published' or coalesce((select (settings->>'content_status_filter')::boolean from businesses where id = $1), false) = false) order by (ts_rank_cd(p.search_tsv, websearch_to_tsquery('simple', ro_unaccent($2)))) desc, p.id limit $12
-- params: ['biz', 'crema or hidratanta or ten', ['ten'], '%Anua%', ['dryness'], 'skin_type', ['dry'], 'key_ingredients', ['niacinamida'], '50 ml', 99.5, 50]

-- sort: price_asc

    select
        p.id::text                  as id,
        p.name                      as name,
        b.name                      as brand,
        coalesce(vp.price, (case when (p.sale_price is not null and p.sale_price < p.price and (p.sale_start is null or p.sale_start <= current_date) and (p.sale_end is null or p.sale_end >= current_date)) then p.sale_price else p.price end))::float8  as price,
        p.product_url               as url,
        p.ai_summary                as ai_summary,
        p.stock_total               as stock,
        p.availability              as availability,
        img.url                     as image,
        p.rating::float8            as rating,
        p.review_count              as review_count,
        prs.top_pros[1]             as review_pro,
        prs.top_pros                as top_pros,
        (p.sale_price is not null and p.sale_price < p.price and (p.sale_start is null or p.sale_start <= current_date) and (p.sale_end is null or p.sale_end >= current_date)) as on_sale,
        -- IZI-anchor: preț ORIGINAL (tăiat), DOAR la reducere reală; altfel NULL → cardul nu
        -- afișează „de la X" fals pe o variantă mai mică. `price` rămâne efectivul curent.
        (case when (p.sale_price is not null and p.sale_price < p.price and (p.sale_start is null or p.sale_start <= current_date) and (p.sale_end is null or p.sale_end >= current_date)) then p.price end)::float8
                                    as list_price,
        -- NX-299 felia 4: voucherul. Singurul semnal comercial de tip iZi pe care catalogul REAL
        -- il sustine: `sale_price < price` = 0/2.758, dar 2.123 produse au `coupon_code` +
        -- `coupon_price`. Coloanele existau de la import, scrise de `catalog/sole_source.py`,
        -- si nu le citea NIMENI din `src/`, deci pretul cu voucher nu ajungea nicaieri.
        p.coupon_code               as coupon_code,
        p.coupon_price::float8      as coupon_price,
        p.attributes->'concerns'    as concerns,
        p.attributes                as attributes,
        -- NX-240: moneda + momentul VERIFICĂRII. `currency` fiindcă o sumă fără unitate nu e o
        -- sumă (grounding-ul o marchează UNKNOWN); `synced_at` fiindcă `updated_at` spune doar
        -- când s-a atins rândul, nu când s-a confruntat cu sursa — iar CTA-urile de comerț se
        -- sprijină pe verificare, nu pe atingere.
        p.currency                  as currency,
        p.synced_at                 as synced_at,
        vr.variants                 as variants
    from products p
    left join brands b on b.id = p.brand_id
    left join categories c on c.id = p.primary_category_id
    -- P7: `business_id` EXPLICIT și pe join, nu doar pe tabela condusă. `product_review_summaries`
    -- are cheia primară pe `product_id` singur, deci join-ul „mergea" fără el — dar izolarea nu
    -- trebuie să depindă de forma unei chei primare care se poate schimba.
    left join product_review_summaries prs
           on prs.product_id = p.id and prs.business_id = p.business_id
    left join lateral (
        select min(case when (p.sale_start is null or p.sale_start <= current_date) and (p.sale_end is null or p.sale_end >= current_date) and v.sale_price is not null
                         and v.sale_price < v.price then v.sale_price else v.price end) as price
        from product_variants v
        where v.product_id = p.id and v.business_id = p.business_id
    ) vp on true
    left join lateral (
        select pi.url from product_images pi
        where pi.product_id = p.id
        order by pi.position asc nulls last
        limit 1
    ) img on true

    left join lateral (
        select jsonb_agg(
            jsonb_build_object(
                'id', v.id::text,
                'variant_id', v.id::text,
                'label', v.label,
                'sku', v.sku,
                'price', (case when v.sale_price is not null and v.sale_price < v.price and (p.sale_start is null or p.sale_start <= current_date) and (p.sale_end is null or p.sale_end >= current_date)
                               then v.sale_price else v.price end)::float8,
                'list_price',
                    (case when v.sale_price is not null and v.sale_price < v.price and (p.sale_start is null or p.sale_start <= current_date) and (p.sale_end is null or p.sale_end >= current_date) then v.price end)::float8,
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
            ) order by (case when v.sale_price is not null and v.sale_price < v.price and (p.sale_start is null or p.sale_start <= current_date) and (p.sale_end is null or p.sale_end >= current_date)
                             then v.sale_price else v.price end) asc
        ) as variants
        from (
            select * from product_variants
            where product_id = p.id and business_id = p.business_id
            order by (case when (p.sale_start is null or p.sale_start <= current_date) and (p.sale_end is null or p.sale_end >= current_date) and sale_price is not null
                           and sale_price < price then sale_price else price end) asc
            limit 16
        ) v
    ) vr on true

 where p.business_id = $1 and p.status = 'active' and p.search_tsv @@ websearch_to_tsquery('simple', ro_unaccent($2)) and exists (select 1 from categories reqc join categories sub on sub.business_id = reqc.business_id and (sub.id = reqc.id or sub.path like reqc.path || '/%') where reqc.business_id = p.business_id and (lower(reqc.slug) = any($3::text[]) or lower(reqc.name) = any($3::text[])) and (sub.id = p.primary_category_id or exists (select 1 from product_category_map m where m.product_id = p.id and m.category_id = sub.id))) and ro_unaccent(b.name) like ro_unaccent($4) and (p.attributes->'concerns') ?| $5::text[] and (case when jsonb_typeof(p.attributes->$6) = 'array' then (p.attributes->$6) ?| $7::text[] else (p.attributes->>$6) = any($7::text[]) end) and exists (select 1 from jsonb_array_elements_text((case when jsonb_typeof(p.attributes->$8)='array' then p.attributes->$8 else '[]'::jsonb end)) fe where translate(lower(fe), 'ăâîșț', 'aaist') = any($9::text[])) and exists (select 1 from product_variants v where v.product_id = p.id and translate(lower(v.label), 'ăâîșț', 'aaist') like '%' || translate(lower($10), 'ăâîșț', 'aaist') || '%') and coalesce(vp.price, (case when (p.sale_price is not null and p.sale_price < p.price and (p.sale_start is null or p.sale_start <= current_date) and (p.sale_end is null or p.sale_end >= current_date)) then p.sale_price else p.price end)) <= $11 and p.availability in ('in_stock', 'low_stock') and (p.content_status = 'published' or coalesce((select (settings->>'content_status_filter')::boolean from businesses where id = $1), false) = false) order by coalesce(vp.price, (case when (p.sale_price is not null and p.sale_price < p.price and (p.sale_start is null or p.sale_start <= current_date) and (p.sale_end is null or p.sale_end >= current_date)) then p.sale_price else p.price end)) asc, ((coalesce(p.review_count, 0) * coalesce(p.rating, 0) + 30 * 4.0) / (coalesce(p.review_count, 0) + 30)) desc, p.id limit $12
-- params: ['biz', 'crema or hidratanta or ten', ['ten'], '%Anua%', ['dryness'], 'skin_type', ['dry'], 'key_ingredients', ['niacinamida'], '50 ml', 99.5, 50]

-- sort: price_desc

    select
        p.id::text                  as id,
        p.name                      as name,
        b.name                      as brand,
        coalesce(vp.price, (case when (p.sale_price is not null and p.sale_price < p.price and (p.sale_start is null or p.sale_start <= current_date) and (p.sale_end is null or p.sale_end >= current_date)) then p.sale_price else p.price end))::float8  as price,
        p.product_url               as url,
        p.ai_summary                as ai_summary,
        p.stock_total               as stock,
        p.availability              as availability,
        img.url                     as image,
        p.rating::float8            as rating,
        p.review_count              as review_count,
        prs.top_pros[1]             as review_pro,
        prs.top_pros                as top_pros,
        (p.sale_price is not null and p.sale_price < p.price and (p.sale_start is null or p.sale_start <= current_date) and (p.sale_end is null or p.sale_end >= current_date)) as on_sale,
        -- IZI-anchor: preț ORIGINAL (tăiat), DOAR la reducere reală; altfel NULL → cardul nu
        -- afișează „de la X" fals pe o variantă mai mică. `price` rămâne efectivul curent.
        (case when (p.sale_price is not null and p.sale_price < p.price and (p.sale_start is null or p.sale_start <= current_date) and (p.sale_end is null or p.sale_end >= current_date)) then p.price end)::float8
                                    as list_price,
        -- NX-299 felia 4: voucherul. Singurul semnal comercial de tip iZi pe care catalogul REAL
        -- il sustine: `sale_price < price` = 0/2.758, dar 2.123 produse au `coupon_code` +
        -- `coupon_price`. Coloanele existau de la import, scrise de `catalog/sole_source.py`,
        -- si nu le citea NIMENI din `src/`, deci pretul cu voucher nu ajungea nicaieri.
        p.coupon_code               as coupon_code,
        p.coupon_price::float8      as coupon_price,
        p.attributes->'concerns'    as concerns,
        p.attributes                as attributes,
        -- NX-240: moneda + momentul VERIFICĂRII. `currency` fiindcă o sumă fără unitate nu e o
        -- sumă (grounding-ul o marchează UNKNOWN); `synced_at` fiindcă `updated_at` spune doar
        -- când s-a atins rândul, nu când s-a confruntat cu sursa — iar CTA-urile de comerț se
        -- sprijină pe verificare, nu pe atingere.
        p.currency                  as currency,
        p.synced_at                 as synced_at,
        vr.variants                 as variants
    from products p
    left join brands b on b.id = p.brand_id
    left join categories c on c.id = p.primary_category_id
    -- P7: `business_id` EXPLICIT și pe join, nu doar pe tabela condusă. `product_review_summaries`
    -- are cheia primară pe `product_id` singur, deci join-ul „mergea" fără el — dar izolarea nu
    -- trebuie să depindă de forma unei chei primare care se poate schimba.
    left join product_review_summaries prs
           on prs.product_id = p.id and prs.business_id = p.business_id
    left join lateral (
        select min(case when (p.sale_start is null or p.sale_start <= current_date) and (p.sale_end is null or p.sale_end >= current_date) and v.sale_price is not null
                         and v.sale_price < v.price then v.sale_price else v.price end) as price
        from product_variants v
        where v.product_id = p.id and v.business_id = p.business_id
    ) vp on true
    left join lateral (
        select pi.url from product_images pi
        where pi.product_id = p.id
        order by pi.position asc nulls last
        limit 1
    ) img on true

    left join lateral (
        select jsonb_agg(
            jsonb_build_object(
                'id', v.id::text,
                'variant_id', v.id::text,
                'label', v.label,
                'sku', v.sku,
                'price', (case when v.sale_price is not null and v.sale_price < v.price and (p.sale_start is null or p.sale_start <= current_date) and (p.sale_end is null or p.sale_end >= current_date)
                               then v.sale_price else v.price end)::float8,
                'list_price',
                    (case when v.sale_price is not null and v.sale_price < v.price and (p.sale_start is null or p.sale_start <= current_date) and (p.sale_end is null or p.sale_end >= current_date) then v.price end)::float8,
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
            ) order by (case when v.sale_price is not null and v.sale_price < v.price and (p.sale_start is null or p.sale_start <= current_date) and (p.sale_end is null or p.sale_end >= current_date)
                             then v.sale_price else v.price end) asc
        ) as variants
        from (
            select * from product_variants
            where product_id = p.id and business_id = p.business_id
            order by (case when (p.sale_start is null or p.sale_start <= current_date) and (p.sale_end is null or p.sale_end >= current_date) and sale_price is not null
                           and sale_price < price then sale_price else price end) asc
            limit 16
        ) v
    ) vr on true

 where p.business_id = $1 and p.status = 'active' and p.search_tsv @@ websearch_to_tsquery('simple', ro_unaccent($2)) and exists (select 1 from categories reqc join categories sub on sub.business_id = reqc.business_id and (sub.id = reqc.id or sub.path like reqc.path || '/%') where reqc.business_id = p.business_id and (lower(reqc.slug) = any($3::text[]) or lower(reqc.name) = any($3::text[])) and (sub.id = p.primary_category_id or exists (select 1 from product_category_map m where m.product_id = p.id and m.category_id = sub.id))) and ro_unaccent(b.name) like ro_unaccent($4) and (p.attributes->'concerns') ?| $5::text[] and (case when jsonb_typeof(p.attributes->$6) = 'array' then (p.attributes->$6) ?| $7::text[] else (p.attributes->>$6) = any($7::text[]) end) and exists (select 1 from jsonb_array_elements_text((case when jsonb_typeof(p.attributes->$8)='array' then p.attributes->$8 else '[]'::jsonb end)) fe where translate(lower(fe), 'ăâîșț', 'aaist') = any($9::text[])) and exists (select 1 from product_variants v where v.product_id = p.id and translate(lower(v.label), 'ăâîșț', 'aaist') like '%' || translate(lower($10), 'ăâîșț', 'aaist') || '%') and coalesce(vp.price, (case when (p.sale_price is not null and p.sale_price < p.price and (p.sale_start is null or p.sale_start <= current_date) and (p.sale_end is null or p.sale_end >= current_date)) then p.sale_price else p.price end)) <= $11 and p.availability in ('in_stock', 'low_stock') and (p.content_status = 'published' or coalesce((select (settings->>'content_status_filter')::boolean from businesses where id = $1), false) = false) order by coalesce(vp.price, (case when (p.sale_price is not null and p.sale_price < p.price and (p.sale_start is null or p.sale_start <= current_date) and (p.sale_end is null or p.sale_end >= current_date)) then p.sale_price else p.price end)) desc, ((coalesce(p.review_count, 0) * coalesce(p.rating, 0) + 30 * 4.0) / (coalesce(p.review_count, 0) + 30)) desc, p.id limit $12
-- params: ['biz', 'crema or hidratanta or ten', ['ten'], '%Anua%', ['dryness'], 'skin_type', ['dry'], 'key_ingredients', ['niacinamida'], '50 ml', 99.5, 50]

-- sort: rating_desc

    select
        p.id::text                  as id,
        p.name                      as name,
        b.name                      as brand,
        coalesce(vp.price, (case when (p.sale_price is not null and p.sale_price < p.price and (p.sale_start is null or p.sale_start <= current_date) and (p.sale_end is null or p.sale_end >= current_date)) then p.sale_price else p.price end))::float8  as price,
        p.product_url               as url,
        p.ai_summary                as ai_summary,
        p.stock_total               as stock,
        p.availability              as availability,
        img.url                     as image,
        p.rating::float8            as rating,
        p.review_count              as review_count,
        prs.top_pros[1]             as review_pro,
        prs.top_pros                as top_pros,
        (p.sale_price is not null and p.sale_price < p.price and (p.sale_start is null or p.sale_start <= current_date) and (p.sale_end is null or p.sale_end >= current_date)) as on_sale,
        -- IZI-anchor: preț ORIGINAL (tăiat), DOAR la reducere reală; altfel NULL → cardul nu
        -- afișează „de la X" fals pe o variantă mai mică. `price` rămâne efectivul curent.
        (case when (p.sale_price is not null and p.sale_price < p.price and (p.sale_start is null or p.sale_start <= current_date) and (p.sale_end is null or p.sale_end >= current_date)) then p.price end)::float8
                                    as list_price,
        -- NX-299 felia 4: voucherul. Singurul semnal comercial de tip iZi pe care catalogul REAL
        -- il sustine: `sale_price < price` = 0/2.758, dar 2.123 produse au `coupon_code` +
        -- `coupon_price`. Coloanele existau de la import, scrise de `catalog/sole_source.py`,
        -- si nu le citea NIMENI din `src/`, deci pretul cu voucher nu ajungea nicaieri.
        p.coupon_code               as coupon_code,
        p.coupon_price::float8      as coupon_price,
        p.attributes->'concerns'    as concerns,
        p.attributes                as attributes,
        -- NX-240: moneda + momentul VERIFICĂRII. `currency` fiindcă o sumă fără unitate nu e o
        -- sumă (grounding-ul o marchează UNKNOWN); `synced_at` fiindcă `updated_at` spune doar
        -- când s-a atins rândul, nu când s-a confruntat cu sursa — iar CTA-urile de comerț se
        -- sprijină pe verificare, nu pe atingere.
        p.currency                  as currency,
        p.synced_at                 as synced_at,
        vr.variants                 as variants
    from products p
    left join brands b on b.id = p.brand_id
    left join categories c on c.id = p.primary_category_id
    -- P7: `business_id` EXPLICIT și pe join, nu doar pe tabela condusă. `product_review_summaries`
    -- are cheia primară pe `product_id` singur, deci join-ul „mergea" fără el — dar izolarea nu
    -- trebuie să depindă de forma unei chei primare care se poate schimba.
    left join product_review_summaries prs
           on prs.product_id = p.id and prs.business_id = p.business_id
    left join lateral (
        select min(case when (p.sale_start is null or p.sale_start <= current_date) and (p.sale_end is null or p.sale_end >= current_date) and v.sale_price is not null
                         and v.sale_price < v.price then v.sale_price else v.price end) as price
        from product_variants v
        where v.product_id = p.id and v.business_id = p.business_id
    ) vp on true
    left join lateral (
        select pi.url from product_images pi
        where pi.product_id = p.id
        order by pi.position asc nulls last
        limit 1
    ) img on true

    left join lateral (
        select jsonb_agg(
            jsonb_build_object(
                'id', v.id::text,
                'variant_id', v.id::text,
                'label', v.label,
                'sku', v.sku,
                'price', (case when v.sale_price is not null and v.sale_price < v.price and (p.sale_start is null or p.sale_start <= current_date) and (p.sale_end is null or p.sale_end >= current_date)
                               then v.sale_price else v.price end)::float8,
                'list_price',
                    (case when v.sale_price is not null and v.sale_price < v.price and (p.sale_start is null or p.sale_start <= current_date) and (p.sale_end is null or p.sale_end >= current_date) then v.price end)::float8,
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
            ) order by (case when v.sale_price is not null and v.sale_price < v.price and (p.sale_start is null or p.sale_start <= current_date) and (p.sale_end is null or p.sale_end >= current_date)
                             then v.sale_price else v.price end) asc
        ) as variants
        from (
            select * from product_variants
            where product_id = p.id and business_id = p.business_id
            order by (case when (p.sale_start is null or p.sale_start <= current_date) and (p.sale_end is null or p.sale_end >= current_date) and sale_price is not null
                           and sale_price < price then sale_price else price end) asc
            limit 16
        ) v
    ) vr on true

 where p.business_id = $1 and p.status = 'active' and p.search_tsv @@ websearch_to_tsquery('simple', ro_unaccent($2)) and exists (select 1 from categories reqc join categories sub on sub.business_id = reqc.business_id and (sub.id = reqc.id or sub.path like reqc.path || '/%') where reqc.business_id = p.business_id and (lower(reqc.slug) = any($3::text[]) or lower(reqc.name) = any($3::text[])) and (sub.id = p.primary_category_id or exists (select 1 from product_category_map m where m.product_id = p.id and m.category_id = sub.id))) and ro_unaccent(b.name) like ro_unaccent($4) and (p.attributes->'concerns') ?| $5::text[] and (case when jsonb_typeof(p.attributes->$6) = 'array' then (p.attributes->$6) ?| $7::text[] else (p.attributes->>$6) = any($7::text[]) end) and exists (select 1 from jsonb_array_elements_text((case when jsonb_typeof(p.attributes->$8)='array' then p.attributes->$8 else '[]'::jsonb end)) fe where translate(lower(fe), 'ăâîșț', 'aaist') = any($9::text[])) and exists (select 1 from product_variants v where v.product_id = p.id and translate(lower(v.label), 'ăâîșț', 'aaist') like '%' || translate(lower($10), 'ăâîșț', 'aaist') || '%') and coalesce(vp.price, (case when (p.sale_price is not null and p.sale_price < p.price and (p.sale_start is null or p.sale_start <= current_date) and (p.sale_end is null or p.sale_end >= current_date)) then p.sale_price else p.price end)) <= $11 and p.availability in ('in_stock', 'low_stock') and (p.content_status = 'published' or coalesce((select (settings->>'content_status_filter')::boolean from businesses where id = $1), false) = false) order by ((coalesce(p.review_count, 0) * coalesce(p.rating, 0) + 30 * 4.0) / (coalesce(p.review_count, 0) + 30)) desc, coalesce(vp.price, (case when (p.sale_price is not null and p.sale_price < p.price and (p.sale_start is null or p.sale_start <= current_date) and (p.sale_end is null or p.sale_end >= current_date)) then p.sale_price else p.price end)) asc, p.id limit $12
-- params: ['biz', 'crema or hidratanta or ten', ['ten'], '%Anua%', ['dryness'], 'skin_type', ['dry'], 'key_ingredients', ['niacinamida'], '50 ml', 99.5, 50]
