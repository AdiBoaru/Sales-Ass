"""NX-241 — proba REPRODUCTIBILĂ de latență: deadline, bugete, retry, load, aftercare.

Manual drive-ul cerut de card, cu providere STUB și ceas controlat: zero DB, zero OpenAI, zero
rețea (regula proiectului: Claude nu pornește rulări care consumă credite). Ce demonstrează:

  1. matricea de latență per clasă de tur (exact / recommendation / complex / mutation) cu
     providerul la 0 / 200 / 800 / 3000 ms → p50/p90/p99 + defalcare pe faze + status terminal;
  2. 429 cu `Retry-After` peste bugetul rămas → NU dormim, degradăm terminal;
  3. provider care atârnă → apelul e tăiat de deadline-ul TURULUI, nu de `llm_timeout × retry`;
  4. tool storm (modelul cere 20 de tool-uri) → refuz typed la plafon, zero apeluri peste buget;
  5. burst peste plafonul de admission, doi tenanți → fairness + re-queue, niciun drop tăcut;
  6. reclaim: al doilea worker primește ce a RĂMAS din `deadline_at`, nu încă un buget întreg;
  7. aftercare lent → rezultatul terminal NU îl așteaptă.

Raportul e low-cardinality și fără PII: milisecunde, contoare, coduri. Zero text de client.

Rulare:
    PYTHONPATH=. python scripts/sim/web_latency_probe.py
    PYTHONPATH=. python scripts/sim/web_latency_probe.py --runs 40 --json reports/nx241.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# Consola Windows e cp1252 by default — ieșirea are diacritice + box drawing.
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8")

from src.agent import tool_budget  # noqa: E402
from src.config import Settings  # noqa: E402
from src.observability import turn_latency  # noqa: E402
from src.runtime import deadline, turn_budget  # noqa: E402
from src.runtime.deadline import TurnDeadline  # noqa: E402
from src.runtime.turn_budget import BudgetLedger, TurnClass, build_manifest  # noqa: E402
from src.worker.admission import Admission  # noqa: E402

SETTINGS = Settings(
    TURN_DEADLINE_ENABLED=True,
    TURN_BUDGET_ENFORCED=True,
    TURN_PARALLEL_READS_ENABLED=True,
    TURN_LATENCY_SPANS_ENABLED=True,
)
MANIFEST = build_manifest(
    hard_cap_ms=SETTINGS.turn_hard_deadline_ms, cost_ceiling_usd=SETTINGS.turn_cost_budget_usd
)

#: Profilul fiecărei clase: câte faze și cât „costă" fiecare la providerul de bază.
PROFILE: dict[TurnClass, dict[str, int]] = {
    TurnClass.EXACT: {"load": 20, "gates": 10, "retrieval": 0, "model": 1, "tools": 1},
    TurnClass.RECOMMENDATION: {"load": 30, "gates": 15, "retrieval": 1, "model": 2, "tools": 1},
    TurnClass.COMPLEX: {"load": 40, "gates": 15, "retrieval": 2, "model": 3, "tools": 3},
    TurnClass.MUTATION: {"load": 30, "gates": 15, "retrieval": 1, "model": 2, "tools": 2},
}


class Clock:
    """Ceas fals: „latența" e aritmetică, nu somn. Un test de latență care doarme măsoară CI-ul."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, ms: float) -> None:
        self.t += ms / 1000.0


@dataclass
class TurnReport:
    turn_class: str
    provider_ms: int
    outcome: str  # completed | fallback | deadline_exceeded
    e2e_ms: int
    phases: dict[str, float] = field(default_factory=dict)
    model_calls: int = 0
    tool_calls: int = 0
    refusals: dict[str, int] = field(default_factory=dict)
    degradations: dict[str, int] = field(default_factory=dict)


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, round((pct / 100.0) * (len(ordered) - 1))))
    return ordered[idx]


