"""NX-232 — ledgerul durabil al turelor web: state machine, proiecție, fingerprint, accept,
seam-ul de commit atomic și comportamentul `/web/chat` sub flag (fără DB/rețea reală).

Garanțiile verificate AICI (unit; cele care cer Postgres real sunt în test_web_turns_db.py):
  • state machine: terminalele sunt finale, `accepted→completed` interzis, harta e închisă;
  • proiecția ledger→wire: `running` NU apare NICIODATĂ pe sârmă; `validating` doar cu phase
    server-owned real; terminalele trec neschimbate — exhaustiv pe toate combinațiile;
  • fingerprint: canonic, HMAC, ne-inversabil (body-ul nu apare în valoare), sensibil la
    input/context/secret;
  • accept: conflictul de fingerprint bate replay-ul; terminal → replay; activ → in-progress;
  • commit hook: rulează ÎN tranzacția TurnCommit; dacă ridică, tot commit-ul se anulează;
  • /web/chat cu flag ON: duplicatul terminal REJOACĂ exact răspunsul persistat (fără al
    doilea apel pipeline), duplicatul activ întoarce status+turn ID (NU blank), același ID cu
    alt input → 409, fenced → rezultatul celuilalt; flag OFF → calea veche, neatinsă.
"""

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from src.config import get_settings
from src.db.provider import static_db
from src.db.queries.web_turns import ClaimResult, WebTurnRow
from src.models import Reply
from src.web import app as wa
from src.web import turn_service as ts
from src.web.app import WebChatIn
from src.web.session import WebSession
from src.worker.processor import TurnResult
from src.worker.turn_uow import TurnCommit, commit_turn

NOW = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)


