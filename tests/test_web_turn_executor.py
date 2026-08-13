"""NX-233 — executorul async: claim, lease heartbeat, fencing, deadline, finalizare (unit).

Fără DB/rețea: toate operațiile de ledger sunt seams monkeypatch-uite pe modulul executorului.
Garanțiile verificate:
  • happy path: claim → pipeline (cu `turn_id` = id-ul de ledger + input PRE-persistat) →
    view persistat prin hook-ul de commit → aftercare DUPĂ terminal;
  • claim pierdut / admission plină → turul rămâne pe loc (None), zero pipeline;
  • attempts peste plafon / deadline depășit → terminal onest, pipeline NEapelat;
  • pipeline crăpat → `failed` cu error-view (P6), fenced → rezultatul e ARUNCAT (zero fail);
  • deduped → deferred (îl reia lease expiry), no-reply → `empty_result`;
  • deadline în timpul pipeline-ului → cancel + `deadline_exceeded`;
  • lease pierdut (renew 0 rânduri) → pipeline anulat, outcome fenced;
  • heartbeat-ul folosește checkout scurt și se oprește la stop;
  • stage_hook: un stagiu de validator → phase `validating` (allowlisted), restul nu.
"""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from src.config import get_settings
from src.db.queries.web_turns import ClaimResult, ExecutionRefs, WebTurnRow
from src.models import Reply
from src.web import turn_executor as te
from src.web import turn_service as ts
from src.worker.admission import reset_admission
from src.worker.processor import TurnResult

NOW = datetime.now(UTC)


def _row(**over) -> WebTurnRow:
    base = dict(
        id=str(uuid4()),
        business_id="b1",
        conversation_id="c1",
        contact_id="ct1",
        session_ref_hash="h",
        client_turn_id=str(uuid4()),
        request_fingerprint="fp",
        schema_version="web-turn.v2",
        status="accepted",
        attempt=0,
        lease_owner=None,
        lease_epoch=0,
        lease_expires_at=None,
        # dinamic, nu la import: în suita COMPLETĂ testul rulează la minute după colectare
        deadline_at=datetime.now(UTC) + timedelta(seconds=60),
        conversation_revision_at_accept=0,
        pipeline_version=ts.RESPONSE_CONTRACT_SYNC_V1,
        response_json=None,
        safe_error_code=None,
        accepted_at=NOW,
        updated_at=NOW,
        completed_at=None,
    )
    base.update(over)
    return WebTurnRow(**base)


def _refs(**over) -> ExecutionRefs:
    base = dict(
        channel_id="chan",
        channel_kind="webchat",
        channel_account_id="tok",
        sender_external_id="web_1",
        inbound_msg_id="m1",
        safe_body="vreau un ser",
        content_type="text",
    )
    base.update(over)
    return ExecutionRefs(**base)


@asynccontextmanager
async def _fake_cm(*a, **k):
    yield None


def _fake_db(business_id):
    return _fake_cm


@pytest.fixture(autouse=True)
def _admission_off(monkeypatch):
    monkeypatch.setattr(get_settings(), "admission_enabled", False)
    reset_admission()
    yield
    reset_admission()


