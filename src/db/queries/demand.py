"""NX-164 — Demand Queries: read-side agregat peste faptele de cerere (NX-163) + venit (NX-162).

STRICT read-side: doar SELECT/agregare — zero scriere, zero LLM, zero estimări de bani, zero scoruri
de inferență (fapte numărate, nu ghicit). Transformă evenimentele deja capturate în răspunsuri de
business (ce se cere, ce lipsește, ce produse se recomandă/adaugă în coș/checkout). Fiecare rând
poartă `evidence_conversation_ids` = drilldown la dovada reală — DOAR ids (P12: niciodată telefoane/
corpuri; consumatorul FE redactează defensiv). Oportunitatea în bani se DERIVĂ la citire în UI.

`analytics_events` se citește pe conn ADMIN (bot_runtime n-are SELECT — ca rollup-ul). Izolarea
tenant = `WHERE business_id = $1` în FIECARE query (P7, mecanism primar). Fereastra = [since, until)
pe `created_at` (index `idx_events_business_type` acoperă business_id + event_type + created_at).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import asyncpg

# Cât de multe conversation_id-uri de drilldown întoarcem per rând (cele mai recente). Ref-uri (P8),
# nu corpuri — suport pentru „arată-mi conversațiile", nu un dump.
_EVIDENCE_CAP = 5
# Câte id-uri agregă SQL-ul înainte de dedup/capare în Python (recente; dedup-ul păstrează ordinea).
_EVIDENCE_POOL = 20


def _evidence(raw: list[str] | None, cap: int = _EVIDENCE_CAP) -> list[str]:
    """Dedup păstrând ordinea (cele mai recente întâi) + capare. `raw` vine deja filtrat de NULL-uri
    din SQL; aici doar unic + cap (o conversație cu 2 evenimente nu apare de 2 ori în dovadă)."""
    out: list[str] = []
    for cid in raw or []:
        if cid and cid not in out:
            out.append(cid)
        if len(out) >= cap:
            break
    return out


async def top_unmet(
    conn: asyncpg.Connection,
    business_id: str,
    since: datetime,
    until: datetime,
    *,
    reason: str,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Cerere neîmplinită grupată pe brand/categorie, pentru un `reason` dat (`no_result` /
    `named_not_found` azi; `out_of_stock`/`missing_variant` întorc gol până le emite NX-163b).
    `WHERE business_id = $1` (P7). Rândul = {brand, category_key, request_count, evidence...}."""
    rows = await conn.fetch(
        f"""
        select
            properties->>'brand'        as brand,
            properties->>'category_key' as category_key,
            count(*)                    as request_count,
            (array_agg(conversation_id::text order by created_at desc)
                filter (where conversation_id is not null))[1:{_EVIDENCE_POOL}] as evidence
        from analytics_events
        where business_id = $1
          and event_type = 'unmet_query'
          and properties->>'reason' = $2
          and created_at >= $3 and created_at < $4
        group by properties->>'brand', properties->>'category_key'
        order by request_count desc, brand nulls last
        limit $5
        """,
        business_id,
        reason,
        since,
        until,
        limit,
    )
    return [
        {
            "brand": r["brand"],
            "category_key": r["category_key"],
            "request_count": r["request_count"],
            "evidence_conversation_ids": _evidence(r["evidence"]),
        }
        for r in rows
    ]


