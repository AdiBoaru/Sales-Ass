"""NX-314 — «mai ieftin» păstrează SUBIECTUL conversației? Read-only, pe traficul real.

Pentru fiecare tur cu `cheaper_followup` din ultimele N zile:

1. setul ANTERIOR = `conversation_traces.recommended` al celui mai recent tur din aceeași
   conversație care a arătat produse înaintea lui;
2. tipul DOMINANT al setului anterior, cu regula din `src/conversation/subject.py` (strict peste
   jumătate din produsele cu tip cunoscut, minimum 2);
3. setul SERVIT = `recommended` al turului „mai ieftin", și câte din el au același tip.

Cu `--replay`, re-rulează `search_cheaper_than` pe setul anterior de două ori, fără subiect
(ordinea de azi) și cu subiect (NX-314), și tipărește primele carduri din fiecare. Nu apelează
niciun model și nu scrie nimic.

    PYTHONPATH=. python scripts/nx314_subject_probe.py [--business sole-ro] [--days 30] [--replay]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from typing import Any

from dotenv import load_dotenv

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    sys.stdout.reconfigure(encoding="utf-8")

load_dotenv()

from src.catalog.render_text import display_name  # noqa: E402
from src.conversation.subject import (  # noqa: E402
    ConversationSubject,
    derive_subject,
    dominant_type,
)
from src.db.connection import admin_conn, close_pool, get_pool, tenant_conn  # noqa: E402
from src.db.queries.catalog import search_cheaper_than  # noqa: E402


def _ids(recommended: Any) -> list[str]:
    raw = json.loads(recommended) if isinstance(recommended, str) else recommended
    out: list[str] = []
    for r in raw or []:
        if isinstance(r, dict) and (pid := r.get("product_id") or r.get("id")):
            out.append(str(pid))
    return out


async def _cases(slug: str, days: int) -> tuple[str, list[dict[str, Any]]]:
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        biz = await conn.fetchrow("select id from businesses where slug = $1", slug)
        if biz is None:
            raise SystemExit(f"tenant necunoscut: {slug}")
        rows = await conn.fetch(
            """
            select e.turn_id, t.conversation_id, t.created_at, t.client_text, t.recommended,
                   (select coalesce(jsonb_agg(p.recommended order by p.created_at), '[]'::jsonb)
                      from conversation_traces p
                     where p.business_id = t.business_id
                       and p.conversation_id = t.conversation_id
                       and p.created_at < t.created_at
                       and jsonb_array_length(coalesce(p.recommended, '[]'::jsonb)) > 0)
                   as history
              from analytics_events e
              join conversation_traces t
                on t.turn_id = e.turn_id and t.business_id = e.business_id
             where e.business_id = $1
               and e.event_type = 'cheaper_followup'
               and e.created_at > now() - ($2::int || ' days')::interval
             order by t.created_at
            """,
            biz["id"],
            days,
        )
    return str(biz["id"]), [dict(r) for r in rows]


async def _types(business_id: str, ids: list[str]) -> dict[str, str | None]:
    if not ids:
        return {}
    async with tenant_conn(business_id) as conn:
        rows = await conn.fetch(
            "select id::text as id, attributes->>'product_type' as t from products "
            "where business_id = $1 and id = any($2::uuid[])",
            business_id,
            ids,
        )
    return {r["id"]: r["t"] for r in rows}


async def _names(business_id: str, ids: list[str]) -> dict[str, tuple[str, float | None]]:
    async with tenant_conn(business_id) as conn:
        rows = await conn.fetch(
            "select id::text as id, name, price::float8 as price from products "
            "where business_id = $1 and id = any($2::uuid[])",
            business_id,
            ids,
        )
    return {r["id"]: (r["name"], r["price"]) for r in rows}


def _line(p: dict[str, Any]) -> str:
    t = (p.get("attributes") or {}).get("product_type") or "-"
    flag = "" if p.get("subject_match", True) else "  [completare]"
    return f"      {p['price']:>7.2f}  {t:<22} {display_name(str(p['name']))[:48]}{flag}"


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--business", default="sole-ro")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--replay", action="store_true")
    args = ap.parse_args()

    business_id, cases = await _cases(args.business, args.days)
    print(f"# NX-314 — «mai ieftin» pe {args.business}, ultimele {args.days} zile")
    print(f"ture cu cheaper_followup: {len(cases)}\n")
    stats = Counter()
    shares: list[float] = []
    for c in cases:
        history = c["history"]
        history = json.loads(history) if isinstance(history, str) else (history or [])
        sets = [_ids(h) for h in history]
        prev_ids = sets[-1] if sets else []
        served_ids = _ids(c["recommended"])
        types = await _types(business_id, [i for s in sets for i in s] + served_ids)
        # Subiectul se PLIAZĂ peste toată conversația, cu aceeași funcție ca în producție: doar
        # setul imediat anterior ar rata exact cazul cardului (howto pe o singură cremă).
        subject: ConversationSubject | None = None
        for s in sets:
            subject = derive_subject(
                displayed=[{"attributes": {"product_type": types.get(i)}} for i in s],
                shelf=None,
                needs=(),
                vocab=None,
                previous=subject,
            )
        subject_type = subject.product_type if subject else None
        stats["cases"] += 1
        if not prev_ids:
            stats["no_previous_set"] += 1
        if subject_type is None:
            stats["no_subject_type"] += 1
        if subject_type is not None and dominant_type([types.get(i) for i in prev_ids]) is None:
            stats["subject_only_by_continuity"] += 1
        matched = sum(1 for i in served_ids if subject_type and types.get(i) == subject_type)
        share = (matched / len(served_ids)) if (served_ids and subject_type) else None
        if share is not None:
            shares.append(share)
        print(
            f"- {str(c['created_at'])[:16]} conv={str(c['conversation_id'])[:8]} "
            f"«{(c['client_text'] or '')[:40]}»"
        )
        print(
            f"    seturi anterioare: {len(sets)}, ultimul {len(prev_ids)} produse; "
            f"tipul subiectului={subject_type or '-'}; "
            f"servit: {len(served_ids)}, același tip: {matched}"
            + (f" ({share:.0%})" if share is not None else "")
        )
        prev_counts = Counter(types.get(i) or "-" for i in prev_ids)
        served_counts = Counter(types.get(i) or "-" for i in served_ids)
        print(f"    tipuri anterior: {dict(prev_counts.most_common())}")
        print(f"    tipuri servit:   {dict(served_counts.most_common())}")
        if args.replay and prev_ids:
            names = await _names(business_id, prev_ids)
            prices = [p for _, p in names.values() if p is not None]
            if not prices:
                continue
            baseline = min(prices)
            async with tenant_conn(business_id) as conn:
                before = await search_cheaper_than(conn, business_id, prev_ids, baseline)
                after = await search_cheaper_than(
                    conn, business_id, prev_ids, baseline, subject=subject
                )
            print(f"    replay, prag {baseline:.2f}. AZI:")
            for p in before:
                print(_line(p))
            print("    CU SUBIECT:")
            for p in after:
                print(_line(p))
        print()

    print("## Sumar")
    for k, v in sorted(stats.items()):
        print(f"- {k}: {v}")
    if shares:
        shares.sort()
        print(
            f"- proporția de carduri cu tipul subiectului, pe {len(shares)} ture cu tip dominant: "
            f"medie {sum(shares) / len(shares):.0%}, mediană {shares[len(shares) // 2]:.0%}, "
            f"zero pe {sum(1 for s in shares if s == 0)}"
        )
    await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
