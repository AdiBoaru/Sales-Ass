"""NX-217 felia 3 — citirea raportului de cerere (fapte + starea curentă a catalogului).

STRICT read-side. Trei responsabilități, toate deterministe:

1. **Fereastră HIBRIDĂ** (`window_facts`): zilele ÎNCHEIATE vin din `demand_daily` (durabil,
   ieftin), iar ziua CURENTĂ direct din `analytics_events` — altfel raportul ar fi gol până la
   miezul nopții, exact când comerciantul se uită în el. Ambele căi folosesc ACELAȘI CTE de fapte
   (`facts_cte`), deci „azi" nu poate număra altfel decât istoricul.
2. **Starea curentă a catalogului** (`brand_presence`, `product_state`): răspunde la „brandul
   ăsta EXISTĂ acum?", „produsul ăsta e pe stoc ACUM?". Se citește la fiecare raport, niciodată
   materializat — altfel o acțiune rezolvată ar fi raportată la infinit.
3. **Funnel per categorie** (`category_funnel`): leagă cererea (NX-163) de bani (NX-162) —
   căutări → checkout → comenzi atribuite. Numărătorii se întorc SEPARAT; ratele se derivă la
   afișare (fapte, nu procente inventate).

`analytics_events` + `demand_daily` se citesc pe conn ADMIN (`bot_runtime` n-are SELECT).
Izolarea = `where business_id = $1` în FIECARE query (P7). Zero PII: doar dimensiuni + id-uri.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import asyncpg

from src.db.queries.demand_rollup import EVIDENCE_CAP, UNMET_REASONS, facts_cte

# Citirea live a zilei curente: aceleași fapte, filtrate pe tenant + interval.
_LIVE_FACTS_SQL = (
    facts_cte("business_id = $1 and created_at >= $2 and created_at < $3", "$4")
    + """
select c.signal_kind, c.dimension_kind, c.dimension_key,
       c.request_count, coalesce(e.ids, '{}'::uuid[]) as evidence
from counts c
left join evidence e
       on e.business_id = c.business_id
      and e.signal_kind = c.signal_kind
      and e.dimension_kind = c.dimension_kind
      and e.dimension_key = c.dimension_key
"""
)

_ROLLUP_FACTS_SQL = """
with src as (
    select signal_kind, dimension_kind, dimension_key, day, request_count,
           evidence_conversation_ids
    from demand_daily
    where business_id = $1 and day >= $2 and day < $3
),
agg as (
    select signal_kind, dimension_kind, dimension_key, sum(request_count)::bigint as request_count
    from src group by 1, 2, 3
),
ev as (
    -- dovada = conversațiile din zilele cele mai recente ale ferestrei (dedup-ul final e în
    -- Python: aceeași conversație poate apărea în dovada a două zile)
    select signal_kind, dimension_kind, dimension_key,
           (array_agg(x order by d desc))[1:%(pool)s] as evidence
    from (
        select signal_kind, dimension_kind, dimension_key, day as d,
               unnest(evidence_conversation_ids) as x
        from src
    ) t
    group by 1, 2, 3
)
select a.signal_kind, a.dimension_kind, a.dimension_key, a.request_count,
       coalesce(e.evidence, '{}'::uuid[]) as evidence
from agg a
left join ev e
       on e.signal_kind = a.signal_kind
      and e.dimension_kind = a.dimension_kind
      and e.dimension_key = a.dimension_key
""" % {"pool": EVIDENCE_CAP * 4}


def _dedup(ids: list[str], cap: int = EVIDENCE_CAP) -> list[str]:
    """Unic, păstrând ordinea (cele mai recente întâi) + cap — aceeași convenție ca NX-164."""
    out: list[str] = []
    for cid in ids:
        if cid and cid not in out:
            out.append(cid)
        if len(out) >= cap:
            break
    return out


def _key(row: Any) -> tuple[str, str, str]:
    return (row["signal_kind"], row["dimension_kind"], row["dimension_key"])


async def window_facts(
    conn: asyncpg.Connection,
    business_id: str,
    since: date,
    until: date,
    *,
    today: date | None = None,
) -> list[dict[str, Any]]:
    """Faptele de cerere pe fereastra [since, until) — rollup pentru zilele închise + evenimente
    live pentru ziua curentă (dacă intră în fereastră).

    `request_count` e ADITIV, deci se însumează peste zile. `conversation_count` NU e (o
    conversație peste miezul nopții s-ar număra de două ori) → nu se expune pe ferestre lungi.
    Dovada = conversațiile cele mai recente, deduplicate, cap `EVIDENCE_CAP`."""
    today = today or datetime.now(UTC).date()
    rollup_until = min(until, today)  # rollup-ul acoperă DOAR zilele încheiate
    merged: dict[tuple[str, str, str], dict[str, Any]] = {}

    if rollup_until > since:
        for r in await conn.fetch(_ROLLUP_FACTS_SQL, business_id, since, rollup_until):
            merged[_key(r)] = {
                "signal_kind": r["signal_kind"],
                "dimension_kind": r["dimension_kind"],
                "dimension_key": r["dimension_key"],
                "request_count": int(r["request_count"]),
                "evidence_conversation_ids": _dedup([str(x) for x in (r["evidence"] or []) if x]),
            }

    if until > today >= since:  # ziua curentă: direct din evenimente (rollup-ul n-a rulat încă)
        lo = datetime.combine(today, datetime.min.time(), tzinfo=UTC)
        hi = lo + timedelta(days=1)
        rows = await conn.fetch(_LIVE_FACTS_SQL, business_id, lo, hi, list(UNMET_REASONS))
        for r in rows:
            cur = merged.setdefault(
                _key(r),
                {
                    "signal_kind": r["signal_kind"],
                    "dimension_kind": r["dimension_kind"],
                    "dimension_key": r["dimension_key"],
                    "request_count": 0,
                    "evidence_conversation_ids": [],
                },
            )
            cur["request_count"] += int(r["request_count"])
            # dovada de AZI trece în față (cea mai recentă), apoi cea din istoric, dedup + cap
            fresh = [str(x) for x in (r["evidence"] or []) if x]
            seen: list[str] = []
            for cid in fresh + cur["evidence_conversation_ids"]:
                if cid not in seen:
                    seen.append(cid)
            cur["evidence_conversation_ids"] = seen[:EVIDENCE_CAP]

    return sorted(merged.values(), key=lambda d: (-d["request_count"], d["dimension_key"]))


async def brand_presence(
    conn: asyncpg.Connection, business_id: str, brands: list[str]
) -> dict[str, dict[str, int]]:
    """Pentru fiecare brand CERUT: câte produse are tenantul acum și câte sunt cumpărabile.

    Potrivire case-insensitive pe numele brandului (brandul cerut vine din triaj ca text
    structurat, nu ca id). `{}` pentru un brand absent din catalog = semnalul „adu-l"."""
    if not brands:
        return {}
    rows = await conn.fetch(
        """
        select lower(b.name) as brand,
               count(*)::int as products,
               count(*) filter (
                   where p.status = 'active' and p.availability in ('in_stock', 'low_stock')
               )::int as buyable
        from products p
        join brands b on b.id = p.brand_id and b.business_id = p.business_id
        where p.business_id = $1 and lower(b.name) = any($2::text[])
        group by 1
        """,
        business_id,
        [b.lower() for b in brands],
    )
    return {r["brand"]: {"products": r["products"], "buyable": r["buyable"]} for r in rows}