async def top_requested_brands(
    conn: asyncpg.Connection,
    business_id: str,
    since: datetime,
    until: datetime,
    *,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Branduri cerute cel mai des în `product_search` (ce limbaj/mărci folosesc clienții).
    Doar rândurile cu brand structural (NULL sărit). `WHERE business_id = $1` (P7)."""
    rows = await conn.fetch(
        f"""
        select
            properties->>'brand' as brand,
            count(*)             as request_count,
            (array_agg(conversation_id::text order by created_at desc)
                filter (where conversation_id is not null))[1:{_EVIDENCE_POOL}] as evidence
        from analytics_events
        where business_id = $1
          and event_type = 'product_search'
          and properties->>'brand' is not null
          and created_at >= $2 and created_at < $3
        group by properties->>'brand'
        order by request_count desc
        limit $4
        """,
        business_id,
        since,
        until,
        limit,
    )
    return [
        {
            "brand": r["brand"],
            "request_count": r["request_count"],
            "evidence_conversation_ids": _evidence(r["evidence"]),
        }
        for r in rows
    ]


async def top_products(
    conn: asyncpg.Connection,
    business_id: str,
    since: datetime,
    until: datetime,
    *,
    event_type: str,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Produse cel mai des menționate într-un event cu `product_ids[]` (`agent_recommended` /
    `cart_updated` / `checkout_link_created`) — ce împinge/adaugă/duce-la-checkout botul. Unnest
    determinist; evenimentele fără `product_ids` SAU cu unul malformat (scalar/obiect) sunt SĂRITE
    grațios prin `CASE ... ELSE '[]'::jsonb` în lateral — `jsonb_array_elements_text` primește MEREU
    un array, altfel ar crăpa ÎNAINTE ca WHERE să filtreze. `WHERE business_id = $1` (P7)."""
    rows = await conn.fetch(
        f"""
        select
            pid          as product_id,
            count(*)     as mention_count,
            (array_agg(ae.conversation_id::text order by ae.created_at desc)
                filter (where ae.conversation_id is not null))[1:{_EVIDENCE_POOL}] as evidence
        from analytics_events ae
        cross join lateral jsonb_array_elements_text(
            case when jsonb_typeof(ae.properties->'product_ids') = 'array'
                 then ae.properties->'product_ids'
                 else '[]'::jsonb
            end
        ) as pid
        where ae.business_id = $1
          and ae.event_type = $2
          and ae.created_at >= $3 and ae.created_at < $4
        group by pid
        order by mention_count desc
        limit $5
        """,
        business_id,
        event_type,
        since,
        until,
        limit,
    )
    return [
        {
            "product_id": r["product_id"],
            "mention_count": r["mention_count"],
            "evidence_conversation_ids": _evidence(r["evidence"]),
        }
        for r in rows
    ]


async def revenue_summary(
    conn: asyncpg.Connection,
    business_id: str,
    since_day: date,
    until_day: date,
) -> dict[str, Any]:
    """North-star peste `usage_daily` (repară „fără cititor de raport", Defect 3): venit atribuit +
    split bot-led/assisted (NX-162) pe fereastra de zile [since_day, until_day). Bot-led și assisted
    întorși SEPARAT — NICIODATĂ însumați (dublă numărare). `WHERE business_id = $1` (P7). Sumele
    numerice → float pentru afișare (nu contabilitate)."""
    row = await conn.fetchrow(
        """
        select
            coalesce(sum(orders_attributed),  0) as orders_attributed,
            coalesce(sum(revenue_attributed), 0) as revenue_attributed,
            coalesce(sum(orders_direct_bot),  0) as orders_direct_bot,
            coalesce(sum(revenue_direct_bot), 0) as revenue_direct_bot,
            coalesce(sum(orders_assisted),    0) as orders_assisted,
            coalesce(sum(revenue_assisted),   0) as revenue_assisted
        from usage_daily
        where business_id = $1 and day >= $2 and day < $3
        """,
        business_id,
        since_day,
        until_day,
    )
    return {
        "orders_attributed": int(row["orders_attributed"]),
        "revenue_attributed": float(row["revenue_attributed"]),
        "orders_direct_bot": int(row["orders_direct_bot"]),
        "revenue_direct_bot": float(row["revenue_direct_bot"]),
        "orders_assisted": int(row["orders_assisted"]),
        "revenue_assisted": float(row["revenue_assisted"]),
    }


async def demand_report(
    conn: asyncpg.Connection,
    business_id: str,
    since: datetime,
    until: datetime,
    *,
    limit: int = 20,
) -> dict[str, list[dict[str, Any]]]:
    """Conveniență: raportul de cerere complet (secțiunile read-side) într-un singur dict, gata de
    serializat spre FE. Pur agregare peste faptele NX-163 — fără estimări, fără PII (doar ids)."""
    return {
        "zero_result": await top_unmet(
            conn, business_id, since, until, reason="no_result", limit=limit
        ),
        "named_not_found": await top_unmet(
            conn, business_id, since, until, reason="named_not_found", limit=limit
        ),
        "top_requested_brands": await top_requested_brands(
            conn, business_id, since, until, limit=limit
        ),
        "top_recommended": await top_products(
            conn, business_id, since, until, event_type="agent_recommended", limit=limit
        ),
        "top_added_to_cart": await top_products(
            conn, business_id, since, until, event_type="cart_updated", limit=limit
        ),
        "top_checkout": await top_products(
            conn, business_id, since, until, event_type="checkout_link_created", limit=limit
        ),
    }
