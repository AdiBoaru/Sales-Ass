"""Felia 0A (NX-161) — instrumentarea pool-ului `bot_runtime`: acquire-wait + held + ocupare.

Probe-ul (`scripts/sim/pool_probe.py`) a măsurat HOLD pe dev (~79% idle-held). ĂSTA e semnalul de
WAIT în prod care declanșează fix-ul conn-per-op: cât BLOCHEZI pe `pool.acquire()` sub contenție.
Vezi `docs/CONN-HOLD-ANALYSIS-2026.md` §Faza 0A. Trigger de îngrijorare: p95 acquire-wait >100-250ms
în burst · pool in-use lipit de max pe tururi LLM-heavy.

Pur observabilitate (P10 — runner/processor măsoară, stagiile nu știu). ZERO PII (P12):
`business_id` e UUID de tenant. Oglindește pattern-ul contextvar din `src/agent/usage.py`:
acquire-wait-ul e măsurat în `tenant_conn` (fără `ctx`) și cărat prin ContextVar la `handle_turn`,
care emite evenimentul corelat pe tur.
"""

from __future__ import annotations

import contextvars
from typing import Any

# Acquire-wait-ul checkout-urilor turului curent (ms). Setat de tenant_conn, citit+resetat de
# handle_turn. ContextVar → izolat per task async (ca `usage`): două tururi concurente nu-și
# amestecă valorile.
#
# NX-231: un tur făcea UN checkout, deci „ultimul câștigă" era exact. Cu conn-per-op face N, iar
# ultimul e de obicei cel mai ieftin (commit-ul) — ar fi ascuns exact vârful pe care îl căutăm.
# Acumulăm: (n, total, max). `db_pool_wait_ms` raportat = MAXIMUL (cea mai lungă așteptare de
# conexiune din tur = semnalul de contenție), plus totalul pentru bugetul de latență.
_acquire: contextvars.ContextVar[tuple[int, float, float] | None] = contextvars.ContextVar(
    "pool_acquire_wait", default=None
)

# Gauge de tururi „în zbor" (checkout-uri bot_pool active). asyncio e single-thread → int simplu,
# fără lock. NOTĂ: cu conn-per-op (felii ulterioare) un tur va face N checkout-uri → gauge-ul devine
# „checkout-uri concurente", iar `turn_inflight` real se mută pe admission (Faza 0C).
_inflight: int = 0


def record_acquire_wait(ms: float) -> None:
    """`tenant_conn` raportează durata `pool.acquire()` (contenția). Acumulează n/total/max."""
    prev = _acquire.get()
    if prev is None:
        _acquire.set((1, ms, ms))
    else:
        n, total, mx = prev
        _acquire.set((n + 1, total + ms, max(mx, ms)))


def take_acquire_stats() -> tuple[int, float, float] | None:
    """`handle_turn` citește + RESETează statistica de acquire a turului: `(n, total_ms, max_ms)`.
    None = niciun checkout instrumentat (sau deja consumat) — un al doilea emit în același tur nu
    re-raportează valori stale."""
    v = _acquire.get()
    if v is not None:
        _acquire.set(None)
    return v


def take_acquire_wait() -> float | None:
    """Compat: cea mai lungă așteptare de conexiune din tur (None = neinstrumentat/consumat).
    Cu conn-per-op maximul e cifra utilă — media ascunde exact vârful care produce timeouts."""
    stats = take_acquire_stats()
    return None if stats is None else stats[2]


def inc_inflight() -> int:
    global _inflight
    _inflight += 1
    return _inflight


def dec_inflight() -> int:
    global _inflight
    _inflight = max(0, _inflight - 1)
    return _inflight


def get_inflight() -> int:
    return _inflight


def pool_snapshot(pool: Any) -> dict[str, int]:
    """Ocuparea pool-ului (size/idle/in_use/max) + inflight — None-safe (pool neinițializat → {}).
    asyncpg expune `get_size`/`get_idle_size`/`get_max_size`. Best-effort: o eroare de introspecție
    NU rupe turul (observabilitate)."""
    if pool is None:
        return {"pool_inflight": _inflight}
    try:
        size = pool.get_size()
        idle = pool.get_idle_size()
        return {
            "pool_size": size,
            "pool_idle": idle,
            "pool_in_use": size - idle,
            "pool_max": pool.get_max_size(),
            "pool_inflight": _inflight,
            # NX-231: numele din card pentru dashboard (`db_pool_active`). Aceeași cifră ca
            # `pool_in_use`, expusă sub eticheta pe care o cere raportul before/after.
            "db_pool_active": size - idle,
        }
    except Exception:  # noqa: BLE001 — stats best-effort, nu blochează turul
        return {"pool_inflight": _inflight}
