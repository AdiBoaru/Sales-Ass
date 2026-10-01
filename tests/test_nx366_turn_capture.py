"""NX-366 felia A — `turn_input`: cu ce a pornit turul, fără vocea clientului și fără secrete."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace as NS

import pytest

from src.config import get_settings
from src.models import (
    Author,
    Contact,
    ConversationState,
    Direction,
    Message,
    Reply,
    RetrievalResult,
)
from src.worker import turn_capture
from src.worker.turn_uow import TurnLoadSnapshot


@pytest.fixture
def capture_on(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "conversation_trace_enabled", True)
    monkeypatch.setattr(s, "trace_model_io_enabled", True)
    return s


def _snap() -> TurnLoadSnapshot:
    return TurnLoadSnapshot(
        deduped=False,
        contact=Contact(id="c1", business_id="b1", display_name="Ana Pop", profile={"skin": "dry"}),
        conversation_id="conv1",
        state={
            "displayed_products": [{"id": "p1", "name": "Crema", "price": 90.0}],
            "constraints": {"budget_max": 100},
        },
        state_version=4,
        locale="ro",
        history=[
            Message(
                direction=Direction.INBOUND,
                author=Author.CONTACT,
                body="vreau o crema",
                created_at=datetime(2026, 10, 1, tzinfo=UTC),
                id="m1",
            ),
            Message(
                direction=Direction.OUTBOUND,
                author=Author.BOT,
                body="Uite trei creme.",
                payload={"shown": [{"id": "p1"}]},
            ),
        ],
        summary=None,
        facts=[{"fact_type": "concerns", "fact_value": "dry"}],
        inbound_msg_id="m2",
    )


def _ctx() -> NS:
    return NS(
        trace={},
        retrieval=RetrievalResult(
            products=[{"id": "p1", "price": 90.0, "availability": "in_stock"}]
        ),
        reply=Reply(text="x", products=[{"product_id": "p2", "price": 50.0, "sale_price": 40.0}]),
    )


def _begin(snap, event=None, business=None, verified=False):
    return turn_capture.begin_turn_input(
        snap,
        event or {},
        business or NS(settings={}),
        channel_id="ch1",
        verified=verified,
    )


def _capture(snap, event=None, business=None, verified=False):
    pending = _begin(snap, event, business, verified)
    ctx = _ctx()
    turn_capture.finish_turn_input(ctx, pending)
    return ctx


def test_capture_off_writes_nothing():
    assert get_settings().conversation_trace_enabled is False  # implicitul de cod
    assert _begin(_snap()) is None
    ctx = _ctx()
    turn_capture.finish_turn_input(ctx, None)
    assert ctx.trace == {}


def test_turn_input_has_snapshot_event_and_env(capture_on):
    business = NS(settings={"domain_pack": {"b": 1, "a": 2}})
    event = {
        "content_type": "text",
        "action": {"kind": "x"},
        "page_context": {"surface": "pdp"},
        "body": "TEXTUL CLIENTULUI",
    }
    doc = _capture(_snap(), event, business, verified=True).trace["turn_input"]
    snap = doc["snapshot"]
    assert doc["v"] == turn_capture.FORMAT_VERSION
    assert snap["state_version"] == 4 and snap["state"]["displayed_products"][0]["id"] == "p1"
    assert [m["direction"] for m in snap["history"]] == ["inbound", "outbound"]
    assert snap["history"][1]["payload"] == {"shown": [{"id": "p1"}]}
    assert snap["contact"]["profile"] == {"skin": "dry"}
    assert doc["event"]["verified"] is True and doc["event"]["channel_id"] == "ch1"
    assert doc["event"]["action"] == {"kind": "x"}
    assert doc["event"]["page_context"] == {"surface": "pdp"}
    assert set(doc["env"]["catalog"]) == {"p1", "p2"}
    assert doc["env"]["catalog"]["p2"][:2] == [50.0, 40.0]
    same_pack_other_order = NS(settings={"domain_pack": {"a": 2, "b": 1}})
    assert doc["env"]["pack_sha"] == turn_capture.pack_sha(same_pack_other_order)
    raw = json.dumps(doc, ensure_ascii=False, default=str)
    # Vocea clientului nu se dublează aici (replay-ul ia forma SAFE din rândul de trace), iar
    # numele contactului nu intră deloc.
    assert "TEXTUL CLIENTULUI" not in raw and "Ana Pop" not in raw


def test_settings_profile_is_complete_and_has_no_secrets(capture_on):
    """Recenzia: doar setările date procesului lăsau restul pe `.env`-ul celui care rejoacă."""
    profile = _capture(_snap()).trace["turn_input"]["env"]["settings"]
    assert "card_slots" in profile and "single_brain_enabled" in profile  # și cele implicite
    assert "openai_api_key" not in profile and "database_url" not in profile
    assert "release_assignment_salt" not in profile  # cheie după rost, nu după nume
    assert all(isinstance(v, (bool, int, float, str)) for v in profile.values())


def test_initial_state_is_frozen_before_the_pipeline_mutates_it(capture_on):
    """Recenzia: `ConversationState.from_jsonb` partajează dicturi cu `snap.state`, iar o
    clarificare scrie în `constraints` pe loc. Luat după pipeline, „starea inițială" ar fi conținut
    deja răspunsul turului."""
    snap = _snap()
    pending = _begin(snap)
    state = ConversationState.from_jsonb(snap.state)
    state.constraints["budget_max"] = 999  # ce face un stagiu în timpul turului
    assert snap.state["constraints"]["budget_max"] == 999  # aliasul există
    ctx = _ctx()
    turn_capture.finish_turn_input(ctx, pending)
    assert ctx.trace["turn_input"]["snapshot"]["state"]["constraints"] == {"budget_max": 100}


def test_oversized_input_is_declared_not_cut(capture_on, monkeypatch):
    monkeypatch.setattr(turn_capture, "MAX_INPUT_BYTES", 1_000)
    doc = _capture(_snap()).trace["turn_input"]
    assert doc == {"v": turn_capture.FORMAT_VERSION, "error": "oversize"}


def test_capture_failure_leaves_marker_not_exception(capture_on, monkeypatch):
    def boom(*a, **k):
        raise ValueError("x")

    monkeypatch.setattr(turn_capture, "settings_profile", boom)
    doc = _capture(_snap()).trace["turn_input"]
    assert doc == {"v": turn_capture.FORMAT_VERSION, "error": "capture_failed"}