async def product_state(
    conn: asyncpg.Connection, business_id: str, product_ids: list[str]
) -> dict[str, dict[str, Any]]:
    """Starea CURENTĂ a produselor cerute: nume, disponibilitate, variante pe stoc și câți
    clienți s-au abonat la revenirea în stoc.

    Abonările sunt un semnal SEPARAT, mult mai tare decât o cerere — se afișează alături, nu
    însumat („41 cereri + 19 abonări", niciodată 60). Id-urile invalide (dintr-un event vechi)
    sunt sărite grațios de cast-ul filtrat, nu crapă raportul."""
    if not product_ids:
        return {}
    rows = await conn.fetch(
        """
        with wanted as (
            select id::uuid as id from unnest($2::text[]) as t(id)
            where t.id ~ '^[0-9a-fA-F-]{36}$'
        )
        select p.id::text as id, p.name, p.availability,
               (select count(*) from product_variants v
                 where v.business_id = p.business_id and v.product_id = p.id
                   and coalesce(v.stock, 0) > 0)::int as variants_in_stock,
               (select count(*) from back_in_stock_subscriptions s
                 where s.business_id = p.business_id and s.product_id = p.id
                   and s.notified_at is null)::int as subscribers
        from products p
        join wanted w on w.id = p.id
        where p.business_id = $1
        """,
        business_id,
        product_ids,
    )
    return {r["id"]: dict(r) for r in rows}


async def category_funnel(
    conn: asyncpg.Connection, business_id: str, since: date, until: date
) -> list[dict[str, Any]]:
    """Bucla cerere × conversie, per categorie: câte căutări, câte ajung la checkout, câte
    devin comenzi ATRIBUITE botului.

    Cele trei numere se întorc SEPARAT — ratele se derivă la afișare, cu numărător și numitor la
    vedere. Categoria produselor de la checkout se derivă prin join pe `products` (dimensiunea se
    calculează unde se agregă, nu în calea de răspuns a clientului). Comenzile atribuite vin din
    `order_items` → `products` → `categories`, filtrate pe `attribution <> 'none'` (NX-162)."""
    rows = await conn.fetch(
        """
        with searched as (
            select dimension_key as category, sum(request_count)::bigint as n
            from demand_daily
            where business_id = $1 and day >= $2 and day < $3
              and signal_kind = 'requested_category' and dimension_kind = 'category'
            group by 1
        ),
        checkout as (
            select c.slug as category, sum(d.request_count)::bigint as n
            from demand_daily d
            join products p on p.business_id = d.business_id and p.id::text = d.dimension_key
            join categories c on c.id = p.primary_category_id and c.business_id = p.business_id
            where d.business_id = $1 and d.day >= $2 and d.day < $3
              and d.signal_kind = 'checkout_product' and d.dimension_kind = 'product'
            group by 1
        ),
        ordered as (
            select c.slug as category, count(distinct o.id)::bigint as n
            from orders o
            join order_items oi on oi.order_id = o.id
            join products p on p.id = oi.product_id and p.business_id = o.business_id
            join categories c on c.id = p.primary_category_id and c.business_id = p.business_id
            where o.business_id = $1
              and o.attribution <> 'none'
              and (o.placed_at at time zone 'UTC')::date >= $2
              and (o.placed_at at time zone 'UTC')::date <  $3
            group by 1
        )
        select coalesce(s.category, ch.category, od.category) as category,
               coalesce(s.n, 0)  as searched,
               coalesce(ch.n, 0) as checkout,
               coalesce(od.n, 0) as ordered
        from searched s
        full join checkout ch on ch.category = s.category
        full join ordered od  on od.category = coalesce(s.category, ch.category)
        order by 2 desc nulls last
        """,
        business_id,
        since,
        until,
    )
    return [
        {
            "category": r["category"],
            "searched": int(r["searched"]),
            "checkout": int(r["checkout"]),
            "ordered": int(r["ordered"]),
        }
        for r in rows
    ]
