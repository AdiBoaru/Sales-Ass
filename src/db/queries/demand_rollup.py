"""NX-217 felia 2 — rollup-ul zilnic al faptelor de cerere în `demand_daily`.

Transformă evenimentele brute (NX-163 + NX-163b) într-un flux NORMALIZAT de fapte
`(signal_kind, dimension_kind, dimension_key, conversation_id)` și îl agregă o singură dată.
Un eveniment poate produce mai multe fapte (brand ȘI categorie) — de aceea rândurile nu se
însumează peste `dimension_kind`.

Decizii care contează (motivate în docs/038_demand_daily.sql):
  • O SINGURĂ trecere peste ziua respectivă pentru TOȚI tenanții (`business_id` în GROUP BY),
    nu bucla per-business din `rollup_usage`: `analytics_events` e tabelul cel mai gras, iar
    semnalele se multiplică pe dimensiuni.
  • Idempotență prin DELETE + INSERT în aceeași tranzacție, NU upsert: upsert-ul ar lăsa
    rânduri ORFANE pentru dimensiunile care dispar la o re-rulare după corecție.
  • Fereastra zilei = interval [zi, zi+1) în UTC, sargabil (spre deosebire de predicatul
    funcțional din `rollup_usage`, care nu poate folosi indexul pe `created_at`).
  • `product_ids` malformat (scalar/obiect în loc de array) e CONȚINUT în SQL — un singur
    eveniment stricat nu are voie să oprească rollup-ul nocturn pentru toți tenanții.

Rulează pe conexiune ADMIN (`bot_runtime` n-are SELECT pe `analytics_events`).
"""

from __future__ import annotations

from datetime import date

import asyncpg

# Câte conversații de dovadă păstrăm per rând (cele mai recente). Ref-uri (P8), nu corpuri:
# suport pentru „arată-mi conversațiile", nu un dump. Aliniat cu _EVIDENCE_CAP din NX-164.
EVIDENCE_CAP = 5

# Reason-urile de `unmet_query` acceptate → `unmet_<reason>`. Allowlist explicit: un `reason`
# nou/greșit din properties NU trebuie să inventeze un `signal_kind` (constrângerea din DB l-ar
# respinge oricum și ar pica tot rollup-ul).
UNMET_REASONS = (
    "no_result",
    "named_not_found",
    "out_of_stock",
    "missing_variant",
    "price_gap",
)

# `jsonb_array_elements_text` crapă dacă nu primește array — și crapă ÎNAINTE ca WHERE să
# filtreze. Guard-ul e același ca în NX-164 (`top_products`).
_SAFE_IDS = """
    case when jsonb_typeof(e.properties->'product_ids') = 'array'
         then e.properties->'product_ids' else '[]'::jsonb end
"""