class _Wired:
    """Toate seams-urile executorului, cu înregistrare (ce s-a chemat, cu ce)."""

    def __init__(self, monkeypatch, row, *, claim, refs=None, handle=None):
        self.row = row
        self.failed: list[dict] = []
        self.completed: list[dict] = []
        self.events: list = []
        self.aftercare_runs = 0
        self.handle_calls = 0
        self.handle_kwargs: dict = {}

        async def fake_claim(db, business_id, turn_id, *, owner, lease_ttl_s):
            return claim

        async def fake_get(conn, business_id, turn_id):
            return row

        async def fake_business(conn, bid):
            return SimpleNamespace(id=bid, daily_cost_cap_usd=None, default_locale="ro")

        async def fake_refs(conn, business_id, turn_id):
            return refs if refs is not None else _refs()

        async def fake_fail(db, business_id, turn_id, *, lease_epoch, code, language):
            self.failed.append({"turn_id": turn_id, "code": code, "lease_epoch": lease_epoch})
            return True

        async def fake_complete(conn, business_id, turn_id, *, lease_epoch, view):
            self.completed.append({"turn_id": turn_id, "lease_epoch": lease_epoch, "view": view})

        async def fake_persist(db, business_id, conversation_id, contact_id, evs, **kw):
            self.events.extend(evs)

        async def fake_aftercare(db, redis, work):
            self.aftercare_runs += 1
            return 0.0

        async def default_handle(db, business, channel_id, event, **kw):
            self.handle_calls += 1
            self.handle_kwargs = {"event": event, **kw}
            reply = Reply(text="Salut! Uite serul.")
            hook = kw.get("commit_hook")
            if hook is not None:
                await hook(None, reply, "ro")
            return TurnResult(
                "c1",
                "ct1",
                "t1",
                reply.text,
                None,
                reply=reply,
                language="ro",
                aftercare=SimpleNamespace(),
            )

        async def counting_handle(db, business, channel_id, event, **kw):
            self.handle_calls += 1
            self.handle_kwargs = {"event": event, **kw}
            return await handle(db, business, channel_id, event, **kw)

        monkeypatch.setattr(te, "tenant_db", _fake_db)
        monkeypatch.setattr(te, "claim_web_turn", fake_claim)
        monkeypatch.setattr(te, "get_turn_by_id", fake_get)
        monkeypatch.setattr(te, "load_business", fake_business)
        monkeypatch.setattr(te, "load_execution_refs", fake_refs)
        monkeypatch.setattr(te, "fail_web_turn", fake_fail)
        monkeypatch.setattr(te, "complete_web_turn_on_conn", fake_complete)
        monkeypatch.setattr(te, "persist_events", fake_persist)
        monkeypatch.setattr(te, "run_aftercare", fake_aftercare)
        monkeypatch.setattr(
            te, "handle_turn", counting_handle if handle is not None else default_handle
        )


class _NoRedis:
    async def brpop(self, key, timeout=None):
        return None

    async def set(self, *a, **k):
        return True

    async def get(self, key):
        return None


def _claim(epoch=1, attempt=1, reclaimed=False) -> ClaimResult:
    return ClaimResult(lease_epoch=epoch, attempt=attempt, reclaimed=reclaimed)


async def test_happy_path_completes_and_runs_aftercare_after_terminal(monkeypatch):
    row = _row()
    w = _Wired(monkeypatch, row, claim=_claim())
    ex = te.WebTurnExecutor(_NoRedis(), owner="w-A")
    out = await ex.process_turn(te.AcceptedTurn(row.business_id, row.id))
    assert out.outcome == "completed"
    assert w.handle_calls == 1
    # pipeline-ul primește traiectoria LEDGERULUI: turn_id = id-ul rândului + inputul persistat
    assert w.handle_kwargs["turn_id"] == row.id
    assert w.handle_kwargs["preinserted_inbound_msg_id"] == "m1"
    assert w.handle_kwargs["deliver"] is False and w.handle_kwargs["defer_aftercare"] is True
    assert w.handle_kwargs["event"]["body"] == "vreau un ser"  # inputul SAFE din DB, nu din Redis
    # view-ul persistat prin hook = exact `render_web(reply)` (replay-ul viitor e identic)
    assert w.completed[0]["lease_epoch"] == 1
    assert "Salut!" in w.completed[0]["view"]["content"]
    assert w.aftercare_runs == 1  # STRICT după terminal commit
    types = [e.type for e in w.events]
    assert "web_turn_claimed" in types
    assert any(
        e.type == "web_turn_executed" and e.properties["outcome"] == "completed" for e in w.events
    )


async def test_claim_lost_means_someone_else_owns_it(monkeypatch):
    row = _row()
    w = _Wired(monkeypatch, row, claim=None)
    ex = te.WebTurnExecutor(_NoRedis())
    assert await ex.process_turn(te.AcceptedTurn(row.business_id, row.id)) is None
    assert w.handle_calls == 0 and not w.failed


