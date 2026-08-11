"""NX-231 — proba de burst: 2× mai multe tururi decât poolul, cu un LLM STUB cu delay controlat.

Ăsta e „manual drive"-ul cerut de card. Nu măsoară calitatea răspunsului și nu cheltuie NIMIC la
OpenAI: modelul e un stub care doar doarme `--llm-delay-ms`. Ce demonstrează e o singură
afirmație, verificabilă:

    în timp ce N tururi sunt în faza lor „de model", conexiunile lor NU sunt în pool-ul ocupat,
    iar un query de health (sau al altui tenant) răspunde imediat.

Cum: pornește `--turns` tururi concurente pe calea REALĂ (`handle_turn` → pipeline → commit), iar
în paralel un sampler citește `bot_pool_stats()` la fiecare 25ms. Raportul dă ocuparea maximă a
poolului, timpul de răspuns al query-ului de health în plin burst și, din `op_metrics`, cât timp
a ținut FIECARE operație o conexiune.

Rulare (din rădăcina proiectului):

    # ACUM (conn-per-op)
    python scripts/sim/conn_hold_probe.py --turns 20 --llm-delay-ms 800 \
        --out reports/nx231-after.json

    # cu idle-held real (proxy de timing pe conexiune)
    DB_QUERY_TIMING_ENABLED=1 python scripts/sim/conn_hold_probe.py --turns 20 --out ...

Cere DB live (scrie date `sim:`, curățabile cu `scripts/sim/cleanup.py`). NU cere OPENAI_API_KEY.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
import uuid
from pathlib import Path

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.db import op_metrics  # noqa: E402
from src.db.connection import (  # noqa: E402
    admin_conn,
    bot_pool_stats,
    close_pool,
    get_pool,
    tenant_conn,
)
from src.db.provider import tenant_db  # noqa: E402
from src.db.queries.businesses import load_business  # noqa: E402
from src.db.queries.channels import upsert_channel  # noqa: E402
from src.worker import processor as proc  # noqa: E402
from src.worker.processor import handle_turn  # noqa: E402

DEMO_BIZ = "6098812a-50fc-44bd-a1ba-bc77e6399158"
SIM_PROVIDER = "SIM-DRIVER"


class StubLLM:
    """Model fals: aceeași formă de apel, latență controlată, ZERO cost.

    Delay-ul e esența probei — el ocupă exact intervalul în care, înainte de NX-231, o conexiune
    stătea pinned degeaba."""

    def __init__(self, delay_ms: int) -> None:
        self.delay_s = delay_ms / 1000.0
        self.calls = 0

    async def _wait(self):
        self.calls += 1
        await asyncio.sleep(self.delay_s)

    async def embed(self, texts):
        await self._wait()
        return [[0.0] * 1536 for _ in texts]

    async def classify_json(self, system, user):
        await self._wait()
        # rută „simple" → răspuns direct, fără agent (proba e despre conexiuni, nu despre calitate)
        return {"route": "simple", "reply": "Salut! Cu ce te pot ajuta?"}

    async def run_tool_loop(self, system, user, tools, execute):
        await self._wait()
        return "Îți răspund imediat."


async def _sample_pool(stop: asyncio.Event, samples: list[dict], interval_s: float = 0.025):
    while not stop.is_set():
        samples.append(bot_pool_stats())
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_s)
        except TimeoutError:
            pass


async def _health_probe(stop: asyncio.Event, latencies: list[float], interval_s: float = 0.05):
    """Un query trivial pe tenant path, repetat. Latența LUI e verdictul: dacă urcă la nivelul
    delay-ului de model, înseamnă că tururile țin conexiunile peste apelul extern."""
    while not stop.is_set():
        t0 = time.perf_counter()
        try:
            async with tenant_conn(DEMO_BIZ) as conn:
                await conn.fetchval("select 1")
            latencies.append((time.perf_counter() - t0) * 1000.0)
        except Exception as e:  # noqa: BLE001 — un health eșuat e tot un rezultat
            latencies.append(float("inf"))
            print(f"   health: EȘEC ({type(e).__name__})")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_s)
        except TimeoutError:
            pass


async def _one_turn(biz, channel_id: str, idx: int, results: list[dict]) -> None:
    event = {
        "channel_kind": "whatsapp",
        "channel_account_id": SIM_PROVIDER,
        "sender_external_id": f"sim:burst:{uuid.uuid4().hex[:8]}",
        "provider_msg_id": f"sim.{uuid.uuid4().hex}",
        "content_type": "text",
        "body": "salut",
        "sender_name": "Client",
    }
    acc, token = op_metrics.push()
    t0 = time.perf_counter()
    try:
        await handle_turn(tenant_db(DEMO_BIZ), biz, channel_id, event, redis=None)
    finally:
        op_metrics.pop(token)
    results.append(
        {
            "turn": idx,
            "wall_ms": round((time.perf_counter() - t0) * 1000.0, 1),
            "checkouts": acc.checkouts,
            "hold_ms": round(acc.hold_ms, 1),
            "checkout_ms": round(acc.checkout_ms, 1),
            "query_ms": round(acc.query_ms, 1),
            "idle_held_ms": (None if acc.idle_held_ms is None else round(acc.idle_held_ms, 1)),
            "by_operation": {k: v.as_dict() for k, v in acc.by_op.items()},
        }
    )


def _pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(p * (len(ordered) - 1))))]


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--turns", type=int, default=20, help="tururi concurente (țintă: 2× pool)")
    ap.add_argument("--llm-delay-ms", type=int, default=800, help="latența stubului de model")
    ap.add_argument("--out", type=Path, default=None, help="raport JSON (before/after)")
    ap.add_argument("--label", default="after", help="eticheta rulării în raport")
    args = ap.parse_args()

    pool = await get_pool()
    async with admin_conn(pool) as conn:
        ch = await upsert_channel(conn, DEMO_BIZ, "whatsapp", SIM_PROVIDER, display_name="Sim")
    channel_id = ch["id"]
    async with tenant_conn(DEMO_BIZ) as conn:
        biz = await load_business(conn, DEMO_BIZ)

    stub = StubLLM(args.llm_delay_ms)
    # Injectăm stubul pe seam-ul folosit de processor (`get_llm`) — zero apeluri reale.
    proc.get_llm = lambda: stub  # type: ignore[assignment]

    pool_max = (bot_pool_stats() or {}).get("pool_max", 10)
    print(f"\n{'=' * 78}")
    print(f"NX-231 BURST PROBE — {args.turns} tururi concurente, pool max={pool_max}")
    print(f"stub LLM: {args.llm_delay_ms}ms/apel (ZERO cost OpenAI)")
    print(f"{'=' * 78}\n")

    stop = asyncio.Event()
    samples: list[dict] = []
    health: list[float] = []
    watchers = [
        asyncio.create_task(_sample_pool(stop, samples)),
        asyncio.create_task(_health_probe(stop, health)),
    ]
    results: list[dict] = []
    t0 = time.perf_counter()
    await asyncio.gather(*(_one_turn(biz, channel_id, i, results) for i in range(args.turns)))
    burst_ms = (time.perf_counter() - t0) * 1000.0
    stop.set()
    await asyncio.gather(*watchers)

    in_use = [s.get("pool_in_use", 0) for s in samples]
    holds = [r["hold_ms"] for r in results]
    walls = [r["wall_ms"] for r in results]
    report = {
        "label": args.label,
        "turns": args.turns,
        "llm_delay_ms": args.llm_delay_ms,
        "pool_max": pool_max,
        "burst_wall_ms": round(burst_ms, 1),
        "pool_in_use_peak": max(in_use) if in_use else 0,
        "pool_in_use_avg": round(statistics.fmean(in_use), 2) if in_use else 0,
        "health_query_ms_p50": round(_pct(health, 0.5), 1),
        "health_query_ms_p95": round(_pct(health, 0.95), 1),
        "health_samples": len(health),
        "turn_hold_ms_avg": round(statistics.fmean(holds), 1) if holds else 0,
        "turn_wall_ms_avg": round(statistics.fmean(walls), 1) if walls else 0,
        "hold_over_wall_pct": (
            round(100 * statistics.fmean(holds) / statistics.fmean(walls), 1) if walls else 0
        ),
        "llm_calls": stub.calls,
        "turns_detail": results,
    }

    print(
        f"pool in_use: vârf={report['pool_in_use_peak']}/{pool_max}  "
        f"mediu={report['pool_in_use_avg']}"
    )
    print(
        f"health `select 1` în plin burst: p50={report['health_query_ms_p50']}ms  "
        f"p95={report['health_query_ms_p95']}ms  ({len(health)} probe)"
    )
    print(
        f"per tur: wall={report['turn_wall_ms_avg']}ms  hold(total conexiune)="
        f"{report['turn_hold_ms_avg']}ms  → hold/wall={report['hold_over_wall_pct']}%"
    )
    print(
        "\nCITIRE: hold/wall aproape de 100% = conexiunea e ținută pe durata modelului (starea de\n"
        "dinainte de NX-231). Aproape de 0% = conexiunea aparține operației. Un health p95 la\n"
        f"nivelul delay-ului ({args.llm_delay_ms}ms) ar însemna că poolul e blocat de tururi.\n"
    )
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"raport scris în {args.out}\n")

    await close_pool()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
