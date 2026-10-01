"""NX-366 felia A — `turn_input`: cu ce a pornit turul, fără vocea clientului și fără secrete."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace as NS

import pytest

from src.config import get_settings
from src.models import Author, Contact, Direction, Message, Reply, RetrievalResult
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
        state={"displayed_products": [{"id": "p1", "name": "Crema", "price": 90.0}]},
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


def _ctx(verified: str | None = None) -> NS:
    return NS(
        trace={},
        verified_customer_ref=verified,
        retrieval=RetrievalResult(
            products=[{"id": "p1", "price": 90.0, "availability": "in_stock"}]
        ),
        reply=Reply(text="x", products=[{"product_id": "p2", "price": 50.0, "sale_price": 40.0}]),
    )


def test_capture_off_writes_nothing():
    assert get_settings().conversation_trace_enabled is False  # implicitul de cod
    ctx = _ctx()
    turn_capture.record_turn_input(ctx, _snap(), {}, NS(settings={}))
    assert ctx.trace == {}


def test_turn_input_has_snapshot_event_and_env(capture_on):
    ctx = _ctx(verified="cust-123")
    business = NS(settings={"domain_pack": {"b": 1, "a": 2}})
    event = {
        "content_type": "text",
        "action": {"kind": "x"},
        "page_context": {"surface": "pdp"},
        "body": "TEXTUL CLIENTULUI",
    }
    turn_capture.record_turn_input(ctx, _snap(), event, business)
    doc = ctx.trace["turn_input"]
    snap = doc["snapshot"]
    assert snap["state_version"] == 4 and snap["state"]["displayed_products"][0]["id"] == "p1"
    assert [m["direction"] for m in snap["history"]] == ["inbound", "outbound"]
    assert snap["history"][1]["payload"] == {"shown": [{"id": "p1"}]}
    assert snap["contact"]["profile"] == {"skin": "dry"}
    assert doc["event"]["verified"] is True
    assert doc["event"]["action"] == {"kind": "x"}
    assert doc["event"]["page_context"] == {"surface": "pdp"}
    assert set(doc["env"]["catalog"]) == {"p1", "p2"}
    assert doc["env"]["catalog"]["p2"][:2] == [50.0, 40.0]
    assert doc["env"]["pack_sha"] == turn_capture.pack_sha(
        NS(settings={"domain_pack": {"a": 2, "b": 1}})
    )
    raw = json.dumps(doc, ensure_ascii=False, default=str)
    # Vocea clientului nu se dublează aici (replay-ul ia forma SAFE din rândul de trace), numele
    # contactului și identitatea verificată nu intră deloc.
    assert "TEXTUL CLIENTULUI" not in raw
    assert "Ana Pop" not in raw and "cust-123" not in raw


def test_settings_overrides_exclude_secrets_and_non_scalars():
    s = NS(
        model_fields_set={
            "openai_api_key",
            "card_slots",
            "search_semantic_enabled",
            "web_cors_origins",
        },
        openai_api_key="sk-secret",
        card_slots=6,
        search_semantic_enabled=False,
        web_cors_origins=["https://demo"],
    )
    assert turn_capture.settings_overrides(s) == {"card_slots": 6, "search_semantic_enabled": False}


def test_capture_failure_leaves_marker_not_exception(capture_on, monkeypatch):
    def boom(*a, **k):
        raise ValueError("x")

    monkeypatch.setattr(turn_capture, "turn_input", boom)
    ctx = _ctx()
    turn_capture.record_turn_input(ctx, _snap(), {}, NS(settings={}))
    assert ctx.trace["turn_input"] == {"v": turn_capture.FORMAT_VERSION, "error": True}