async def test_attempts_exhausted_terminal_without_pipeline(monkeypatch):
    row = _row()
    w = _Wired(monkeypatch, row, claim=_claim(epoch=4, attempt=4, reclaimed=True))
    ex = te.WebTurnExecutor(_NoRedis())
    out = await ex.process_turn(te.AcceptedTurn(row.business_id, row.id))
    assert out.outcome == "failed" and out.code == "attempts_exhausted"
    assert w.handle_calls == 0
    assert w.failed[0]["code"] == "attempts_exhausted"
    assert any(e.type == "web_turn_reclaimed" for e in w.events)


async def test_deadline_already_passed_terminal_without_pipeline(monkeypatch):
    row = _row(deadline_at=NOW - timedelta(seconds=1))
    w = _Wired(monkeypatch, row, claim=_claim())
    ex = te.WebTurnExecutor(_NoRedis())
    out = await ex.process_turn(te.AcceptedTurn(row.business_id, row.id))
    assert out.outcome == "failed" and out.code == "deadline_exceeded"
    assert w.handle_calls == 0


async def test_missing_execution_refs_fails_honestly(monkeypatch):
    row = _row()
    w = _Wired(monkeypatch, row, claim=_claim(), refs=_refs(inbound_msg_id=None))
    ex = te.WebTurnExecutor(_NoRedis())
    out = await ex.process_turn(te.AcceptedTurn(row.business_id, row.id))
    assert out.outcome == "failed" and out.code == "processing_error"
    assert w.handle_calls == 0


async def test_pipeline_exception_fails_with_renderable_error(monkeypatch):
    row = _row()

    async def boom(db, business, channel_id, event, **kw):
        raise RuntimeError("kaput")

    w = _Wired(monkeypatch, row, claim=_claim(), handle=boom)
    ex = te.WebTurnExecutor(_NoRedis())
    out = await ex.process_turn(te.AcceptedTurn(row.business_id, row.id))
    assert out.outcome == "failed" and out.code == "processing_error"
    assert w.failed[0]["lease_epoch"] == 1  # fencing: fail-ul e CAS pe epoch-ul NOSTRU


async def test_fenced_completion_discards_result_without_fail(monkeypatch):
    row = _row()

    async def fenced(db, business, channel_id, event, **kw):
        raise ts.FencedTurnCompletion("alt owner")

    w = _Wired(monkeypatch, row, claim=_claim(), handle=fenced)
    ex = te.WebTurnExecutor(_NoRedis())
    out = await ex.process_turn(te.AcceptedTurn(row.business_id, row.id))
    assert out.outcome == "fenced"
    assert not w.failed and not w.completed  # rezultatul e ARUNCAT, nu suprascris
    assert any(e.type == "web_turn_fenced_write_rejected" for e in w.events)


async def test_deduped_result_is_deferred_to_lease_expiry(monkeypatch):
    row = _row()

    async def deduped(db, business, channel_id, event, **kw):
        return TurnResult(None, None, None, None, None, deduped=True)

    w = _Wired(monkeypatch, row, claim=_claim(), handle=deduped)
    ex = te.WebTurnExecutor(_NoRedis())
    out = await ex.process_turn(te.AcceptedTurn(row.business_id, row.id))
    assert out.outcome == "deferred"
    assert not w.failed  # nu terminalizăm: claim-ul durabil expiră odată cu lease-ul


async def test_no_reply_fails_with_empty_result(monkeypatch):
    row = _row()

    async def silent(db, business, channel_id, event, **kw):
        return TurnResult("c1", "ct1", "t1", None, None, language="ro")

    w = _Wired(monkeypatch, row, claim=_claim(), handle=silent)
    ex = te.WebTurnExecutor(_NoRedis())
    out = await ex.process_turn(te.AcceptedTurn(row.business_id, row.id))
    assert out.outcome == "failed" and out.code == "empty_result"
    assert w.failed[0]["code"] == "empty_result"


async def test_deadline_during_pipeline_cancels_and_fails(monkeypatch):
    row = _row(deadline_at=datetime.now(UTC) + timedelta(seconds=0.3))
    cancelled = {"flag": False}

    async def slow(db, business, channel_id, event, **kw):
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled["flag"] = True
            raise
        return TurnResult(None, None, None, None, None)

    w = _Wired(monkeypatch, row, claim=_claim(), handle=slow)
    ex = te.WebTurnExecutor(_NoRedis())
    out = await ex.process_turn(te.AcceptedTurn(row.business_id, row.id))
    assert out.outcome == "failed" and out.code == "deadline_exceeded"
    assert cancelled["flag"]  # pipeline-ul a fost ANULAT, nu lăsat să scrie mai târziu
    assert w.failed[0]["code"] == "deadline_exceeded"


