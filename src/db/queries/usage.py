"""Rollup `usage_daily` (F2-3) — sursa UNICĂ pt dashboard + facturare.

Agregă o zi din `analytics_events` (ops), `messages` (mesaje) și `orders` (bani) într-un rând
`usage_daily`, idempotent (`on conflict (business_id, day) do update` → re-rularea recalculează).
Fiecare CTE filtrează EXPLICIT pe `business_id` (principiul 7), deși jobul rulează pe conn admin
(rollup-ul citește `analytics_events`, pe care bot_runtime n-are SELECT). Fereastra zilei = UTC.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import asyncpg

# Un singur statement: CTE-uri de agregare + upsert. `$1` = business_id, `$2` = ziua (date, UTC).
_ROLLUP_SQL = """
with ev as (
    select
        count(distinct conversation_id)                       as conversations,
        -- tokeni/cost agregați DOAR din event-urile llm_usage (singurele care populează aceste
        -- coloane / cheia cached_tokens). FILTER explicit = apărare: un viitor event_type care ar
        -- pune din greșeală tokens_in/cost_usd în properties nu poate polua/dubla rollup-ul.
        coalesce(sum(tokens_in) filter (where event_type = 'llm_usage'), 0)  as tokens_in,
        coalesce(sum(tokens_out) filter (where event_type = 'llm_usage'), 0) as tokens_out,
        coalesce(sum((properties->>'cached_tokens')::bigint)
                 filter (where event_type = 'llm_usage'), 0)  as cached_tokens,
        coalesce(sum(cost_usd) filter (where event_type = 'llm_usage'), 0)   as cost_usd,
        count(*) filter (
            where event_type = 'cache_lookup'
              and properties->>'layer' in ('exact', 'semantic')
        )                                                     as cache_hits,
        count(*) filter (where event_type = 'handoff_requested') as handoffs
    from analytics_events
    where business_id = $1 and (created_at at time zone 'UTC')::date = $2
),
intents as (
    select coalesce(jsonb_object_agg(route, cnt), '{}'::jsonb) as intents
    from (
        select properties->>'route' as route, count(*) as cnt
        from analytics_events
        where business_id = $1
          and event_type = 'intent_detected'
          and (created_at at time zone 'UTC')::date = $2
          and properties->>'route' is not null
        group by 1
    ) t
),
msg as (
    select
        count(*) filter (where direction = 'inbound')  as messages_in,
        count(*) filter (where direction = 'outbound') as messages_out,
        count(*) filter (
            where direction = 'outbound' and content_type = 'template'
        )                                              as templates_sent
    from messages
    where business_id = $1 and (created_at at time zone 'UTC')::date = $2
),
ord as (
    -- NX-162: split bot-led vs assisted, derivat din orders.attribution (FILTER). Perechea
    -- agregată (attribution <> 'none') rămâne pentru back-compat; split-ul o descompune —
    -- consumatorul NU trebuie să prezinte direct_bot + assisted ca un total unic (dublă numărare).
    select
        count(*) filter (where attribution <> 'none')                        as orders_attributed,
        coalesce(sum(total) filter (where attribution <> 'none'), 0)         as revenue_attributed,
        count(*) filter (where attribution = 'direct_bot')                   as orders_direct_bot,
        coalesce(sum(total) filter (where attribution = 'direct_bot'), 0)    as revenue_direct_bot,
        count(*) filter (where attribution = 'assisted')                     as orders_assisted,
        coalesce(sum(total) filter (where attribution = 'assisted'), 0)      as revenue_assisted
    from orders
    where business_id = $1 and (placed_at at time zone 'UTC')::date = $2
)
insert into usage_daily (
    business_id, day, conversations, messages_in, messages_out, templates_sent,
    tokens_in, tokens_out, cached_tokens, cost_usd, cache_hits, handoffs,
    orders_attributed, revenue_attributed,
    orders_direct_bot, revenue_direct_bot, orders_assisted, revenue_assisted, intents
)
select
    $1, $2, ev.conversations, msg.messages_in, msg.messages_out, msg.templates_sent,
    ev.tokens_in, ev.tokens_out, ev.cached_tokens, ev.cost_usd, ev.cache_hits, ev.handoffs,
    ord.orders_attributed, ord.revenue_attributed,
    ord.orders_direct_bot, ord.revenue_direct_bot, ord.orders_assisted, ord.revenue_assisted,
    intents.intents
from ev, intents, msg, ord
on conflict (business_id, day) do update set
    conversations      = excluded.conversations,
    messages_in        = excluded.messages_in,
    messages_out       = excluded.messages_out,
    templates_sent     = excluded.templates_sent,
    tokens_in          = excluded.tokens_in,
    tokens_out         = excluded.tokens_out,
    cached_tokens      = excluded.cached_tokens,
    cost_usd           = excluded.cost_usd,
    cache_hits         = excluded.cache_hits,
    handoffs           = excluded.handoffs,
    orders_attributed  = excluded.orders_attributed,
    revenue_attributed = excluded.revenue_attributed,
    orders_direct_bot  = excluded.orders_direct_bot,
    revenue_direct_bot = excluded.revenue_direct_bot,
    orders_assisted    = excluded.orders_assisted,
    revenue_assisted   = excluded.revenue_assisted,
    intents            = excluded.intents
returning *
"""


async def rollup_usage_day(conn: asyncpg.Connection, business_id: str, day: date) -> dict[str, Any]:
    """Recalculează (idempotent) rândul `usage_daily` al unei zile pentru un business.
    Întoarce rândul rezultat (intents ca dict). `conn` = admin (citește analytics_events)."""
    row = await conn.fetchrow(_ROLLUP_SQL, business_id, day)
    out = dict(row)
    if isinstance(out.get("intents"), str):
        out["intents"] = json.loads(out["intents"])
    return out


async def list_active_business_ids(conn: asyncpg.Connection) -> list[str]:
    """Id-urile businessurilor active (pt iterarea rollup-ului nocturn)."""
    rows = await conn.fetch("select id::text as id from businesses where status = 'active'")
    return [r["id"] for r in rows]
