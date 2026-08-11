"""Pool-hold probe — măsoară cât din durata unui tur ține conexiunea DB pinned degeaba.

Întrebarea de arhitectură (review 5.3): `handle_turn` ține O conexiune din `bot_pool`
(max_size=10) pe TOATĂ durata turului, INCLUSIV apelurile LLM (triaj + agent + tools +
post-tur). O conexiune blocată cât rulează OpenAI nu face nimic pe DB → sub concurență
poolul se epuizează pe timp de rețea, nu pe muncă de DB.

Nu ne trebuie concurență ca să răspundem (și nici n-am putea fidel: sim-ul lovește pooler-ul
Supabase capat la 15, prod lovește conexiune directă — altă topologie). Măsurăm un RAPORT
per-tur, determinist pe UN singur tur:

    held_ms      = cât timp e conexiunea pinned de handle_turn
    db_active_ms = cât din el chiar execută query-uri (proxy care cronometrează fiecare apel)
    idle_held_ms = held - db_active  ≈ timpul pinned dar idle (majoritar LLM)

Apoi contention-ul e aritmetică (legea lui Little): cu pool=P, saturezi la debitul
λ_max = P / held_s. Dacă am elibera conexiunea pe durata LLM, held → db_active_s și
λ_max crește cu factorul held/db_active. Ăsta e headroom-ul pe care fix-ul îl cumpără.

Rulează (din rădăcina proiectului):  python scripts/sim/pool_probe.py
Scrie date `sim:` (curățabile cu scripts/sim/cleanup.py). Cere OPENAI_API_KEY + DB live.
"""

from __future__ import annotations

import asyncio
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

from src.agent.llm import get_llm  # noqa: E402
from src.db import op_metrics  # noqa: E402
from src.db.connection import admin_conn, close_pool, get_pool, tenant_conn  # noqa: E402
from src.db.provider import tenant_db  # noqa: E402
from src.db.queries.businesses import load_business  # noqa: E402
from src.db.queries.channels import upsert_channel  # noqa: E402
from src.worker.processor import handle_turn  # noqa: E402

DEMO_BIZ = "6098812a-50fc-44bd-a1ba-bc77e6399158"
SIM_PROVIDER = "SIM-DRIVER"

# Mini-conversație reprezentativă: 1 salut + 2 tururi de sales (forțează triaj + agent + tools,
# calea care ține conexiunea cel mai mult). Turul 1 creează contact/conv (one-off).
SCRIPT = [
    "buna ziua",
    "caut o crema hidratanta pentru ten uscat sub 150 lei",
    "si ceva mai ieftin?",
]


class TimedConn:
    """Proxy transparent peste conexiunea asyncpg care ACUMULEAZĂ timpul petrecut în apelurile
    DB (execute/fetch/...). Tot restul e delegat prin __getattr__ (inclusiv `.transaction()`,
    care întoarce Transaction pe conexiunea reală — execuțiile nested folosesc ACELAȘI proxy →
    timate). BEGIN/COMMIT emise de Transaction rămân nemăsurate (mic rezidual, contat ca idle →
    subestimează idle-ul, deci concluzia e conservatoare)."""

    __slots__ = ("_c", "acc")

    def __init__(self, conn, acc: dict):
        self._c = conn
        self.acc = acc

    def __getattr__(self, name):
        return getattr(self._c, name)

    async def _timed(self, meth, *a, **k):
        t = time.perf_counter()
        try:
            return await meth(*a, **k)
        finally:
            self.acc["db_ms"] += (time.perf_counter() - t) * 1000.0
            self.acc["calls"] += 1

    async def execute(self, *a, **k):
        return await self._timed(self._c.execute, *a, **k)

    async def executemany(self, *a, **k):
        return await self._timed(self._c.executemany, *a, **k)

    async def fetch(self, *a, **k):
        return await self._timed(self._c.fetch, *a, **k)

    async def fetchrow(self, *a, **k):
        return await self._timed(self._c.fetchrow, *a, **k)

    async def fetchval(self, *a, **k):
        return await self._timed(self._c.fetchval, *a, **k)


def _fmt(ms: float) -> str:
    return f"{ms / 1000:.2f}s" if ms >= 1000 else f"{ms:.0f}ms"


