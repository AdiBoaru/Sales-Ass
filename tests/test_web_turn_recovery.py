"""NX-233 — sweeper-ul de recovery (unit, fără DB/Redis reale).

Garanțiile verificate:
  • `accepted` peste deadline → `cancelled` cu error-view (fără epoch: nu există lease);
  • `running` cu lease expirat peste deadline → claim (epoch+1) + `failed` (zombie deposedat);
  • `running` expirat cu attempts la plafon → `attempts_exhausted`;
  • turn sănătos neconsumat → RE-WAKE (nu terminal, nu claim) — wake loss e recuperat din DB;
  • advisory lock ocupat → trecerea NU scanează (un singur sweeper mătură per flotă);
  • CAS pierdut (alt drum a terminat între scan și scriere) → skip, nu forțare;
  • un turn care crapă nu oprește restul trecerii (bounded, continuă).
"""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

from src.config import get_settings
from src.db.queries.web_turns import ClaimableTurn, ClaimResult
from src.web import turn_recovery as tr


def _now():
    """Dinamic, nu la import: suita COMPLETA ruleaza testul la minute dupa colectare."""
    return datetime.now(UTC)


def _turn(status="accepted", *, attempt=0, deadline=None, business_id="b1") -> ClaimableTurn:
    return ClaimableTurn(
        business_id=business_id,
        turn_id=str(uuid4()),
        status=status,
        attempt=attempt,
        deadline_at=deadline,
        accepted_at=_now() - timedelta(seconds=30),
    )


class _AdminConn:
    def __init__(self, lock_free=True):
        self.lock_free = lock_free
        self.unlocked = False

    async def fetchval(self, sql, *a):
        assert "pg_try_advisory_lock" in sql
        return self.lock_free

    async def execute(self, sql, *a):
        if "pg_advisory_unlock" in sql:
            self.unlocked = True
        return "SELECT 1"


class _Wired:
    def __init__(self, monkeypatch, turns, *, lock_free=True, claim=ClaimResult(2, 2, True)):
        self.conn = _AdminConn(lock_free)
        self.scanned = 0
        self.cancels: list[dict] = []
        self.claims: list[str] = []
        self.fails: list[dict] = []
        self.woken: list[str] = []

        @asynccontextmanager
        async def fake_admin(pool):
            yield self.conn

        async def fake_pool():
            return None

        async def fake_scan(conn, *, limit):
            self.scanned += 1
            return turns

        async def fake_cancel(db, business_id, turn_id, *, code, language, lease_epoch=None):
            self.cancels.append({"turn_id": turn_id, "code": code, "language": language})
            return True

        async def fake_claim(db, business_id, turn_id, *, owner, lease_ttl_s):
            self.claims.append(turn_id)
            return claim

        async def fake_fail(db, business_id, turn_id, *, lease_epoch, code, language):
            self.fails.append({"turn_id": turn_id, "code": code, "lease_epoch": lease_epoch})
            return True

        async def fake_wake(redis, business_id, turn_id):
            self.woken.append(turn_id)

        async def fake_business(conn, bid):
            return SimpleNamespace(id=bid, default_locale="ro")

        monkeypatch.setattr(tr, "get_pool", fake_pool)
        monkeypatch.setattr(tr, "admin_conn", fake_admin)
        monkeypatch.setattr(tr, "claimable_turns", fake_scan)
        monkeypatch.setattr(tr, "cancel_web_turn", fake_cancel)
        monkeypatch.setattr(tr, "claim_web_turn", fake_claim)
        monkeypatch.setattr(tr, "fail_web_turn", fake_fail)
        monkeypatch.setattr(tr, "wake_executor", fake_wake)
        monkeypatch.setattr(tr, "tenant_db", lambda bid: lambda op=None: _null_cm())
        monkeypatch.setattr(tr, "load_business", fake_business)


@asynccontextmanager
async def _null_cm(*a, **k):
    yield None