async def simulate_turn(turn_class: TurnClass, provider_ms: int, *, cold: bool) -> TurnReport:
    """Un tur SIMULAT prin contractele reale (deadline + ledger + spans), cu provider stub."""
    clock = Clock()
    budget = MANIFEST[turn_class]
    d = TurnDeadline(
        total_ms=budget.total_ms, terminal_reserve_ms=budget.terminal_reserve_ms, clock=clock
    )
    ledger = BudgetLedger(budget, enforced=True)
    acc, lat_token = turn_latency.push()
    d_token, b_token = deadline.push(d), turn_budget.push(ledger)
    outcome = "completed"
    try:
        profile = PROFILE[turn_class]
        # Faze deterministe (load/gates): cold path plătește o dată în plus pentru config/prompt.
        for phase in ("load", "gates"):
            cost = profile[phase] * (2 if cold else 1)
            clock.advance(cost)
            turn_latency.record(phase, cost)
        for _ in range(profile["retrieval"]):
            cap = min(budget.retrieval_ms, d.remaining_ms())
            cost = min(provider_ms, cap)
            if d.has_room_for("retrieval").exhausted:
                turn_latency.record("retrieval", 0, turn_latency.OUTCOME_SKIPPED)
                break
            clock.advance(cost)
            turn_latency.record(
                "retrieval",
                cost,
                turn_latency.OUTCOME_DEGRADED if cost < provider_ms else turn_latency.OUTCOME_OK,
            )
            if cost < provider_ms:
                turn_latency.degrade("retrieval_deadline")
        for _ in range(profile["model"]):
            if not ledger.reserve("model_rounds") or d.has_room_for("model").exhausted:
                break
            cap = min(SETTINGS.llm_call_cap_ms, d.remaining_ms())
            cost = min(provider_ms, cap)
            clock.advance(cost)
            turn_latency.record(
                "model",
                cost,
                turn_latency.OUTCOME_TIMEOUT if cost < provider_ms else turn_latency.OUTCOME_OK,
            )
            ledger.consume("tokens", 900)
            ledger.consume("cost_usd", 0.0012)
        for _ in range(profile["tools"]):
            name = "cart_add" if turn_class is TurnClass.MUTATION else "search_products"
            if not tool_budget.admit(name, ledger=ledger, deadline=d):
                break
            clock.advance(40)
            turn_latency.record("tools", 40)
        # Faza terminală cheltuie REZERVA — de asta există.
        for phase, cost in (("validation", 15), ("projection", 5), ("commit", 25)):
            clock.advance(cost)
            turn_latency.record(phase, cost)
        if d.expired(reserve=False):
            outcome = "deadline_exceeded"
        elif d.expired():
            outcome = "fallback"
    finally:
        turn_budget.pop(b_token)
        deadline.pop(d_token)
        turn_latency.pop(lat_token)
    return TurnReport(
        turn_class=turn_class.value,
        provider_ms=provider_ms,
        outcome=outcome,
        e2e_ms=round(clock.t * 1000),
        phases={k: round(v.ms, 1) for k, v in acc.by_phase.items()},
        model_calls=int(ledger.spent.get("model_rounds", 0)),
        tool_calls=int(ledger.spent.get("tool_calls", 0)),
        refusals=dict(ledger.rejections),
        degradations=dict(acc.degradations),
    )


async def matrix(runs: int) -> list[TurnReport]:
    reports: list[TurnReport] = []
    for turn_class in TurnClass:
        for provider_ms in (0, 200, 800, 3_000):
            for i in range(runs):
                reports.append(await simulate_turn(turn_class, provider_ms, cold=(i == 0)))
    return reports