async def test_lost_lease_cancels_pipeline_and_discards(monkeypatch):
    row = _row(deadline_at=datetime.now(UTC) + timedelta(seconds=60))

    async def slow(db, business, channel_id, event, **kw):
        await asyncio.sleep(30)
        return TurnResult(None, None, None, None, None)

    w = _Wired(monkeypatch, row, claim=_claim(), handle=slow)

    async def renew_denied(conn, business_id, turn_id, **kw):
        return False  # am fost deposedați (reclaim de alt worker)

    monkeypatch.setattr(te, "renew_lease", renew_denied)
    monkeypatch.setattr(get_settings(), "web_turn_heartbeat_s", 0.05)
    ex = te.WebTurnExecutor(_NoRedis())
    out = await ex.process_turn(te.AcceptedTurn(row.business_id, row.id))
    assert out.outcome == "fenced"
    assert not w.failed and not w.completed  # zombie-ul nu scrie NIMIC după reclaim


async def test_lease_guard_heartbeat_renews_and_stops(monkeypatch):
    calls = []

    async def renew_ok(conn, business_id, turn_id, *, owner, lease_epoch, lease_ttl_s):
        calls.append((owner, lease_epoch, lease_ttl_s))
        return True

    monkeypatch.setattr(te, "renew_lease", renew_ok)
    claim = te.TurnClaim("b1", "t1", lease_epoch=2, attempt=1, reclaimed=False)
    guard = te.LeaseGuard(_fake_db("b1"), claim, owner="w-A", ttl_s=300, interval_s=0.02)
    guard.start()
    await asyncio.sleep(0.08)
    await guard.stop()
    n = len(calls)
    assert n >= 2 and calls[0] == ("w-A", 2, 300)
    await asyncio.sleep(0.05)
    assert len(calls) == n  # oprit GARANTAT — niciun tick după stop


async def test_stage_hook_sets_validating_phase_only_for_validator_stages(monkeypatch):
    row = _row()
    phases = []

    async def fake_set_phase(redis, business_id, turn_id, phase, *, ttl_s):
        phases.append(phase)

    async def hook_probe(db, business, channel_id, event, **kw):
        hook = kw["stage_hook"]
        hook("agent_stage")
        hook("validator_stage")
        await asyncio.sleep(0)  # lasă create_task-urile să ruleze
        reply = Reply(text="ok")
        await kw["commit_hook"](None, reply, "ro")
        return TurnResult("c1", "ct1", "t1", reply.text, None, reply=reply, language="ro")

    monkeypatch.setattr(te, "set_phase", fake_set_phase)
    w = _Wired(monkeypatch, row, claim=_claim(), handle=hook_probe)
    out = await te.WebTurnExecutor(_NoRedis()).process_turn(
        te.AcceptedTurn(row.business_id, row.id)
    )
    assert out.outcome == "completed"
    assert phases[0] == "working"  # setat la claim
    assert "validating" in phases  # DOAR stagiul de validator o setează
    assert phases.count("validating") == 1
    assert w.handle_calls == 1


async def test_run_loop_stops_on_request_stop(monkeypatch):
    ex = te.WebTurnExecutor(_NoRedis())

    async def no_refs():
        ex.request_stop()  # primul scan cere oprirea
        return []

    monkeypatch.setattr(ex, "_next_refs", no_refs)
    await asyncio.wait_for(ex.run(), timeout=2)  # iese curat, fără cancel


def test_executor_types_carry_no_raw_body_or_token():
    """Tipurile executorului (AcceptedTurn/TurnClaim/TerminalCommit) transportă DOAR id-uri,
    epoch-uri și coduri — niciun câmp de body, token sau conexiune (contractul din card)."""
    for cls in (te.AcceptedTurn, te.TurnClaim, te.TerminalCommit):
        fields = set(cls.__dataclass_fields__)
        assert not fields & {"body", "text", "token", "sig", "conn", "connection"}