def _row(**over) -> WebTurnRow:
    base = dict(
        id=str(uuid4()),
        business_id="b1",
        conversation_id="c1",
        contact_id="ct1",
        session_ref_hash="abc",
        client_turn_id=str(uuid4()),
        request_fingerprint="fp",
        schema_version="web-turn.v2",
        status="accepted",
        attempt=0,
        lease_owner=None,
        lease_epoch=0,
        lease_expires_at=None,
        deadline_at=None,
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


# ── State machine ─────────────────────────────────────────────────────────────


def test_transition_map_is_exactly_the_allowed_set():
    """Harta e ÎNCHISĂ: orice pereche în afara ALLOWED_TRANSITIONS e interzisă."""
    for a in ts.LEDGER_STATUSES:
        for b in ts.LEDGER_STATUSES:
            assert ts.can_transition(a, b) == ((a, b) in ts.ALLOWED_TRANSITIONS)


def test_terminal_states_are_final():
    for terminal in ts.TERMINAL_LEDGER_STATUSES:
        for target in ts.LEDGER_STATUSES:
            assert not ts.can_transition(terminal, target)


def test_accepted_cannot_jump_to_terminal_result():
    """Fără claim nu există single-flight: accepted→completed|failed e interzis."""
    assert not ts.can_transition("accepted", "completed")
    assert not ts.can_transition("accepted", "failed")
    # dar cancel din accepted e permis (nimeni nu lucrează încă)
    assert ts.can_transition("accepted", "cancelled")


def test_nothing_reenters_accepted_and_unknown_is_rejected():
    for status in ts.LEDGER_STATUSES:
        assert not ts.can_transition(status, "accepted")
    assert not ts.can_transition("accepted", "working")  # stare de WIRE, nu de ledger
    assert not ts.can_transition("validating", "completed")  # nu există în ledger


# ── Proiecția ledger → wire (exhaustivă) ──────────────────────────────────────


def test_projection_exhaustive_running_never_on_wire():
    phases = (None, "", "tools", "model", ts.VALIDATING_PHASE)
    for status in ts.LEDGER_STATUSES:
        for phase in phases:
            wire = ts.project_wire_status(status, phase)
            assert wire != "running"  # enumul intern NU se scurge pe sârmă
            if status in ts.TERMINAL_LEDGER_STATUSES or status == "accepted":
                assert wire == status  # passthrough exact
            else:  # running
                expected = "validating" if phase == ts.VALIDATING_PHASE else "working"
                assert wire == expected


def test_projection_validating_requires_real_phase():
    assert ts.project_wire_status("running", None) == "working"
    assert ts.project_wire_status("running", "validating") == "validating"
    # `validating` nu se inventează pentru alte statusuri, indiferent de phase
    assert ts.project_wire_status("accepted", "validating") == "accepted"


def test_projection_rejects_unknown_status():
    with pytest.raises(ValueError):
        ts.project_wire_status("working")  # stare de wire, nu de ledger
    with pytest.raises(ValueError):
        ts.project_wire_status("mystery")


# ── Fingerprint ───────────────────────────────────────────────────────────────


def test_fingerprint_deterministic_and_sensitive():
    kw = dict(business_id="b1", channel_token="tok", text="vreau un șampon")
    fp1 = ts.request_fingerprint("sek", **kw)
    assert fp1 == ts.request_fingerprint("sek", **kw)  # determinist
    assert len(fp1) == 64 and all(c in "0123456789abcdef" for c in fp1)
    assert fp1 != ts.request_fingerprint("sek", **{**kw, "text": "vreau un sampon"})
    assert fp1 != ts.request_fingerprint("sek", **{**kw, "business_id": "b2"})
    assert fp1 != ts.request_fingerprint("sek", **{**kw, "channel_token": "alt"})
    assert fp1 != ts.request_fingerprint("alt-secret", **kw)
    assert fp1 != ts.request_fingerprint("sek", **kw, content_type="image")


def test_fingerprint_and_session_ref_do_not_leak_input():
    """P12: nici body-ul, nici visitor_id-ul brut nu apar în valorile stocate."""
    text = "telefonul meu e 0722123456"
    fp = ts.request_fingerprint("sek", business_id="b1", channel_token="tok", text=text)
    assert text not in fp and "0722123456" not in fp
    ref = ts.session_ref_hash("tok", "web_abcdef123")
    assert "web_abcdef123" not in ref and len(ref) == 32


# ── inspect_existing (ordinea deciziilor) ─────────────────────────────────────


def test_conflict_wins_over_replay():
    """Același ID cu ALT fingerprint e conflict CHIAR dacă rândul e terminal — nu servim
    răspunsul turului A unui request B diferit."""
    row = _row(status="completed", response_json={"content": "x"}, request_fingerprint="fp-A")
    out = ts.inspect_existing(row, "fp-B")
    assert isinstance(out, ts.IdempotencyConflict)


def test_terminal_same_fingerprint_replays():
    row = _row(status="failed", response_json={"content": "err"}, request_fingerprint="fp")
    assert isinstance(ts.inspect_existing(row, "fp"), ts.ExistingCompleted)


def test_active_same_fingerprint_is_in_progress():
    for status in ("accepted", "running"):
        row = _row(status=status, request_fingerprint="fp")
        assert isinstance(ts.inspect_existing(row, "fp"), ts.ExistingInProgress)


# ── error_view / renderable (P6) ──────────────────────────────────────────────


def test_error_view_renderable_for_all_codes_and_locales():
    for lang in ("ro", "en", "hu", "de"):  # de → fallback pe pilot, nu KeyError
        for code in ("empty_result", "processing_error", "cancelled", "unknown_code"):
            view = ts.error_view(code, lang)
            assert ts.renderable(view)
            assert view["error_code"] == code


def test_renderable_rejects_blank_and_accepts_products_only():
    assert not ts.renderable(None)
    assert not ts.renderable({"content": "", "products": [], "suggestions": []})
    assert ts.renderable({"content": "", "products": [{"name": "X"}], "suggestions": []})


def test_attempt_bucket_low_cardinality():
    assert ts.attempt_bucket(1) == "1"
    assert ts.attempt_bucket(2) == "2"
    assert ts.attempt_bucket(7) == "3+"


# ── accept_web_turn (service, cu seams) ───────────────────────────────────────


def _wire_accept_seams(monkeypatch, *, insert_result, existing=None):
    async def fake_contact(conn, business_id, kind, ext, **kw):
        return SimpleNamespace(id="ct1")

    async def fake_conv(conn, business_id, contact_id, channel_id, locale=None):
        return {"id": "c1", "state_version": 3}

    async def fake_insert(conn, *a, **kw):
        return insert_result

    async def fake_get(conn, business_id, conversation_id, client_turn_id):
        return existing

    async def fake_get_active(conn, business_id, conversation_id):
        return None  # NX-233: referința la turnul activ (aici nu testăm îmbogățirea)

    monkeypatch.setattr(ts, "get_or_create_contact", fake_contact)
    monkeypatch.setattr(ts, "get_or_create_conversation", fake_conv)
    monkeypatch.setattr(ts, "insert_turn", fake_insert)
    monkeypatch.setattr(ts, "get_turn", fake_get)
    monkeypatch.setattr(ts, "get_active_turn", fake_get_active)


_ACCEPT_KW = dict(
    business_id="b1",
    channel_id="chan",
    channel_kind="webchat",
    channel_token="tok",
    sender_external_id="web_1",
    client_turn_id=str(uuid4()),
    fingerprint="fp",
)


async def test_accept_new_row(monkeypatch):
    row = _row()
    _wire_accept_seams(monkeypatch, insert_result=row)
    out = await ts.accept_web_turn(static_db(None), **_ACCEPT_KW)
    assert isinstance(out, ts.Accepted) and out.row is row


async def test_accept_existing_completed_replays(monkeypatch):
    existing = _row(status="completed", response_json={"content": "salut"})
    _wire_accept_seams(monkeypatch, insert_result=None, existing=existing)
    out = await ts.accept_web_turn(static_db(None), **_ACCEPT_KW)
    assert isinstance(out, ts.ExistingCompleted)
    assert out.row.response_json == {"content": "salut"}


async def test_accept_single_flight_conflict(monkeypatch):
    """INSERT eșuat + rândul cu ID-ul nostru NU există ⇒ unique-ul lovit a fost
    single-flight-ul (alt turn activ pe conversație)."""
    _wire_accept_seams(monkeypatch, insert_result=None, existing=None)
    out = await ts.accept_web_turn(static_db(None), **_ACCEPT_KW)
    assert isinstance(out, ts.ActiveTurnConflict)


# ── complete_web_turn_on_conn (P6 + fencing) ──────────────────────────────────


async def test_complete_refuses_empty_terminal(monkeypatch):
    with pytest.raises(ts.EmptyTerminalResult):
        await ts.complete_web_turn_on_conn(
            None, "b1", "t1", lease_epoch=1, view={"content": "", "products": []}
        )


async def test_complete_raises_fenced_on_zero_rows(monkeypatch):
    async def fake_complete(conn, business_id, turn_id, *, lease_epoch, response_json):
        return False  # 0 rânduri: alt owner a reclamat

    monkeypatch.setattr(ts, "complete_turn", fake_complete)
    with pytest.raises(ts.FencedTurnCompletion):
        await ts.complete_web_turn_on_conn(None, "b1", "t1", lease_epoch=1, view={"content": "ok"})


# ── Seam-ul on_commit din TurnCommit ─────────────────────────────────────────


class _Tx:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        self._conn.tx_depth += 1
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self._conn.tx_depth -= 1
        if exc_type is not None:
            self._conn.rolled_back = True
        return False


class _CommitConn:
    """Conn minimal pentru commit_turn cu fragmente goale: doar patch-ul de stare."""

    def __init__(self):
        self.tx_depth = 0
        self.rolled_back = False

    def transaction(self):
        return _Tx(self)

    async def fetchval(self, sql, *a):
        return 2  # patch_conversation_state → new_version

    async def execute(self, sql, *a):
        return "UPDATE 1"


def _commit(business_id="b1") -> TurnCommit:
    return TurnCommit(
        business_id=business_id,
        conversation_id="c1",
        contact_id="ct1",
        fragments=[],
        new_state={},
        expected_version=1,
        provider_msg_id=None,
        deliver=False,
    )


async def test_on_commit_runs_inside_transaction():
    conn = _CommitConn()
    seen = {}

    async def hook(c):
        seen["conn"] = c
        seen["tx_depth"] = c.tx_depth

    await commit_turn(static_db(conn), _commit(), rebuild_state=lambda s: s, on_commit=hook)
    assert seen["conn"] is conn
    assert seen["tx_depth"] == 1  # ÎN tranzacție, nu după


async def test_on_commit_failure_rolls_back_everything():
    conn = _CommitConn()

    async def hook(c):
        raise ts.FencedTurnCompletion("fenced")

    with pytest.raises(ts.FencedTurnCompletion):
        await commit_turn(static_db(conn), _commit(), rebuild_state=lambda s: s, on_commit=hook)
    assert conn.rolled_back  # mesaj + stare + rezultat: împreună sau deloc


# ── /web/chat sub flag (regresia „duplicate blank") ───────────────────────────


class _FakeRedis:
    async def incr(self, key):
        return 1

    async def expire(self, key, ttl):
        return True

    async def get(self, key):
        return None

    async def incrbyfloat(self, key, amount):
        return float(amount)


class _Req:
    def __init__(self, body=b"{}"):
        self.client = SimpleNamespace(host="1.2.3.4")
        self._body = body
        self.headers = {"content-length": str(len(body))}

    async def stream(self):
        yield self._body


@asynccontextmanager
async def _fake_cm(*a, **k):
    yield None


async def _coro(value):
    return value


def _chat_req(client_msg_id):
    return WebChatIn(
        token="tok", visitor_id="web_1", sig="s", message="hei", client_msg_id=client_msg_id
    )


def _wire_endpoint(monkeypatch, *, accept=None, claim=None, handle=None, refreshed=None):
    """Seams comune pentru web_chat cu flag ON: sesiune, canal, business, ledger."""
    settings = get_settings()
    monkeypatch.setattr(settings, "web_turn_ledger_enabled", True)
    monkeypatch.setattr(settings, "web_turn_lock_enabled", False)
    monkeypatch.setattr(settings, "web_admission_enabled", False)

    async def fake_verify(token, vid, sig):
        return WebSession(business_id="b1", token=token, visitor_id=vid)

    async def fake_resolve_channel(conn, kind, token):
        return {"channel_id": "chan", "business_id": "b1"}

    async def fake_load_business(conn, bid):
        return SimpleNamespace(id=bid, daily_cost_cap_usd=None, default_locale="ro")

    events: list = []

    async def fake_persist(db, business_id, conversation_id, contact_id, evs, **kw):
        events.extend(evs)

    async def fake_get_turn(conn, business_id, conversation_id, client_turn_id):
        return refreshed

    monkeypatch.setattr(wa, "_verify", fake_verify)
    monkeypatch.setattr(wa, "get_redis", lambda: _coro(_FakeRedis()))
    monkeypatch.setattr(wa, "get_pool", lambda: _coro(None))
    monkeypatch.setattr(wa, "admin_conn", _fake_cm)
    monkeypatch.setattr(wa, "tenant_db", lambda business_id: _fake_cm)
    monkeypatch.setattr(wa, "resolve_channel", fake_resolve_channel)
    monkeypatch.setattr(wa, "load_business", fake_load_business)
    monkeypatch.setattr(wa, "persist_events", fake_persist)
    monkeypatch.setattr(wa, "get_turn", fake_get_turn)
    if accept is not None:
        monkeypatch.setattr(wa, "accept_web_turn", accept)
    if claim is not None:
        monkeypatch.setattr(wa, "claim_web_turn", claim)
    if handle is not None:
        monkeypatch.setattr(wa, "handle_turn", handle)
    return events


async def test_duplicate_completed_replays_exact_response_without_pipeline(monkeypatch):
    """REGRESIA NX-232: duplicatul unui turn terminat NU mai întoarce content gol, ci EXACT
    răspunsul persistat — și pipeline-ul (LLM-ul) nu mai e apelat deloc."""
    stored = {"content": "Salut! Uite serul.", "products": [{"name": "Ser X"}], "suggestions": []}
    row = _row(status="completed", response_json=stored, request_fingerprint=_fp())
    called = {"pipeline": 0}

    async def fake_accept(db, **kw):
        return ts.ExistingCompleted(row)

    async def fake_handle(*a, **k):
        called["pipeline"] += 1
        raise AssertionError("pipeline-ul NU trebuie apelat pe replay")

    events = _wire_endpoint(monkeypatch, accept=fake_accept, handle=fake_handle, refreshed=row)
    res = await wa.web_chat(_chat_req(row.client_turn_id), _Req())

    assert res == stored  # replay EXACT, nu o reconstrucție
    assert called["pipeline"] == 0
    assert {e.type for e in events} >= {"web_turn_accepted", "web_turn_replayed"}


async def test_duplicate_in_progress_returns_status_not_blank(monkeypatch):
    row = _row(status="running", lease_owner="w1", lease_epoch=1)

    async def fake_accept(db, **kw):
        return ts.ExistingInProgress(row)

    _wire_endpoint(monkeypatch, accept=fake_accept, refreshed=row)
    res = await wa.web_chat(_chat_req(row.client_turn_id), _Req())

    assert "turn" in res  # NU blank mut: status + turn ID
    assert res["turn"]["status"] == "working"  # `running` nu iese pe sârmă
    assert res["turn"]["id"] == row.id


async def test_same_id_different_input_409(monkeypatch):
    row = _row(status="completed", response_json={"content": "x"}, request_fingerprint="ALT")

    async def fake_accept(db, **kw):
        return ts.IdempotencyConflict(row)

    _wire_endpoint(monkeypatch, accept=fake_accept)
    with pytest.raises(wa.HTTPException) as ei:
        await wa.web_chat(_chat_req(str(uuid4())), _Req())
    assert ei.value.status_code == 409
    assert ei.value.detail == "idempotency_conflict"


async def test_other_active_turn_409(monkeypatch):
    async def fake_accept(db, **kw):
        return ts.ActiveTurnConflict()

    _wire_endpoint(monkeypatch, accept=fake_accept)
    with pytest.raises(wa.HTTPException) as ei:
        await wa.web_chat(_chat_req(str(uuid4())), _Req())
    assert ei.value.status_code == 409
    assert ei.value.detail == "active_turn"


async def test_new_turn_persists_and_serves_same_view(monkeypatch):
    """Happy path cu flag ON: hook-ul de commit persistă view-ul, iar răspunsul HTTP e EXACT
    acel view (replay-ul viitor va fi byte-identic)."""
    row = _row()
    completed = {}

    async def fake_accept(db, **kw):
        return ts.Accepted(row)

    async def fake_claim(db, business_id, turn_id, *, owner, lease_ttl_s):
        return ClaimResult(lease_epoch=1, attempt=1, reclaimed=False)

    async def fake_complete(conn, business_id, turn_id, *, lease_epoch, view):
        completed.update(turn_id=turn_id, lease_epoch=lease_epoch, view=view)

    async def fake_handle(db, business, channel_id, event, *, commit_hook=None, **kw):
        reply = Reply(text="Salut! Cu ce te ajut?")
        await commit_hook(None, reply, "ro")  # procesorul rulează hook-ul în tranzacția commit
        return TurnResult("c1", "ct1", "t1", reply.text, None, reply=reply, language="ro")

    events = _wire_endpoint(monkeypatch, accept=fake_accept, claim=fake_claim, handle=fake_handle)
    monkeypatch.setattr(wa, "complete_web_turn_on_conn", fake_complete)
    res = await wa.web_chat(_chat_req(row.client_turn_id), _Req())

    assert completed["turn_id"] == row.id and completed["lease_epoch"] == 1
    assert res == completed["view"]  # servit EXACT ce s-a persistat
    assert "Salut!" in res["content"]
    types = [e.type for e in events]
    assert "web_turn_accepted" in types and "web_turn_claimed" in types
    assert "web_turn_reclaimed" not in types


async def test_fenced_completion_serves_the_other_workers_result(monkeypatch):
    """Owner vechi, fenced la commit: rezultatul LUI e aruncat, al noului owner e servit."""
    row = _row()
    winner = _row(
        id=row.id,
        client_turn_id=row.client_turn_id,
        status="completed",
        response_json={"content": "răspunsul câștigătorului", "products": [], "suggestions": []},
    )

    async def fake_accept(db, **kw):
        return ts.Accepted(row)

    async def fake_claim(db, *a, **kw):
        return ClaimResult(lease_epoch=1, attempt=2, reclaimed=True)

    async def fake_handle(*a, **kw):
        raise ts.FencedTurnCompletion("fenced")

    events = _wire_endpoint(
        monkeypatch, accept=fake_accept, claim=fake_claim, handle=fake_handle, refreshed=winner
    )
    res = await wa.web_chat(_chat_req(row.client_turn_id), _Req())

    assert res == winner.response_json
    types = [e.type for e in events]
    assert "web_turn_fenced_completion_rejected" in types
    assert "web_turn_reclaimed" in types


async def test_deduped_turn_consults_ledger_not_blank(monkeypatch):
    """`snap.deduped` (claim durabil ținut de un tur crăpat recent) NU mai produce blank:
    marginea consultă ledgerul și întoarce statusul canonic."""
    row = _row(status="running", lease_owner="w0", lease_epoch=1)

    async def fake_accept(db, **kw):
        return ts.Accepted(row)

    async def fake_claim(db, *a, **kw):
        return ClaimResult(lease_epoch=2, attempt=2, reclaimed=True)

    async def fake_handle(db, business, channel_id, event, **kw):
        return TurnResult(None, None, None, None, None, deduped=True)

    _wire_endpoint(
        monkeypatch, accept=fake_accept, claim=fake_claim, handle=fake_handle, refreshed=row
    )
    res = await wa.web_chat(_chat_req(row.client_turn_id), _Req())

    assert res != {"content": "", "products": [], "suggestions": []}  # NU blank-ul vechi
    assert res["turn"]["status"] == "working"


async def test_no_reply_turn_fails_with_safe_error_view(monkeypatch):
    """P6: tur fără reply pe calea cu ledger → terminal ONEST (error-view safe), nu tăcere."""
    row = _row()
    failed = {}

    async def fake_accept(db, **kw):
        return ts.Accepted(row)

    async def fake_claim(db, *a, **kw):
        return ClaimResult(lease_epoch=1, attempt=1, reclaimed=False)

    async def fake_handle(db, business, channel_id, event, **kw):
        return TurnResult("c1", "ct1", "t1", None, None, language="ro")

    async def fake_fail(db, business_id, turn_id, *, lease_epoch, code, language):
        failed.update(turn_id=turn_id, code=code, lease_epoch=lease_epoch)
        return True

    _wire_endpoint(monkeypatch, accept=fake_accept, claim=fake_claim, handle=fake_handle)
    monkeypatch.setattr(wa, "fail_web_turn", fake_fail)
    res = await wa.web_chat(_chat_req(row.client_turn_id), _Req())

    assert failed["code"] == "empty_result" and failed["turn_id"] == row.id
    assert ts.renderable(res)  # niciun terminal gol
    assert res["error_code"] == "empty_result"


async def test_flag_off_keeps_legacy_path(monkeypatch):
    """OFF = byte-identic cu v1: ledgerul nu e atins deloc."""

    async def fake_accept(db, **kw):
        raise AssertionError("ledgerul NU trebuie atins cu flagul OFF")

    async def fake_handle(db, business, channel_id, event, **kw):
        reply = Reply(text="Salut!")
        return TurnResult("c1", "ct1", "t1", reply.text, None, reply=reply, language="ro")

    _wire_endpoint(monkeypatch, accept=fake_accept, handle=fake_handle)
    monkeypatch.setattr(get_settings(), "web_turn_ledger_enabled", False)
    res = await wa.web_chat(_chat_req(str(uuid4())), _Req())
    assert "Salut!" in res["content"] and "turn" not in res


async def test_non_uuid_client_id_falls_back_to_legacy(monkeypatch):
    async def fake_accept(db, **kw):
        raise AssertionError("fără UUID nu există cheie de replay → calea veche")

    async def fake_handle(db, business, channel_id, event, **kw):
        reply = Reply(text="Salut!")
        return TurnResult("c1", "ct1", "t1", reply.text, None, reply=reply, language="ro")

    _wire_endpoint(monkeypatch, accept=fake_accept, handle=fake_handle)
    res = await wa.web_chat(_chat_req("nu-e-uuid"), _Req())
    assert "Salut!" in res["content"]


def _fp() -> str:
    """Fingerprint-ul REAL pe care web_chat îl calculează pentru requestul din _chat_req."""
    return ts.request_fingerprint(
        get_settings().web_turn_fingerprint_secret,
        business_id="b1",
        channel_token="tok",
        text="hei",
    )
