"""NX-388 — pe ce strat se pierde familia unei rutine: interpretare, validator sau reducer.

Pentru fiecare tur interpretat de kernel cu un act `bundle` (`conversation_traces.diagnostics
["kernel"]`), tipărește schimbările modelului (dimensiune, valoare, citat), pe cele respinse (cu
motivul), subiectul după reducer și familia planului, plus verdictul pe strat:

- `family` — planul are familie (nimic de reparat);
- `interpretation` — modelul n-a scris niciun raft și nicio nevoie cu familie în date;
- `validator` — modelul a scris un raft, iar validatorul l-a respins;
- `reducer` — raftul a fost acceptat, dar subiectul după reducer nu-l are;
- `planner` — subiectul are raftul, dar planul n-are familie (raft fără `family_by_shelf`).

    python scripts/nx388_routine_family_probe.py --business sole-ro [--since 2026-09-01]

READ-ONLY, zero model. Slug-ul se rezolvă pe `admin_conn` (operația care derivă tenantul), iar
rândurile se citesc pe `tenant_conn` cu `business_id = $1`. Textul clientului e `client_text`, forma
SAFE (NX-230). Raportul complet (cu textul) merge în `reports/nx388/` (local, gitignored); pe ecran
doar rezumatul pe straturi."""

from __future__ import annotations

import argparse
import asyncio
import collections
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    sys.stdout.reconfigure(encoding="utf-8")

BUSINESS_SQL = "select id::text from businesses where slug = $1 or id::text = $1"
TRACES_SQL = """
select turn_id::text as turn_id, created_at, client_text, diagnostics
from conversation_traces
where business_id = $1::uuid
  and created_at >= $2::date
  and diagnostics->'kernel'->'interpretation'->'acts' @> '[{"kind": "bundle"}]'::jsonb
order by created_at
"""

LAYERS = ("family", "interpretation", "validator", "reducer", "planner")


def classify(kernel: dict[str, Any]) -> dict[str, Any]:
    """Un trace de kernel → rândul raportului, cu stratul vinovat. PUR."""
    interp = kernel.get("interpretation") or {}
    changes = interp.get("changes") or []
    checked = kernel.get("checked_changes") or []
    plans = kernel.get("plans") or ([kernel["plan"]] if kernel.get("plan") else [])
    primary = plans[0] if plans else {}
    topic = (kernel.get("state_after") or {}).get("topic")
    shelf_changes = [c for c in changes if c.get("dimension") == "category"]
    rejected = [
        {
            "dimension": (c.get("change") or {}).get("dimension"),
            "value": (c.get("change") or {}).get("value"),
            "reason": c.get("rejected"),
        }
        for c in checked
        if c.get("rejected")
    ]
    shelf_rejected = [r for r in rejected if r["dimension"] == "category"]
    family = primary.get("family")
    if family:
        layer = "family"
    elif not shelf_changes:
        layer = "interpretation"
    elif shelf_rejected and len(shelf_rejected) == len(shelf_changes):
        layer = "validator"
    elif not topic:
        layer = "reducer"
    else:
        layer = "planner"
    return {
        "layer": layer,
        "executor": primary.get("executor"),
        "family": family,
        "topic": topic,
        "changes": [
            {"dimension": c.get("dimension"), "value": c.get("value"), "quote": c.get("quote")}
            for c in changes
        ],
        "rejected": rejected,
    }


async def _rows(business: str, since: date) -> list[dict[str, Any]]:
    from src.db.connection import admin_conn, close_pool, get_pool, tenant_conn  # noqa: PLC0415

    try:
        pool = await get_pool()
        async with admin_conn(pool) as conn:
            business_id = await conn.fetchval(BUSINESS_SQL, business)
        if business_id is None:
            raise SystemExit(f"tenant necunoscut: {business!r}")
        async with tenant_conn(business_id) as conn:
            rows = await conn.fetch(TRACES_SQL, business_id, since)
    finally:
        await close_pool()
    out = []
    for r in rows:
        doc = r["diagnostics"]
        doc = json.loads(doc) if isinstance(doc, str) else dict(doc or {})
        row = classify(doc.get("kernel") or {})
        row.update(
            turn_id=r["turn_id"], at=r["created_at"].isoformat(), client_text=r["client_text"]
        )
        out.append(row)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--business", required=True, help="slug sau business_id")
    ap.add_argument("--since", default="2026-09-01", help="data de început (YYYY-MM-DD)")
    args = ap.parse_args()
    rows = asyncio.run(_rows(args.business, date.fromisoformat(args.since)))
    by_layer = collections.Counter(r["layer"] for r in rows)
    print(f"{len(rows)} ture interpretate cu un act bundle")
    for layer in LAYERS:
        print(f"  {layer:15} {by_layer.get(layer, 0)}")
    for r in rows:
        if r["layer"] != "family":
            print(f"  {r['turn_id'][:8]} {r['layer']:14} executor={r['executor']}")
    out = ROOT / "reports" / "nx388" / f"routine-family-{args.business}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"raport: {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
