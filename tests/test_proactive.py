"""NX-70 — motorul proactiv. Fake conn + monkeypatch pe deps (model test_rollup_usage).

ZERO DB real, ZERO apel Meta/Telegram, ZERO LLM în CI. Poarta NX-71 e mock-uită
(testată separat în test_proactive_gating). Aici testăm: rutare → build → poartă →
outbox + mark (atomic), idempotența, skip-urile, izolarea unui job picat.
"""

from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from src.models import Contact, Event
from src.proactive import builders, scheduler
from src.proactive.builders import BuildError, MessageSpec
from src.proactive.templates import ProactiveDecision


class FakeTxn:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *a):
        return False


class FakeConn:
    """Stub conn: `transaction()` e un context manager no-op (savepoint simulat)."""

    def transaction(self):
        return FakeTxn()


def _contact(consent=None) -> Contact:
    return Contact(id="c1", business_id="b1", consent=consent or {"proactive": True})


def _job(**kw):
    base = {
        "id": "job1",
        "kind": "awb_update",
        "contact_id": "c1",
        "conversation_id": "conv1",
        "payload": {"awb": "123", "carrier": "FAN"},
        "template_id": None,
        "scheduled_at": None,
    }
    base.update(kw)
    return base


ROUTE = {"id": "conv1", "channel_id": "ch1", "locale": "ro", "channel_kind": "telegram"}


def _patch_engine(
    monkeypatch,
    *,
    route=ROUTE,
    contact=None,
    to="chat-9",
    spec=None,
    decision=None,
    enqueue_id="ob1",
):
    """Patch-uiește toate dependențele lui `_process_job` în namespace-ul scheduler-ului."""
    calls = {"mark": [], "enqueue": []}

    async def f_route(conn, biz, cid):
        return route

    async def f_contact(conn, biz, cid):
        return contact or _contact()

    async def f_to(conn, biz, cid, kind):
        return to

    async def f_spec(conn, biz, job, r):
        return spec or MessageSpec(free_text="hi", template_name="awb_update", variables={})

    async def f_decide(conn, **kw):
        return decision or ProactiveDecision(
            allowed=True, mode="free", reason="ok_free", rendered_text="hi"
        )

    async def f_enqueue(conn, biz, conv, idem, payload, *, kind, priority=None):
        calls["enqueue"].append((conv, idem, payload, kind))
        return enqueue_id

    async def f_mark(conn, biz, jid, status):
        calls["mark"].append((jid, status))

    monkeypatch.setattr(scheduler, "get_proactive_route", f_route)
    monkeypatch.setattr(scheduler, "get_contact_by_id", f_contact)
    monkeypatch.setattr(scheduler, "get_recipient_external_id", f_to)
    monkeypatch.setattr(scheduler, "build_message_spec", f_spec)
    monkeypatch.setattr(scheduler, "decide_proactive", f_decide)
    monkeypatch.setattr(scheduler, "enqueue_outbox", f_enqueue)
    monkeypatch.setattr(scheduler, "mark_job", f_mark)
    return calls


# --------------------------------------------------------------------------- #
# _process_job — happy path, idempotență, skip-uri, template blocat în v1
# --------------------------------------------------------------------------- #


async def test_free_enqueues_text_and_marks_sent(monkeypatch):
    calls = _patch_engine(monkeypatch)
    events: list[Event] = []
    await scheduler._process_job(FakeConn(), "b1", _job(), events)
    assert calls["enqueue"] == [
        # kind='message' = transport valid (CHECK + dispatcher); proactivul e în idempotency_key
        ("conv1", "proactive:job1", {"type": "text", "to": "chat-9", "text": "hi"}, "message")
    ]
    assert calls["mark"] == [("job1", "sent")]
    assert events[0].type == "proactive_enqueued"
    assert events[0].properties == {"kind": "awb_update", "deduped": False, "mode": "free"}