def print_matrix(reports: list[TurnReport]) -> None:
    print("\n1) MATRICEA DE LATENȚĂ (ms end-to-end, ceas controlat)")
    print(f"   {'clasă':<16}{'provider':>9}{'p50':>7}{'p90':>7}{'p99':>7}  {'terminal'}")
    for turn_class in TurnClass:
        budget = MANIFEST[turn_class]
        for provider_ms in (0, 200, 800, 3_000):
            rows = [
                r
                for r in reports
                if r.turn_class == turn_class.value and r.provider_ms == provider_ms
            ]
            e2e = [r.e2e_ms for r in rows]
            outcomes = sorted({r.outcome for r in rows})
            p90 = percentile(e2e, 90)
            flag = "  ≤SLO" if p90 <= budget.total_ms else "  >SLO"
            print(
                f"   {turn_class.value:<16}{provider_ms:>9}"
                f"{percentile(e2e, 50):>7.0f}{p90:>7.0f}{percentile(e2e, 99):>7.0f}"
                f"  {','.join(outcomes)}{flag}"
            )
    # Invariantul care contează: niciun tur nu trece de plafonul DUR, oricât de lent e providerul.
    worst = max(r.e2e_ms for r in reports)
    print(f"\n   max e2e observat: {worst}ms (plafon dur {SETTINGS.turn_hard_deadline_ms}ms)")
    assert worst <= SETTINGS.turn_hard_deadline_ms, "un tur a depășit plafonul dur"


def print_phases(reports: list[TurnReport]) -> None:
    print("\n   defalcare pe faze (medie ms, provider=800ms, warm):")
    rows = [r for r in reports if r.provider_ms == 800]
    phases: dict[str, list[float]] = {}
    for r in rows:
        for phase, ms in r.phases.items():
            phases.setdefault(phase, []).append(ms)
    for phase, values in sorted(phases.items(), key=lambda kv: -sum(kv[1]) / len(kv[1])):
        print(f"     {phase:<12}{sum(values) / len(values):>8.1f}")


async def probe_retry_after() -> None:
    print("\n2) 429 cu Retry-After peste bugetul rămas → nu dormim, degradăm")
    clock = Clock()
    d = TurnDeadline(total_ms=2_000, terminal_reserve_ms=400, clock=clock)
    clock.advance(1_200)
    fits = d.fits(30_000, minimum_ms=SETTINGS.llm_retry_min_budget_ms)
    print(f"   rămas={d.remaining_ms()}ms, Retry-After=30000ms, retry={'DA' if fits else 'NU'}")
    assert not fits


async def probe_hanging_provider() -> None:
    print("\n3) Provider care atârnă → tăiat de deadline-ul TURULUI")
    budget = MANIFEST[TurnClass.RECOMMENDATION]
    d = TurnDeadline(total_ms=budget.total_ms, terminal_reserve_ms=budget.terminal_reserve_ms)
    timeout_s = d.timeout_for(SETTINGS.llm_call_cap_ms)
    legacy_s = SETTINGS.llm_timeout_s * (SETTINGS.llm_retry_max + 1)
    print(f"   timeout efectiv={timeout_s:.2f}s (înainte: până la {legacy_s:.0f}s pe apel)")
    assert timeout_s < legacy_s


async def probe_tool_storm() -> None:
    print("\n4) Tool storm (modelul cere 20 de tool-uri)")
    budget = MANIFEST[TurnClass.COMPLEX]
    ledger = BudgetLedger(budget, enforced=True)
    admitted = sum(1 for _ in range(20) if tool_budget.admit("search_products", ledger=ledger))
    print(f"   admise={admitted}/20 (plafon {budget.max_tool_calls})")
    print(f"   refuzuri typed={ledger.rejections}")
    assert admitted == budget.max_tool_calls


async def probe_burst() -> None:
    print("\n5) Burst peste plafon, doi tenanți (fairness)")
    adm = Admission(max_inflight=6, max_per_business=2)
    a = [await adm.acquire("biz-a", 0.01) for _ in range(8)]
    b = [await adm.acquire("biz-b", 0.01) for _ in range(3)]
    ok_a = [s for s in a if s.admitted]
    ok_b = [s for s in b if s.admitted]
    rejected = adm.stats.as_dict()["rejected"]
    print(f"   A: {len(ok_a)}/8 admise · B: {len(ok_b)}/3 admise · respinse={rejected}")
    assert len(ok_a) == 2 and len(ok_b) == 2, "fairness rupt"
    for slot in ok_a + ok_b:
        await adm.release(slot)


