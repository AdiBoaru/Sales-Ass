"""NX-146 felia 1 — teste pentru Turn Replay (asamblare pură, fără DB)."""

from scripts.turn_replay import build_turn_trace, render_table


def _events() -> list[dict]:
    """Evenimentele unui tur sales cu 2 tool calls (ordonate cronologic)."""
    return [
        {"event_type": "stage_completed", "properties": {"stage": "gates", "latency_ms": 3}},
        {"event_type": "stage_completed", "properties": {"stage": "triage", "latency_ms": 40}},
        {
            "event_type": "tool_call",
            "properties": {"tool": "search_products", "n_results": 6, "latency_ms": 22},
        },
        {
            "event_type": "tool_call",
            "properties": {"tool": "get_product_details", "n_results": 1, "latency_ms": 8},
        },
        {
            "event_type": "constraints_merged",
            "properties": {"keys": ["budget_max", "category_key"]},
        },
        {
            "event_type": "agent_prompt",
            "properties": {
                "prompt_hash": "abc123",
                "retrieval_ids": ["p1", "p2"],
                "validator_ok": True,
                "validator_reasons": [],
            },
        },
        {
            "event_type": "llm_usage",
            "properties": {"route": "sales"},
            "tokens_in": 300,
            "tokens_out": 120,
            "cost_usd": 0.0004,
        },
    ]


def test_replay_reconstructs_sales_turn():
    trace = build_turn_trace(
        _events(), turn_id="t-1", inbound="caut o cremă", reply="Îți recomand Aqua la 82.99 lei"
    )

    assert trace["route"] == "sales"
    assert [s["stage"] for s in trace["stages"]] == ["gates", "triage"]
    assert [t["tool"] for t in trace["tools"]] == ["search_products", "get_product_details"]
    assert trace["constraints"] == ["budget_max", "category_key"]
    assert trace["retrieval_ids"] == ["p1", "p2"]
    assert trace["validator"] == {"ok": True, "reasons": []}
    assert trace["usage"] == {"tokens_in": 300, "tokens_out": 120, "cost_usd": 0.0004}
    assert trace["reply"] == "Îți recomand Aqua la 82.99 lei"
    assert trace["failed"] is None


def test_replay_surfaces_turn_failed():
    events = [
        {"event_type": "stage_completed", "properties": {"stage": "agent", "latency_ms": 900}},
        {"event_type": "turn_failed", "properties": {"reason": "tool_loop_timeout"}},
    ]
    trace = build_turn_trace(
        events, turn_id="t-2", inbound="ceva", reply="Îmi pare rău, reîncearcă"
    )

    assert trace["failed"] == {"reason": "tool_loop_timeout"}
    assert trace["route"] is None  # tur oprit devreme, fără rută


def test_replay_redacts_phone_in_inbound_and_reply():
    trace = build_turn_trace(
        [
            {
                "event_type": "tool_call",
                "properties": {"tool": "check_order", "args": {"note": "sună la 0722 123 456"}},
            }
        ],
        turn_id="t-3",
        inbound="comanda mea, telefon +40 722 123 456",
        reply="Te sun la 0722123456",
    )

    assert "0722" not in trace["inbound"]
    assert "722 123 456" not in (trace["inbound"] or "")
    assert "0722123456" not in trace["reply"]
    # redactare defensivă și în args de tool
    assert "0722" not in str(trace["tools"][0]["args"])
    assert "***" in trace["inbound"]


def test_replay_empty_for_unknown_turn():
    trace = build_turn_trace([], turn_id="missing")
    assert trace["n_events"] == 0
    assert trace["route"] is None
    assert trace["stages"] == []
    assert trace["reply"] is None


def test_render_table_is_readable_and_pii_free():
    trace = build_turn_trace(
        _events(), turn_id="t-1", inbound="caut o cremă la 0722 000 111", reply="Aqua 82.99 lei"
    )
    out = render_table(trace)

    assert "Turn Replay: t-1" in out
    assert "search_products" in out
    assert "82.99" in out
    assert "0722" not in out