async def test_rerun_deduped_when_enqueue_returns_none(monkeypatch):
    calls = _patch_engine(monkeypatch, enqueue_id=None)
    events: list[Event] = []
    await scheduler._process_job(FakeConn(), "b1", _job(), events)
    assert calls["mark"] == [("job1", "sent")]  # tot sent (re-run idempotent)
    assert events[0].properties["deduped"] is True


async def test_no_optin_skips_without_enqueue(monkeypatch):
    calls = _patch_engine(monkeypatch, decision=ProactiveDecision(False, "blocked", "no_optin"))
    events: list[Event] = []
    await scheduler._process_job(FakeConn(), "b1", _job(), events)
    assert calls["enqueue"] == []
    assert calls["mark"] == [("job1", "skipped_no_optin")]
    assert events[0].type == "proactive_skipped"
    assert events[0].properties == {"kind": "awb_update", "reason": "no_optin"}


async def test_no_window_no_template_skips(monkeypatch):
    calls = _patch_engine(
        monkeypatch, decision=ProactiveDecision(False, "blocked", "no_window_no_template")
    )
    events: list[Event] = []
    await scheduler._process_job(FakeConn(), "b1", _job(), events)
    assert calls["enqueue"] == []
    assert calls["mark"] == [("job1", "skipped_no_window")]


async def test_template_mode_enqueues_template(monkeypatch):
    """PL-1: calea template e LIVE — în afara ferestrei 24h, motorul pune un payload
    `type=template` în outbox (name/language/params), nu mai dă skip."""
    calls = _patch_engine(
        monkeypatch,
        decision=ProactiveDecision(
            True,
            "template",
            "ok_template",
            rendered_text="AWB 123 la FAN",
            template_id="x",
            provider_template_id="y",
            template_name="awb_update",
            template_language="ro",
            template_params=["123", "FAN"],
        ),
    )
    events: list[Event] = []
    await scheduler._process_job(FakeConn(), "b1", _job(), events)
    assert calls["enqueue"] == [
        (
            "conv1",
            "proactive:job1",
            {
                "type": "template",
                "to": "chat-9",
                "text": "AWB 123 la FAN",  # floor de degradare pe canale fără TEMPLATE
                "template_name": "awb_update",
                "language": "ro",
                "params": ["123", "FAN"],
            },
            "message",
        )
    ]
    assert calls["mark"] == [("job1", "sent")]
    assert events[0].type == "proactive_enqueued"
    assert events[0].properties == {"kind": "awb_update", "deduped": False, "mode": "template"}


async def test_cancel_when_spec_cancel(monkeypatch):
    calls = _patch_engine(monkeypatch, spec=MessageSpec(cancel=True))
    events: list[Event] = []
    await scheduler._process_job(FakeConn(), "b1", _job(kind="abandoned_cart"), events)
    assert calls["enqueue"] == []
    assert calls["mark"] == [("job1", "cancelled")]
    assert events[0].properties == {"kind": "abandoned_cart", "reason": "cancelled"}


@pytest.mark.parametrize(
    "override",
    [{"conversation_id": None}],
)
async def test_raises_without_conversation_id(monkeypatch, override):
    _patch_engine(monkeypatch)
    with pytest.raises(scheduler.ProactiveRouteError):
        await scheduler._process_job(FakeConn(), "b1", _job(**override), [])


async def test_raises_when_route_missing(monkeypatch):
    _patch_engine(monkeypatch, route=None)
    with pytest.raises(scheduler.ProactiveRouteError):
        await scheduler._process_job(FakeConn(), "b1", _job(), [])


async def test_raises_when_no_channel_identity(monkeypatch):
    _patch_engine(monkeypatch, to=None)
    with pytest.raises(scheduler.ProactiveRouteError):
        await scheduler._process_job(FakeConn(), "b1", _job(), [])


