"""NX-241 — comportament sub LOAD: fairness între tenanți, coadă care consumă deadline-ul,
paralelism plafonat și zero N+1 pe bucla de tool-uri.

Fără DB și fără provider real: load-ul care ne interesează aici e cel de CONTENȚIE (cine așteaptă
pe cine și cât), nu debitul mașinii de CI. De aceea totul e pe ceas fals și pe fake-uri
deterministe — un test de latență care măsoară CI-ul e zgomot, nu semnal.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from src.agent import tool_budget
from src.agent import tool_executor as te
from src.agent.tool_budget import ToolGate
from src.agent.tool_executor import ToolRun
from src.runtime import deadline, turn_budget
from src.runtime.deadline import TurnDeadline
from src.runtime.turn_budget import BudgetLedger, TurnClass, build_manifest
from src.worker.admission import Admission


def _result(**kw):
    """`ToolResult` duck-typed — câmpurile pe care le citește `ToolRun` (ca în test_tools)."""
    base = dict(
        products=[],
        ok=True,
        links=[],
        prices=set(),
        relevance=None,
        state_patch=None,
        llm_view="view",
        error=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _ctx():
    events: list[tuple] = []
    return SimpleNamespace(
        emit=lambda event_type, **kw: events.append((event_type, kw)),
        state_patch={},
        business=SimpleNamespace(id="biz-1"),
        _events=events,
    )


def _ledger(turn_class=TurnClass.COMPLEX, *, enforced=True) -> BudgetLedger:
    manifest = build_manifest(hard_cap_ms=15_000, cost_ceiling_usd=0.01)
    return BudgetLedger(manifest[turn_class], enforced=enforced)


# ── admission: coada consumă ACELAȘI buget ────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_queue_wait_beyond_the_deadline_is_rejected_without_starting_work():
    """Un tur al cărui deadline s-a scurs în coadă NU mai intră: a-i da un slot ar însemna să
    ocupăm capacitatea pentru un răspuns care oricum ajunge prea târziu."""
    adm = Admission(max_inflight=1, max_per_business=0)
    d = TurnDeadline(total_ms=500, terminal_reserve_ms=100, clock=lambda: 0.0)
    d.elapsed_before_ms = 5_000
    token = deadline.push(d)
    try:
        slot = await adm.acquire("biz-a", timeout_s=5.0)
    finally:
        deadline.pop(token)
    assert not slot.admitted and slot.reason == "deadline_exceeded"
    assert adm.stats.rejected["deadline_exceeded"] == 1


@pytest.mark.asyncio
async def test_admission_wait_is_clamped_to_the_remaining_budget():
    """Așteptarea în coadă nu are voie să depășească bugetul rămas: altfel `admission_timeout` ar
    consuma exact timpul în care ar fi trebuit să răspundem."""
    adm = Admission(max_inflight=1, max_per_business=0)
    held = await adm.acquire("biz-a", timeout_s=1.0)
    assert held.admitted
    d = TurnDeadline(total_ms=300, terminal_reserve_ms=50, clock=asyncio.get_running_loop().time)
    token = deadline.push(d)
    try:
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        slot = await adm.acquire("biz-a", timeout_s=30.0)  # 30s ar fi absurd pe un tur de 300ms
        waited = loop.time() - t0
    finally:
        deadline.pop(token)
        await adm.release(held)
    assert not slot.admitted and slot.reason == "queue_timeout"
    assert waited < 1.0


@pytest.mark.asyncio
async def test_tenant_burst_cannot_starve_another_tenant():
    """Fairness: A în burst nu poate lua decât plafonul lui, deci B găsește loc chiar dacă vine
    ultimul. Ăsta e testul care contează sub load, nu debitul total."""
    adm = Admission(max_inflight=6, max_per_business=2)
    a_slots = [await adm.acquire("biz-a", 0.05) for _ in range(5)]
    admitted_a = [s for s in a_slots if s.admitted]
    b = await adm.acquire("biz-b", 0.05)
    try:
        assert len(admitted_a) == 2  # plafonul de tenant
        assert b.admitted  # B nu a fost înfometat de burst-ul lui A
        assert adm.stats.rejected["tenant_cap"] == 3
    finally:
        for slot in [*admitted_a, b]:
            await adm.release(slot)


@pytest.mark.asyncio
async def test_capacity_smaller_than_the_burst_defers_instead_of_dropping():
    adm = Admission(max_inflight=2, max_per_business=0)
    slots = await asyncio.gather(*(adm.acquire("biz-a", 0.01) for _ in range(6)))
    admitted = [s for s in slots if s.admitted]
    try:
        assert len(admitted) == 2
        # Restul primesc un motiv EXPLICIT (re-queue, P6), nu dispar tăcut.
        assert all(s.reason == "queue_timeout" for s in slots if not s.admitted)
    finally:
        for slot in admitted:
            await adm.release(slot)


# ── paralelismul de citiri sub buget ───────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_read_parallelism_never_exceeds_the_class_cap_under_a_storm():
    ledger = _ledger()
    gate = ToolGate(max_parallel_reads=ledger.budget.max_parallel_reads)
    active = peak = 0

    async def read():
        nonlocal active, peak
        if not tool_budget.admit("search_products", ledger=ledger):
            return "refuzat"
        async with gate.hold("search_products"):
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.005)
            active -= 1
        return "ok"

    results = await asyncio.gather(*(read() for _ in range(20)))
    assert peak <= ledger.budget.max_parallel_reads
    assert results.count("ok") == ledger.budget.max_tool_calls  # restul, refuzate typed
    assert results.count("refuzat") == 20 - ledger.budget.max_tool_calls


@pytest.mark.asyncio
async def test_mutations_stay_serial_while_reads_run_in_parallel():
    gate = ToolGate(max_parallel_reads=3)
    concurrent_mutations = 0
    overlaps = 0

    async def mutate():
        nonlocal concurrent_mutations, overlaps
        async with gate.hold("cart_add"):
            concurrent_mutations += 1
            if concurrent_mutations > 1:
                overlaps += 1
            await asyncio.sleep(0.002)
            concurrent_mutations -= 1

    async def read():
        async with gate.hold("search_products"):
            await asyncio.sleep(0.002)

    await asyncio.gather(*(mutate() for _ in range(4)), *(read() for _ in range(8)))
    assert overlaps == 0


# ── zero N+1: numărul de apeluri nu crește cu numărul de produse ──────────────────────────
@pytest.mark.asyncio
@pytest.mark.parametrize("n_products", [1, 6, 10])
async def test_tool_calls_do_not_grow_with_the_result_size(monkeypatch, n_products):
    """Contorul de apeluri e per TUR, nu per produs: un rezultat de 10 produse costă exact la fel
    ca unul de 1. Dacă un card viitor introduce hidratare per produs, testul ăsta o prinde."""
    calls: list[str] = []

    async def fake_run_tool(ctx, deps, name, args):  # noqa: ARG001
        calls.append(name)
        return _result(products=[{"id": f"p{i}"} for i in range(n_products)])

    monkeypatch.setattr(te, "run_tool", fake_run_tool)
    ledger = _ledger()
    token = turn_budget.push(ledger)
    try:
        run = ToolRun(_ctx(), type("D", (), {"db": None})())
        await run.execute("search_products", {})
    finally:
        turn_budget.pop(token)
    assert len(calls) == 1
    assert ledger.spent["tool_calls"] == 1
    assert len(run.retrieved) == n_products


@pytest.mark.asyncio
async def test_parallel_reads_accumulate_in_call_order_not_completion_order(monkeypatch):
    """Paralelismul taie latența, dar NU are voie să schimbe ce vede clientul.

    `retrieved` e o listă ordonată după relevanță, nu o mulțime: dacă a doua căutare răspunde
    prima, produsele ei nu au voie să treacă în fața celei dintâi — altfel aceleași două tool-uri
    ar produce carduri în ordini diferite de la o rulare la alta."""

    async def fake_run_tool(ctx, deps, name, args):  # noqa: ARG001
        # Al doilea apel („rapid") se termină ÎNAINTEA primului („lent").
        delay = 0.03 if args.get("q") == "lent" else 0.0
        await asyncio.sleep(delay)
        return _result(products=[{"id": args["q"]}])

    monkeypatch.setattr(te, "run_tool", fake_run_tool)
    monkeypatch.setattr(
        te, "get_settings", lambda: SimpleNamespace(turn_parallel_reads_enabled=True)
    )

    def per_op(operation=None):  # provider conn-per-op → paralelismul e permis
        raise AssertionError("nu se apelează")

    per_op.shared_connection = False
    ledger = _ledger()
    token = turn_budget.push(ledger)
    try:
        run = ToolRun(_ctx(), SimpleNamespace(db=per_op))
        assert run._max_parallel_reads() > 1
        await asyncio.gather(
            run.execute("search_products", {"q": "lent"}),
            run.execute("search_products", {"q": "rapid"}),
        )
    finally:
        turn_budget.pop(token)
    assert [p["id"] for p in run.retrieved] == ["lent", "rapid"]


@pytest.mark.asyncio
async def test_a_refused_call_does_not_block_the_ones_after_it(monkeypatch):
    """Bilețelul de ordonare se eliberează și pe calea de REFUZ — altfel un tool respins de buget
    ar bloca definitiv tot ce vine după el (deadlock, nu degradare)."""

    async def fake_run_tool(ctx, deps, name, args):  # noqa: ARG001
        return _result(products=[{"id": args["q"]}])

    monkeypatch.setattr(te, "run_tool", fake_run_tool)
    monkeypatch.setattr(
        te, "get_settings", lambda: SimpleNamespace(turn_parallel_reads_enabled=True)
    )

    def per_op(operation=None):
        raise AssertionError("nu se apelează")

    per_op.shared_connection = False
    ledger = _ledger(TurnClass.EXACT)  # 2 tool calls, deci al treilea e refuzat
    token = turn_budget.push(ledger)
    try:
        run = ToolRun(_ctx(), SimpleNamespace(db=per_op))
        results = await asyncio.wait_for(
            asyncio.gather(
                *(run.execute("search_products", {"q": f"c{i}"}) for i in range(4)),
            ),
            timeout=2.0,  # dacă bilețelele s-ar scurge, aici am atârna la infinit
        )
    finally:
        turn_budget.pop(token)
    assert sum(1 for r in results if r == "view") == 2
    assert [p["id"] for p in run.retrieved] == ["c0", "c1"]


@pytest.mark.asyncio
async def test_a_storm_of_tool_calls_cannot_outrun_the_budget(monkeypatch):
    """Contorul e rezervat ÎNAINTE de a porni apelul, fără `await` între verificare și increment →
    douăzeci de apeluri lansate deodată nu pot trece toate."""
    started = 0

    async def fake_run_tool(ctx, deps, name, args):  # noqa: ARG001
        nonlocal started
        started += 1
        await asyncio.sleep(0.001)
        return _result()

    monkeypatch.setattr(te, "run_tool", fake_run_tool)
    ledger = _ledger()
    token = turn_budget.push(ledger)
    try:
        run = ToolRun(_ctx(), type("D", (), {"db": None})())
        await asyncio.gather(*(run.execute("search_products", {}) for _ in range(20)))
    finally:
        turn_budget.pop(token)
    assert started == ledger.budget.max_tool_calls