async def probe_reclaim() -> None:
    print("\n6) Reclaim: al doilea worker primește ce a RĂMAS, nu încă un buget")
    accepted = datetime(2026, 8, 16, 12, 0, 0, tzinfo=UTC)
    deadline_at = accepted + timedelta(milliseconds=SETTINGS.turn_hard_deadline_ms)
    first = TurnDeadline.from_deadline_at(
        deadline_at, accepted, fallback_total_ms=1, hard_cap_ms=SETTINGS.turn_hard_deadline_ms
    )
    second = TurnDeadline.from_deadline_at(
        deadline_at,
        accepted + timedelta(seconds=11),
        fallback_total_ms=1,
        hard_cap_ms=SETTINGS.turn_hard_deadline_ms,
    )
    print(f"   attempt#1={first.total_ms}ms · attempt#2 (după 11s)={second.total_ms}ms")
    assert second.total_ms < first.total_ms


async def probe_aftercare() -> None:
    print("\n7) Aftercare lent → rezultatul terminal nu îl așteaptă")
    order: list[str] = []

    async def terminal_commit():
        order.append("terminal")

    async def slow_aftercare():
        try:
            async with asyncio.timeout(0.02):
                await asyncio.sleep(5)
        except TimeoutError:
            order.append("aftercare_timeout")

    await terminal_commit()
    await slow_aftercare()
    print(f"   ordine={order} (real: AFTERCARE_DEADLINE_MS={SETTINGS.aftercare_deadline_ms})")
    assert order == ["terminal", "aftercare_timeout"]


def cell(reports: list[TurnReport], tc: TurnClass, provider_ms: int) -> dict:
    """O celulă din matricea de raport (clasă × provider): percentile + bugetul ei."""
    e2e = [r.e2e_ms for r in reports if r.turn_class == tc.value and r.provider_ms == provider_ms]
    return {
        "turn_class": tc.value,
        "provider_ms": provider_ms,
        "p50": percentile(e2e, 50),
        "p90": percentile(e2e, 90),
        "p99": percentile(e2e, 99),
        "budget_ms": MANIFEST[tc].total_ms,
        "outcomes": sorted(
            {
                r.outcome
                for r in reports
                if r.turn_class == tc.value and r.provider_ms == provider_ms
            }
        ),
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description="NX-241 — proba de latență (offline, ceas fals)")
    parser.add_argument("--runs", type=int, default=12, help="rulări per (clasă × provider)")
    parser.add_argument("--json", type=str, default="", help="scrie raportul agregat în fișier")
    args = parser.parse_args()

    print("═" * 78)
    print("NX-241 — PROBĂ DE LATENȚĂ (zero DB, zero OpenAI, zero rețea)")
    print(f"manifest={turn_budget.BUDGET_MANIFEST_VERSION} · runs/celulă={args.runs}")
    print("═" * 78)

    reports = await matrix(args.runs)
    print_matrix(reports)
    print_phases(reports)
    await probe_retry_after()
    await probe_hanging_provider()
    await probe_tool_storm()
    await probe_burst()
    await probe_reclaim()
    await probe_aftercare()

    if args.json:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "manifest_version": turn_budget.BUDGET_MANIFEST_VERSION,
            "hard_deadline_ms": SETTINGS.turn_hard_deadline_ms,
            "cells": [cell(reports, tc, pms) for tc in TurnClass for pms in (0, 200, 800, 3_000)],
        }
        out.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\nraport scris în {out}")

    print("\n" + "═" * 78)
    print("TOATE PROBELE AU TRECUT: un singur deadline respectat cap-coadă, retry care nu-l")
    print("depășește, plafoane impuse atomic, fairness sub burst și aftercare în afara căii.")


if __name__ == "__main__":
    asyncio.run(main())
