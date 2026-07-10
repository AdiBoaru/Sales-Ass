"""NX-161 Felia 0C — frâna de admission: semafor global + plafon per-business + defer (P6).

Testăm clasa `Admission` direct (fără singleton): acquire/release, saturație globală → timeout →
None, plafon per-business → None imediat (fără wait), dezactivat → no-op, curățarea contorului.
"""

import asyncio
import time

from src.worker.admission import Admission


async def test_acquire_release_roundtrip():
    a = Admission(max_inflight=2, max_per_business=0)
    wait = await a.acquire("b1", timeout_s=1.0)
    assert wait is not None and wait >= 0.0
    assert a.inflight == 1
    a.release("b1")
    assert a.inflight == 0


async def test_global_saturation_defers_after_timeout():
    a = Admission(max_inflight=1, max_per_business=0)
    assert await a.acquire("b1", 1.0) is not None  # slotul 1
    t0 = time.perf_counter()
    assert await a.acquire("b1", 0.05) is None  # peste limită → timeout scurt → defer
    assert (time.perf_counter() - t0) >= 0.04  # chiar a așteptat ~timeout-ul
    assert a.inflight == 1  # deferul NU a luat slot
    a.release("b1")
    assert await a.acquire("b1", 1.0) is not None  # slot liber acum


async def test_per_business_cap_defers_immediately():
    a = Admission(max_inflight=10, max_per_business=1)
    assert await a.acquire("b1", 1.0) is not None
    t0 = time.perf_counter()
    assert (
        await a.acquire("b1", 5.0) is None
    )  # b1 saturat → None IMEDIAT (nu așteaptă un slot global)
    assert (time.perf_counter() - t0) < 0.5  # a returnat imediat, n-a blocat 5s
    assert await a.acquire("b2", 1.0) is not None  # alt business are slot
    assert a.inflight == 2


async def test_disabled_is_noop():
    a = Admission(max_inflight=0, max_per_business=0)
    assert await a.acquire("b1", 1.0) == 0.0  # fără frână → mereu admis
    assert a.inflight == 0  # gauge-ul nu contorizează când frâna e off
    a.release("b1")  # no-op, fără crash
    assert a.inflight == 0


async def test_per_business_counter_cleaned_on_release():
    a = Admission(max_inflight=10, max_per_business=3)
    await a.acquire("b1", 1.0)
    await a.acquire("b1", 1.0)
    assert a._per_business["b1"] == 2
    a.release("b1")
    a.release("b1")
    assert "b1" not in a._per_business  # curățat la 0 → dict-ul nu crește nemărginit


async def test_per_business_cap_holds_under_global_wait():
    # TOCTOU (Codex #207): două task-uri pt ACELAȘI business trec de pre-check (business la 0) și
    # AȘTEAPTĂ pe semaforul global; la eliberare, re-check-ul de DUPĂ acquire trebuie să respecte
    # cap-ul, altfel ambele incrementează → depășesc max_per_business.
    a = Admission(max_inflight=2, max_per_business=1)
    assert await a.acquire("b2", 1.0) is not None  # umple slotul global 1 (alt business)
    assert await a.acquire("b3", 1.0) is not None  # umple slotul global 2 → global FULL
    t1 = asyncio.create_task(a.acquire("b1", 2.0))
    t2 = asyncio.create_task(a.acquire("b1", 2.0))
    await asyncio.sleep(0.02)  # ambele ajung la await sem.acquire (pre-check trecut, b1=0)
    a.release("b2")
    a.release("b3")  # eliberează 2 sloturi globale → ambele task-uri b1 se trezesc
    r1, r2 = await asyncio.gather(t1, t2)
    # cap-ul respectat: exact UNUL primește slot, celălalt defer (re-check după acquire)
    assert sorted([r1 is None, r2 is None]) == [False, True]
    assert a._per_business.get("b1", 0) == 1  # niciodată 2
    assert a.inflight == 1


async def test_singleton_reset(monkeypatch):
    from src.worker import admission as adm

    adm.reset_admission()
    first = adm.get_admission()
    assert adm.get_admission() is first  # singleton
    adm.reset_admission()
    assert adm.get_admission() is not first  # reset → instanță nouă
