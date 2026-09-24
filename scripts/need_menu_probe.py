"""NX-322 — linia de bază pentru meniul de nevoi. Read-only, zero model.

Înainte de aprinderea `NEED_MENU_ENABLED` (D15), două întrebări pe traficul real, fiecare cu
numitorul ei:

1. **Câte nevoi s-au pierdut la rezoluție?** `vocabulary_resolved` pe dimensiunea `concerns` cu
   verdict `unknown` (fraza modelului nu s-a potrivit pe nicio cheie), pe căutare și pe rutină.
   Ce rezolvă meniul: cheia o alege modelul, deci fraza nu mai trebuie să se potrivească exact.
2. **Câte nevoi trimise de model nu apar în mesajele clientului?** Pe `routine_plan` argumentele se
   loghează abia din NX-321 (fără text), deci măsurătoarea e pe `search_products`, unde
   `tool_call.args.concerns` poartă încă fraza (trunchiată). Aceeași coroborare ca în producție
   (`corroborated_by`, pe mesajele clientului din `conversation_traces`).

Plus meniul pe care l-ar primi modelul azi (chei + etichete), ca să se vadă înainte ce se aprinde.

    PYTHONPATH=. python scripts/need_menu_probe.py [--business sole-ro] [--days 30]

Rezultatul merge în `reports/nx322/baseline.json`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import types
from collections import Counter
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    sys.stdout.reconfigure(encoding="utf-8")

load_dotenv()

from src.catalog.need_menu import build_menu  # noqa: E402
from src.catalog.vocabulary import facet_overlays, load_vocabulary  # noqa: E402
from src.conversation.needs import corroborated_by  # noqa: E402
from src.db.connection import admin_conn, close_pool, get_pool  # noqa: E402
from src.domain.loader import load_domain_pack  # noqa: E402


def _load(v: Any) -> Any:
    return json.loads(v) if isinstance(v, str) else v


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--business", default="sole-ro")
    ap.add_argument("--days", type=int, default=30)
    args = ap.parse_args()

    pool = await get_pool()
    async with admin_conn(pool) as conn:
        biz = await conn.fetchrow(
            "select id::text as id, vertical, settings from businesses where slug = $1",
            args.business,
        )
        if biz is None:
            raise SystemExit(f"tenant necunoscut: {args.business}")
        pack = load_domain_pack(
            types.SimpleNamespace(vertical=biz["vertical"], settings=_load(biz["settings"]))
        )
        vocab = await load_vocabulary(conn, biz["id"])
        resolved = await conn.fetch(
            """
            select properties from analytics_events
             where business_id = $1 and event_type = 'vocabulary_resolved'
               and properties->>'dimension' = 'concerns'
               and created_at > now() - make_interval(days => $2)
            """,
            biz["id"],
            args.days,
        )
        calls = await conn.fetch(
            """
            select e.properties, t.client_text,
                   (select coalesce(jsonb_agg(p.client_text), '[]'::jsonb)
                      from conversation_traces p
                     where p.business_id = t.business_id
                       and p.conversation_id = t.conversation_id
                       and p.created_at < t.created_at) as earlier
              from analytics_events e
              join conversation_traces t
                on t.turn_id = e.turn_id and t.business_id = e.business_id
             where e.business_id = $1 and e.event_type = 'tool_call'
               and e.properties->>'name' = 'search_products'
               and e.created_at > now() - make_interval(days => $2)
            """,
            biz["id"],
            args.days,
        )
    await close_pool()

    by_status = Counter(
        (_load(r["properties"]).get("consumer") or "search", _load(r["properties"]).get("status"))
        for r in resolved
    )
    sent = spoken = 0
    for row in calls:
        concerns = (_load(row["properties"]).get("args") or {}).get("concerns") or []
        texts = [row["client_text"] or "", *(_load(row["earlier"]) or [])]
        for term in concerns:
            if not isinstance(term, str):
                continue
            sent += 1
            spoken += any(corroborated_by(t, term) for t in texts)

    menu = build_menu(pack, vocab.dimensions, facet_overlays(pack, vocab.facet_names), "ro")
    report = {
        "business": args.business,
        "days": args.days,
        "vocabulary_resolved_concerns": {f"{c}:{s}": n for (c, s), n in sorted(by_status.items())},
        "search_concern_terms": {"sent": sent, "spoken_by_client": spoken},
        "menu": [{"dimension": o.dimension, "key": o.key, "label": o.label} for o in menu.options],
        "partitioning": sorted(menu.partitioning),
    }
    out = Path("reports/nx322/baseline.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"rezoluții `concerns` ({args.days} zile): {report['vocabulary_resolved_concerns']}")
    print(f"termeni trimiși de model pe căutare: {sent}, rostiți de client: {spoken}")
    print(f"meniu: {len(menu.options)} chei, partiționante: {sorted(menu.partitioning)}")
    print(f"raport: {out}")


if __name__ == "__main__":
    asyncio.run(main())
