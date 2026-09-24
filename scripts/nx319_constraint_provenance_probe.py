"""NX-319 — de unde vin prețul, sortarea și raftul pe care le trimite modelul? Read-only.

Pentru fiecare `search_products` din ultimele N zile (`analytics_events.tool_call`), confruntă
argumentele modelului cu ce a scris CLIENTUL (`conversation_traces.client_text` pe turul curent și
pe cele anterioare ale aceleiași conversații):

1. `price_max`: numărul apare în mesajul curent / într-unul anterior al clientului / nicăieri, iar
   când nu apare nicăieri, dacă mesajul curent e o cerere relativă de preț (`_CHEAPER_RE`, același
   detector ca ramura deterministă «mai ieftin»);
2. `sort_mode` explicit: același test de cerere relativă;
3. `category`: rostită (`corroborated_by`, ca în producție) și, dacă da, rostită doar prin numele
   unui SUBRAFT (cheia rezolvată are părinte, iar rădăcina nu apare în nicio replică a clientului).

Nu apelează niciun model și nu scrie nimic.

    PYTHONPATH=. python scripts/nx319_constraint_provenance_probe.py [--days 30]
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

from src.agent.deterministic import _CHEAPER_RE  # noqa: E402
from src.conversation.needs import corroborated_by  # noqa: E402
from src.db.connection import admin_conn, close_pool, get_pool  # noqa: E402


def _load(v: Any) -> Any:
    return json.loads(v) if isinstance(v, str) else v


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--business", default="sole-ro")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--show", type=int, default=12)
    args = ap.parse_args()

    pool = await get_pool()
    async with admin_conn(pool) as conn:
        biz = await conn.fetchval("select id from businesses where slug = $1", args.business)
        if biz is None:
            raise SystemExit(f"tenant necunoscut: {args.business}")
        rows = await conn.fetch(
            """
            select e.turn_id, e.properties, t.conversation_id, t.created_at, t.client_text,
                   (select coalesce(jsonb_agg(p.client_text order by p.created_at), '[]'::jsonb)
                      from conversation_traces p
                     where p.business_id = t.business_id
                       and p.conversation_id = t.conversation_id
                       and p.created_at < t.created_at) as earlier
              from analytics_events e
              join conversation_traces t
                on t.turn_id = e.turn_id and t.business_id = e.business_id
             where e.business_id = $1
               and e.event_type = 'tool_call'
               and e.properties->>'name' = 'search_products'
               and e.created_at > now() - make_interval(days => $2)
             order by t.created_at
            """,
            biz,
            args.days,
        )
        cats = await conn.fetch(
            """select c.slug, c.name, par.slug as parent_slug, par.name as parent_name
                 from categories c left join categories par on par.id = c.parent_id
                where c.business_id = $1""",
            biz,
        )
    await close_pool()

    by_slug = {r["slug"]: r for r in cats}
    stats: Counter[str] = Counter()
    samples: dict[str, list[str]] = {}

    def note(key: str, line: str) -> None:
        stats[key] += 1
        samples.setdefault(key, [])
        if len(samples[key]) < args.show:
            samples[key].append(line)

    for r in rows:
        a = (_load(r["properties"]) or {}).get("args") or {}
        msg = str(r["client_text"] or "")
        earlier = [str(x) for x in (_load(r["earlier"]) or []) if x]
        cheaper_now = _CHEAPER_RE.search(msg) is not None
        stats["searches"] += 1
        tag = f"{str(r['conversation_id'])[:8]} «{msg[:70]}»"

        pm = a.get("price_max")
        if pm is not None:
            stats["price_max"] += 1
            if corroborated_by(msg, pm):
                note("price_max:spoken_now", f"{tag} price_max={pm}")
            elif any(corroborated_by(t, pm) for t in earlier):
                note("price_max:spoken_earlier", f"{tag} price_max={pm}")
            elif cheaper_now:
                note("price_max:relative_now", f"{tag} price_max={pm}")
            else:
                note("price_max:UNSUPPORTED", f"{tag} price_max={pm}")

        sort = a.get("sort_mode") or "relevance"
        if sort != "relevance":
            stats["sort_explicit"] += 1
            note(
                "sort:relative_now" if cheaper_now else "sort:no_relative_request",
                f"{tag} sort={sort}",
            )

        cat = a.get("category")
        if cat:
            stats["category"] += 1
            texts = [msg, *earlier]
            if not any(corroborated_by(t, cat) for t in texts):
                note("category:guessed", f"{tag} category={cat}")
                continue
            row = by_slug.get(str(cat))
            # Cheia trimisă nu e mereu slug-ul exact (modelul scrie „fata"); o căutăm și ca frunză.
            leafs = [c for c in cats if c["slug"].endswith("-" + str(cat)) and c["parent_slug"]]
            parents = {c["parent_name"] for c in leafs} if row is None else set()
            if row is not None and row["parent_name"]:
                parents = {row["parent_name"]}
            if parents and not any(corroborated_by(t, p) for t in texts for p in parents):
                note(
                    "category:uttered_as_subshelf_only",
                    f"{tag} category={cat} (sub {'/'.join(sorted(parents))})",
                )
            else:
                note("category:uttered", f"{tag} category={cat}")

    print(f"# NX-319 — proveniența argumentelor de căutare, {args.business}, {args.days} zile\n")
    for key in sorted(stats):
        print(f"{key:40} {stats[key]}")
    for key in sorted(samples):
        if key.endswith(("UNSUPPORTED", "no_relative_request", "subshelf_only", "relative_now")):
            print(f"\n## {key}")
            for line in samples[key]:
                print("  " + line)


if __name__ == "__main__":
    asyncio.run(main())
