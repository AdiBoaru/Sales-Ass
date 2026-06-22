"""Raport de cost LLM (cost-obs NX-103) — citește `analytics_events.llm_usage` și defalcă consumul.

Răspunde la „cât a costat?" la trei granularități:
  • o conversație:        python scripts/sim/cost_report.py --conversation <uuid>
  • o zi (UTC):           python scripts/sim/cost_report.py --day 2026-06-20
  • azi (implicit):       python scripts/sim/cost_report.py

Defalcă pe FAZĂ (reply vs fundal), pe MODEL (nano/mini/embeddings) și pe STAGIU (triaj/agent/...),
arată economia din prompt caching și costul mediu pe conversație/mesaj. Sursa e analytics_events
(adevărul granular); rollup-ul `usage_daily` rămâne agregatul nocturn pt dashboard/facturare.

Inspecție pe admin_conn (bot_runtime n-are SELECT pe analytics_events), scoped pe DEMO_BIZ.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.db.connection import admin_conn, close_pool, get_pool  # noqa: E402

DEMO_BIZ = "6098812a-50fc-44bd-a1ba-bc77e6399158"


def _jsonb(v):
    if isinstance(v, str):
        try:
            return json.loads(v or "{}")
        except ValueError:
            return {}
    return v or {}


def _fmt_usd(v: float) -> str:
    return f"${v:.6f}" if 0 < v < 0.01 else f"${v:.4f}"


def _empty():
    return {"calls": 0, "tokens_in": 0, "tokens_out": 0, "cached_tokens": 0, "cost_usd": 0.0}


def _merge(dst: dict, key: str, row: dict) -> None:
    into = dst.setdefault(key, _empty())
    for k in ("calls", "tokens_in", "tokens_out", "cached_tokens", "cost_usd"):
        into[k] += row.get(k, 0) or 0


async def _fetch_rows(conn, *, conversation_id: str | None, day: str | None):
    if conversation_id:
        return await conn.fetch(
            "select conversation_id, properties from analytics_events "
            "where business_id=$1 and conversation_id=$2 and event_type='llm_usage'",
            DEMO_BIZ,
            conversation_id,
        )
    return await conn.fetch(
        "select conversation_id, properties from analytics_events "
        "where business_id=$1 and event_type='llm_usage' "
        "and (created_at at time zone 'UTC')::date = $2::date",
        DEMO_BIZ,
        day,
    )


def _aggregate(rows):
    totals = _empty()
    totals["savings_usd"] = 0.0
    by_phase: dict = {}
    by_model: dict = {}
    by_stage: dict = {}
    convs: set = set()
    for r in rows:
        p = _jsonb(r["properties"])
        convs.add(r["conversation_id"])
        self_row = {
            "calls": int(p.get("llm_calls") or 0),
            "tokens_in": int(p.get("tokens_in") or 0),
            "tokens_out": int(p.get("tokens_out") or 0),
            "cached_tokens": int(p.get("cached_tokens") or 0),
            "cost_usd": float(p.get("cost_usd") or 0.0),
        }
        for k in ("calls", "tokens_in", "tokens_out", "cached_tokens", "cost_usd"):
            totals[k] += self_row[k]
        totals["savings_usd"] += float(p.get("savings_usd") or 0.0)
        _merge(by_phase, p.get("phase") or "turn", self_row)
        for model, row in (p.get("by_model") or {}).items():
            _merge(by_model, model, row)
        for stage, row in (p.get("by_stage") or {}).items():
            _merge(by_stage, stage, row)
    return totals, by_phase, by_model, by_stage, len(convs)


def _print_table(title: str, breakdown: dict) -> None:
    if not breakdown:
        return
    print(f"\n  {title}")
    for name, row in sorted(breakdown.items(), key=lambda kv: -(kv[1].get("cost_usd") or 0)):
        print(
            f"    {name:<22} {_fmt_usd(row['cost_usd']):>11}  "
            f"in {row['tokens_in']:>8,}  out {row['tokens_out']:>7,}  "
            f"cached {row['cached_tokens']:>7,}  {row['calls']:>3} apeluri"
        )


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--conversation", help="UUID-ul unei conversații")
    ap.add_argument("--day", help="zi UTC YYYY-MM-DD (implicit: azi)")
    a = ap.parse_args()

    pool = await get_pool()
    try:
        async with admin_conn(pool) as conn:
            day = a.day
            if not a.conversation and not day:
                day = (await conn.fetchval("select (now() at time zone 'UTC')::date")).isoformat()
            rows = await _fetch_rows(conn, conversation_id=a.conversation, day=day)
    finally:
        await close_pool()

    scope = f"conversație {a.conversation}" if a.conversation else f"ziua {day} (UTC)"
    print(f"\n📊 Raport cost LLM — {scope}  ·  business {DEMO_BIZ}")
    if not rows:
        print("  (niciun event llm_usage — fără consum sau date încă negenerate)")
        return 0

    totals, by_phase, by_model, by_stage, n_convs = _aggregate(rows)
    tin = totals["tokens_in"] or 1
    cache_pct = round(100 * totals["cached_tokens"] / tin)
    print("\n  TOTAL")
    print(
        f"    cost {_fmt_usd(totals['cost_usd'])}  ·  "
        f"economie cache {_fmt_usd(totals['savings_usd'])}  ·  "
        f"{totals['calls']} apeluri  ·  {n_convs} conversații"
    )
    print(
        f"    tokeni in {totals['tokens_in']:,} (cached {totals['cached_tokens']:,} = {cache_pct}%)"
        f"  ·  out {totals['tokens_out']:,}"
    )
    if n_convs:
        print(
            f"    cost/conversație {_fmt_usd(totals['cost_usd'] / n_convs)}  ·  "
            f"cost/apel {_fmt_usd(totals['cost_usd'] / (totals['calls'] or 1))}"
        )
    _print_table("PE FAZĂ (reply vs fundal)", by_phase)
    _print_table("PE MODEL", by_model)
    _print_table("PE STAGIU (doar reply)", by_stage)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