# --------------------------------------------------------------------------- #
# _process_tenant — un job picat nu rupe lotul; analytics scrise
# --------------------------------------------------------------------------- #


async def test_failing_job_isolated_rest_continue(monkeypatch):
    fake = FakeConn()

    @asynccontextmanager
    async def fake_tenant(business_id):
        yield fake

    jobs = [_job(id="j1"), _job(id="j2"), _job(id="j3")]
    marks: list[tuple] = []
    inserted: list[Event] = []

    async def f_claim(conn, biz, *, limit):
        return jobs

    async def f_mark(conn, biz, jid, status):
        marks.append((jid, status))

    async def f_insert(conn, biz, events):
        inserted.extend(events)
        return len(events)

    async def f_process(conn, biz, job, events):
        if job["id"] == "j2":
            raise RuntimeError("boom")
        events.append(Event("proactive_enqueued", {"kind": job["kind"], "deduped": False}))

    monkeypatch.setattr(scheduler, "tenant_conn", fake_tenant)
    monkeypatch.setattr(scheduler, "claim_due_jobs", f_claim)
    monkeypatch.setattr(scheduler, "mark_job", f_mark)
    monkeypatch.setattr(scheduler, "insert_events", f_insert)
    monkeypatch.setattr(scheduler, "_process_job", f_process)

    handled = await scheduler._process_tenant("b1", batch=20)

    assert handled == 3
    assert ("j2", "failed") in marks  # jobul picat marcat failed
    types = [e.type for e in inserted]
    assert types.count("proactive_enqueued") == 2  # j1 + j3 procesate
    assert types.count("proactive_failed") == 1  # j2


# --------------------------------------------------------------------------- #
# builders — text per kind
# --------------------------------------------------------------------------- #


async def test_build_awb_from_payload():
    spec = await builders.build_message_spec(
        FakeConn(), "b1", _job(payload={"awb": "AWB1", "carrier": "FAN"}), ROUTE
    )
    assert "AWB1" in spec.free_text and "FAN" in spec.free_text
    assert spec.variables == {"awb": "AWB1", "courier": "FAN"}


async def test_build_unknown_kind_raises():
    with pytest.raises(BuildError):
        await builders.build_message_spec(FakeConn(), "b1", _job(kind="custom", payload={}), ROUTE)


async def test_build_awb_without_awb_raises():
    with pytest.raises(BuildError):
        await builders.build_message_spec(FakeConn(), "b1", _job(payload={}), ROUTE)


async def test_build_abandoned_cart_cancel_when_converted(monkeypatch):
    async def f_checkout(conn, biz, conv):
        return {"id": "x", "url": "u", "converted_order_id": "o1", "expired": False}

    monkeypatch.setattr(builders, "get_latest_checkout", f_checkout)
    spec = await builders.build_message_spec(
        FakeConn(), "b1", _job(kind="abandoned_cart", payload={}), ROUTE
    )
    assert spec.cancel is True


async def test_build_follow_up_uses_body():
    spec = await builders.build_message_spec(
        FakeConn(), "b1", _job(kind="follow_up", payload={"body": "  salut  "}), ROUTE
    )
    assert spec.free_text == "salut"


# --------------------------------------------------------------------------- #
# P7 + P5/P12 — verificări în sursă
# --------------------------------------------------------------------------- #


def test_tenant_queries_filter_business_id():
    src = Path("src/db/queries/proactive.py").read_text(encoding="utf-8")
    # query-urile tenant-scoped au business_id = $1 (control-plane-ul e excepția documentată)
    assert src.count("business_id = $1") >= 6
    assert "for update skip locked" in src.lower()


def test_no_direct_channel_send_in_proactive():
    for fname in ("scheduler.py", "builders.py"):
        src = Path(f"src/proactive/{fname}").read_text(encoding="utf-8")
        assert "MetaClient" not in src
        assert "TelegramClient" not in src
        assert "import httpx" not in src
