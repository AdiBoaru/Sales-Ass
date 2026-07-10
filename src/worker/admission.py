"""Felia 0C (NX-161) — admission control: frâna EXPLICITĂ de concurență a tururilor.

De ce (docs/CONN-HOLD-ANALYSIS-2026.md): poolul DB (max 10) e azi frâna ACCIDENTALĂ a sistemului —
debouncer-ul spawnează un task/expeditor nemărginit, iar poolul e singurul care limitează câte
tururi rulează simultan. Conn-per-op (feliile următoare) ELIBEREAZĂ poolul din rolul ăsta → fără o
frână de înlocuire, un burst ar trimite zeci de tururi simultan la OpenAI (cost/memorie/rate-limit).

0C pune frâna corectă în locul corect: un **semafor global** de tururi, luat ÎNAINTE de secțiunea
LLM-heavy și **fără conn DB ținut** (invariant: NU în interiorul `tenant_conn`). Peste capacitate →
apelantul re-queue cu backoff (P6 — niciodată drop tăcut), NU blochează la infinit. Introdus DEVREME
(ca plasă) chiar dacă azi poolul încă bindează primul: setat > pool ca să nu reducă throughput-ul
curent, dar să existe ca frână pentru când feliile 1+ eliberează poolul.

Precedent în cod: `dispatcher_global_concurrency`/`dispatcher_tenant_concurrency` (NX-147). Fairness
COMPLET per-tenant (cozi, scheduling echitabil) = epic SEPARAT; aici doar frâna minimă + un plafon
per-business opțional.
"""

from __future__ import annotations

import asyncio
from time import perf_counter

from src.config import get_settings


class Admission:
    """Semafor global de tururi + plafon opțional per-business. asyncio single-thread → contoarele
    per-business sunt int-uri simple, fără lock. Dezactivat (`max_inflight<=0`) → no-op."""

    def __init__(self, max_inflight: int, max_per_business: int) -> None:
        self._sem = asyncio.Semaphore(max_inflight) if max_inflight > 0 else None
        self._max_per_business = max_per_business
        self._per_business: dict[str, int] = {}
        self._inflight = 0

    def _business_saturated(self, business_id: str) -> bool:
        return (
            self._max_per_business > 0
            and self._per_business.get(business_id, 0) >= self._max_per_business
        )

    async def acquire(self, business_id: str, timeout_s: float) -> float | None:
        """Ia un slot. Întoarce `wait_ms` (≥0) la SUCCES; `None` dacă e peste capacitate
        (per-business saturat SAU timeout pe semaforul global) → apelantul re-queue (P6).
        Dezactivat → 0.0. Cancellation-safe: `asyncio.timeout` (3.12) nu scapă slotul la timeout."""
        if self._sem is None:
            return 0.0
        if self._business_saturated(business_id):
            return None  # plafon per-business → defer fără să aștepți un slot global
        t0 = perf_counter()
        try:
            async with asyncio.timeout(timeout_s):
                await self._sem.acquire()
        except TimeoutError:
            return None  # global saturat peste timeout → defer
        # Re-check per-business DUPĂ acquire (TOCTOU, Codex #207): alt task pt ACELAȘI business a
        # putut trece de pre-check și lua sloturi cât așteptam pe semaforul global → cap depășit.
        # Fără `await` între re-check și increment → atomic în asyncio → cap respectat strict.
        if self._business_saturated(business_id):
            self._sem.release()  # dăm slotul global înapoi (nu-l ținem degeaba) → defer
            return None
        self._inflight += 1
        self._per_business[business_id] = self._per_business.get(business_id, 0) + 1
        return (perf_counter() - t0) * 1000.0

    def release(self, business_id: str) -> None:
        """Eliberează slotul la finalul turului. No-op dacă frâna e dezactivată."""
        if self._sem is None:
            return
        self._inflight = max(0, self._inflight - 1)
        remaining = self._per_business.get(business_id, 0) - 1
        if remaining <= 0:
            self._per_business.pop(business_id, None)
        else:
            self._per_business[business_id] = remaining
        self._sem.release()

    @property
    def inflight(self) -> int:
        """Câte tururi dețin ACUM un slot (gauge pt observabilitate/health)."""
        return self._inflight


_admission: Admission | None = None


def get_admission() -> Admission:
    """Singleton/proces (ca poolurile). Citește configul o dată; OFF → limite 0 (no-op)."""
    global _admission
    if _admission is None:
        s = get_settings()
        on = s.admission_enabled
        _admission = Admission(
            s.admission_max_inflight if on else 0,
            s.admission_max_per_business if on else 0,
        )
    return _admission


def reset_admission() -> None:
    """Test hook — resetează singleton-ul (ca `get_settings.cache_clear`)."""
    global _admission
    _admission = None