# Fereastra + filtrul de tenant sunt PARAMETRI ai CTE-ului: același flux de fapte alimentează
# rollup-ul nocturn (o zi, toți tenanții) ȘI citirea live a zilei curente (un tenant, interval).
# O singură definiție a faptelor = imposibil ca raportul „azi" să numere altfel decât istoricul.
_FACTS_BODY = f"""facts as (
    -- cerere neîmplinită × brand / categorie / produs (scalar) / variantă
    select business_id, signal_kind, 'brand' as dimension_kind,
           btrim(properties->>'brand') as dimension_key, conversation_id, created_at
    from unmet where nullif(btrim(properties->>'brand'), '') is not null
    union all
    select business_id, signal_kind, 'category',
           btrim(properties->>'category_key'), conversation_id, created_at
    from unmet where nullif(btrim(properties->>'category_key'), '') is not null
    union all
    select business_id, signal_kind, 'product',
           btrim(properties->>'product_id'), conversation_id, created_at
    from unmet where nullif(btrim(properties->>'product_id'), '') is not null
    union all
    select business_id, signal_kind, 'variant_attr',
           btrim(properties->>'variant_attr'), conversation_id, created_at
    from unmet where nullif(btrim(properties->>'variant_attr'), '') is not null
    union all
    -- cerere neîmplinită × produse (array: missing_variant / price_gap)
    select e.business_id, e.signal_kind, 'product', pid, e.conversation_id, e.created_at
    from unmet e
    cross join lateral jsonb_array_elements_text({_SAFE_IDS}) as pid
    where nullif(btrim(pid), '') is not null
    union all
    -- ce se caută: brand + categorie din product_search
    select business_id, 'requested_brand', 'brand',
           btrim(properties->>'brand'), conversation_id, created_at
    from ev
    where event_type = 'product_search'
      and nullif(btrim(properties->>'brand'), '') is not null
    union all
    select business_id, 'requested_category', 'category',
           btrim(properties->>'category_key'), conversation_id, created_at
    from ev
    where event_type = 'product_search'
      and nullif(btrim(properties->>'category_key'), '') is not null
    union all
    -- ce împinge botul / ce ajunge în coș / la checkout (produse, ca ref-uri)
    select e.business_id,
           case e.event_type
               when 'agent_recommended'     then 'recommended_product'
               when 'cart_updated'          then 'cart_product'
               else 'checkout_product'
           end,
           'product', pid, e.conversation_id, e.created_at
    from ev e
    cross join lateral jsonb_array_elements_text({_SAFE_IDS}) as pid
    where e.event_type in ('agent_recommended', 'cart_updated', 'checkout_link_created')
      and nullif(btrim(pid), '') is not null
    union all
    -- sănătatea cunoștințelor: FAQ fără răspuns (fără dimensiune — vezi nota din modul)
    select business_id, 'faq_miss', 'none', '', conversation_id, created_at
    from ev where event_type = 'faq_lookup' and properties->>'layer' = 'miss'
    union all
    -- ce informație lipsește ca să se poată recomanda (câmpul cerut la clarificare)
    select business_id, 'clarify_asked', 'clarify_field',
           btrim(properties->>'field'), conversation_id, created_at
    from ev
    where event_type = 'clarify_asked'
      and nullif(btrim(properties->>'field'), '') is not null
),
counts as (
    select business_id, signal_kind, dimension_kind, coalesce(dimension_key, '') as dimension_key,
           count(*) as request_count,
           count(distinct conversation_id) as conversation_count
    from facts
    group by 1, 2, 3, 4
),
evidence as (
    -- conversații DISTINCTE, cele mai recente întâi (o conversație cu 2 evenimente apare o dată)
    select business_id, signal_kind, dimension_kind, dimension_key,
           (array_agg(conversation_id order by last_seen desc))[1:{EVIDENCE_CAP}] as ids
    from (
        select business_id, signal_kind, dimension_kind,
               coalesce(dimension_key, '') as dimension_key,
               conversation_id, max(created_at) as last_seen
        from facts
        where conversation_id is not null
        group by 1, 2, 3, 4, 5
    ) t
    group by 1, 2, 3, 4
)
"""


def facts_cte(ev_where: str, reasons_ph: str) -> str:
    """Compune CTE-urile `ev` / `unmet` / `facts` / `counts` / `evidence` cu fereastra dată.

    `ev_where` = predicatul de selecție a evenimentelor (o zi întreagă la rollup; interval +
    tenant la citirea live a zilei curente); `reasons_ph` = placeholder-ul pentru allowlist-ul
    de reason-uri. O singură definiție a faptelor pentru ambele căi."""
    return (
        f"""
with ev as (
    select business_id, conversation_id, event_type, properties, created_at
    from analytics_events
    where {ev_where}
),
unmet as (
    select business_id, conversation_id, properties, created_at,
           'unmet_' || (properties->>'reason') as signal_kind
    from ev
    where event_type = 'unmet_query'
      and properties->>'reason' = any({reasons_ph}::text[])
),
"""
        + _FACTS_BODY
    )


_ROLLUP_DAY_WHERE = (
    "created_at >= ($1::date)::timestamp at time zone 'UTC'"
    " and created_at <  ($1::date + 1)::timestamp at time zone 'UTC'"
)

_FACTS_SQL = (
    facts_cte(_ROLLUP_DAY_WHERE, "$2")
    + """
insert into demand_daily (
    business_id, day, signal_kind, dimension_kind, dimension_key,
    request_count, conversation_count, evidence_conversation_ids
)
select c.business_id, $1::date, c.signal_kind, c.dimension_kind, c.dimension_key,
       c.request_count, c.conversation_count, coalesce(e.ids, '{}'::uuid[])
from counts c
left join evidence e
       on e.business_id = c.business_id
      and e.signal_kind = c.signal_kind
      and e.dimension_kind = c.dimension_kind
      and e.dimension_key = c.dimension_key
"""
)


async def rollup_demand_day(conn: asyncpg.Connection, day: date) -> int:
    """Recalculează (idempotent) faptele de cerere ale unei zile, pentru TOȚI tenanții.

    DELETE + INSERT într-o singură tranzacție: re-rularea după o corecție șterge și rândurile
    dimensiunilor care au dispărut (un upsert le-ar lăsa orfane, raportând cerere fantomă).
    Întoarce câte rânduri au fost scrise. `conn` = admin."""
    async with conn.transaction():
        await conn.execute("delete from demand_daily where day = $1", day)
        status = await conn.execute(_FACTS_SQL, day, list(UNMET_REASONS))
    return int(status.rsplit(" ", 1)[-1]) if status.startswith("INSERT") else 0