async def test_accepted_overdue_is_cancelled_renderable(monkeypatch):
    turn = _turn("accepted", deadline=_now() - timedelta(seconds=5))
    w = _Wired(monkeypatch, [turn])
    report = await tr.sweep_once(None)
    assert report.cancelled == 1 and report.failed == 0
    assert w.cancels[0]["code"] == "deadline_exceeded"
    assert not w.claims  # `accepted` n-are lease → nu se revendică pentru a fi anulat
    assert w.conn.unlocked  # advisory lock-ul se eliberează mereu


async def test_expired_running_overdue_is_claimed_then_failed(monkeypatch):
    turn = _turn("running", attempt=1, deadline=_now() - timedelta(seconds=5))
    w = _Wired(monkeypatch, [turn])
    report = await tr.sweep_once(None)
    assert report.failed == 1
    assert w.claims == [turn.turn_id]  # epoch+1: zombie-ul e deposedat ÎNAINTE de terminal
    assert w.fails[0]["code"] == "deadline_exceeded"
    assert w.fails[0]["lease_epoch"] == 2  # fail-ul e pe epoch-ul NOU


async def test_expired_running_with_exhausted_attempts_fails(monkeypatch):
    max_attempts = get_settings().web_turn_max_attempts
    turn = _turn("running", attempt=max_attempts, deadline=_now() + timedelta(seconds=60))
    w = _Wired(monkeypatch, [turn])
    report = await tr.sweep_once(None)
    assert report.failed == 1
    assert w.fails[0]["code"] == "attempts_exhausted"


async def test_healthy_accepted_is_rewoken_not_terminalized(monkeypatch):
    turn = _turn("accepted", deadline=_now() + timedelta(seconds=60))
    w = _Wired(monkeypatch, [turn])
    report = await tr.sweep_once(None)
    assert report.rewoken == 1 and report.cancelled == 0 and report.failed == 0
    assert w.woken == [turn.turn_id]  # wake pierdut → re-notificare din DB (failure matrix)


async def test_advisory_lock_busy_skips_scan(monkeypatch):
    w = _Wired(monkeypatch, [_turn()], lock_free=False)
    report = await tr.sweep_once(None)
    assert report.scanned == 0 and w.scanned == 0  # alt sweeper mătură; noi nu atingem nimic


async def test_cas_lost_between_scan_and_write_is_skipped(monkeypatch):
    turn = _turn("running", attempt=1, deadline=_now() - timedelta(seconds=5))
    w = _Wired(monkeypatch, [turn], claim=None)  # alt drum a luat/terminat turul între timp
    report = await tr.sweep_once(None)
    assert report.skipped == 1 and report.failed == 0
    assert not w.fails  # sweeperul nu forțează nimic — DB-ul a decis


async def test_one_broken_turn_does_not_stop_the_sweep(monkeypatch):
    bad = _turn("accepted", deadline=_now() - timedelta(seconds=5))
    good = _turn("accepted", deadline=_now() + timedelta(seconds=60))
    w = _Wired(monkeypatch, [bad, good])

    async def exploding_cancel(db, business_id, turn_id, **kw):
        raise RuntimeError("kaput")

    monkeypatch.setattr(tr, "cancel_web_turn", exploding_cancel)
    report = await tr.sweep_once(None)
    assert report.skipped == 1
    assert w.woken == [good.turn_id]  # al doilea turn e tot procesat


async def test_recovery_loop_survives_a_failing_sweep(monkeypatch):
    calls = {"n": 0}

    async def flaky_sweep(redis, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("kaput")
        raise asyncio.CancelledError  # oprește bucla după a doua trecere

    monkeypatch.setattr(tr, "sweep_once", flaky_sweep)
    monkeypatch.setattr(get_settings(), "web_turn_sweep_interval_s", 0.01)
    try:
        await asyncio.wait_for(tr.run_recovery_loop(None), timeout=2)
    except asyncio.CancelledError:
        pass
    assert calls["n"] == 2  # prima trecere a crăpat, bucla a continuat