async def main() -> int:
    if get_llm() is None:
        print("EROARE: get_llm() e None — setează OPENAI_API_KEY (fără LLM nu are sens măsurarea).")
        return 1

    pool = await get_pool()
    async with admin_conn(pool) as conn:
        ch = await upsert_channel(
            conn, DEMO_BIZ, "whatsapp", SIM_PROVIDER, display_name="Sim Driver"
        )
    channel_id = ch["id"]
    async with tenant_conn(DEMO_BIZ) as conn:
        biz = await load_business(conn, DEMO_BIZ)

    sender = f"sim:poolprobe:{uuid.uuid4().hex[:8]}"
    print(f"\n{'=' * 78}\nPOOL-HOLD PROBE — bot_pool max_size=10 (hardcodat în connection.py)")
    print(f"sender={sender}  biz={biz.slug}\n{'=' * 78}\n")

    rows = []
    for i, text in enumerate(SCRIPT):
        event = {
            "channel_kind": "whatsapp",
            "channel_account_id": SIM_PROVIDER,
            "sender_external_id": sender,
            "provider_msg_id": f"sim.{uuid.uuid4().hex}",
            "content_type": "text",
            "body": text,
            "sender_name": "Client",
        }
        # NX-231: turul nu mai ține O conexiune — face N checkout-uri scurte. `held` devine SUMA
        # hold-urilor (cât timp cumulat a existat o conexiune în mâna turului), iar `db_active`
        # vine din proxy-ul de timing (`DB_QUERY_TIMING_ENABLED`). Diferența = idle-held: exact
        # cifra pe care fix-ul o duce spre zero.
        acc, token = op_metrics.push()
        t0 = time.perf_counter()
        try:
            result = await handle_turn(tenant_db(DEMO_BIZ), biz, channel_id, event, redis=None)
        finally:
            op_metrics.pop(token)
        wall = (time.perf_counter() - t0) * 1000.0
        held = acc.hold_ms
        db_ms = acc.query_ms
        idle = max(0.0, held - db_ms)
        n_queries = sum(r.queries for r in acc.by_op.values())
        rows.append((i, text, wall, held, db_ms, idle, acc.checkouts, result.reply_text))
        print(
            f"[tur {i}] {text!r}\n"
            f"   wall={_fmt(wall)}  held(total)={_fmt(held)}  db_active={_fmt(db_ms)} "
            f"({acc.checkouts} checkout-uri, {n_queries} query-uri)"
            f"  idle_held={_fmt(idle)}\n"
            f"   hold/wall={100 * held / wall:.0f}%  ← înainte de NX-231 era ~100% (o conexiune "
            f"ținută tot turul)\n"
        )

    # Analiza pe tururile de sales (index >=1) — calea scumpă (agent + tools). Turul 0 (salut)
    # e mai ieftin + include one-off-ul de creare contact/conv, nereprezentativ pt steady-state.
    sales = [r for r in rows if r[0] >= 1]
    avg_wall = sum(r[2] for r in sales) / len(sales)
    avg_held = sum(r[3] for r in sales) / len(sales)
    avg_db = sum(r[4] for r in sales) / len(sales)

    print(f"{'=' * 78}\nSTEADY-STATE (tururi de sales, medie pe {len(sales)}):")
    print(
        f"   wall={_fmt(avg_wall)}  held(total)={_fmt(avg_held)}  db_active={_fmt(avg_db)}  "
        f"hold/wall={100 * avg_held / avg_wall:.0f}%"
    )
    print(f"{'=' * 78}\nSATURAȚIE (legea lui Little: λ_max = pool / held_s):\n")
    pool_size = 10
    lam_before = pool_size / (avg_wall / 1000.0)  # un tur ținea conexiunea TOT wall-clock-ul
    lam_now = pool_size / (avg_held / 1000.0) if avg_held > 0 else float("inf")
    print(
        f"   ÎNAINTE (conn ținut tot turul): ~{lam_before:.1f} tururi/s "
        f"= ~{lam_before * 60:.0f}/min"
    )
    print(f"   ACUM    (conn-per-op):          ~{lam_now:.1f} tururi/s = ~{lam_now * 60:.0f}/min")
    print(f"   headroom câștigat: ×{avg_wall / avg_held:.1f}\n{'=' * 78}\n")
    if avg_db <= 0:
        print(
            "NOTĂ: `db_active` e 0 → rulează cu DB_QUERY_TIMING_ENABLED=1 pentru idle-held real.\n"
        )

    await close_pool()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
