"""NX-146 felia 1 — Turn Replay: reconstruiește traiectoria unui tur din `analytics_events`.

Pentru orice `turn_id`, adună evenimentele deja corelate (NX-122: `turn_id` în coloană) și
le asamblează într-un `TurnTrace` lizibil: inbound (redactat) → rută → stagii (cu latență) →
tool calls (args deja whitelisted la emitere) → retrieval IDs → reply (redactat) → rezultatul
validatorului → cost/tokens. Debugging „botul a răspuns aiurea la clientul X, ora Y" în
secunde, nu zeci de minute (condiția #6 de pilot, audit §5.12).

`build_turn_trace` e PUR (fără DB) → testabil pe evenimente seedate. CLI-ul e stratul subțire
care citește din DB (tenant-scoped, P7) și redactează (P12).

Utilizare:
    python scripts/turn_replay.py <turn_id> [--business-id <uuid>] [--json]

Notă: emiterea `agent_prompt` (prompt_hash + retrieval_ids + validator) din `agent_stage` =
felia 2 (atinge `src/worker/stages/agent.py`); replay-ul o afișează când e prezentă, dar nu
depinde de ea.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from src.worker.summarizer import _redact_pii

# business_id-ul clientului demo (fallback comod pt rulări locale; vezi CLAUDE.md).
_DEMO_BUSINESS_ID = "6098812a-50fc-44bd-a1ba-bc77e6399158"


def _first(props: dict, *keys: str) -> Any:
    for k in keys:
        v = props.get(k)
        if v is not None:
            return v
    return None


def _redact_val(value: Any) -> Any:
    """Redactare defensivă recursivă a stringurilor (P12) — evenimentele sunt deja
    PII-safe la emitere, dar nu ne bazăm pe asta în output-ul de suport."""
    if isinstance(value, str):
        return _redact_pii(value)
    if isinstance(value, dict):
        return {k: _redact_val(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_val(v) for v in value]
    return value


def build_turn_trace(
    events: list[dict],
    *,
    turn_id: str,
    inbound: str | None = None,
    reply: str | None = None,
) -> dict[str, Any]:
    """Asamblează `TurnTrace` din evenimentele ordonate cronologic ale unui tur. PUR:
    robust la evenimente lipsă (un tur oprit devreme n-are agent/reply). Toate stringurile
    sunt redactate (P12)."""
    stages: list[dict[str, Any]] = []
    tools: list[dict[str, Any]] = []
    route: str | None = None
    constraints: list[str] | None = None
    retrieval_ids: list[str] = []
    validator: dict[str, Any] | None = None
    failed: dict[str, Any] | None = None
    tokens_in = tokens_out = 0
    cost_usd = 0.0

    for ev in events:
        etype = ev.get("event_type")
        props = ev.get("properties") or {}
        if route is None:
            route = _first(props, "route")

        if etype == "stage_completed":
            stages.append(
                {
                    "stage": _first(props, "stage", "name"),
                    "latency_ms": _first(props, "latency_ms", "duration_ms"),
                }
            )
        elif etype == "tool_call":
            tools.append(
                {
                    "tool": _first(props, "tool", "name"),
                    "n_results": _first(props, "n_results", "n"),
                    "latency_ms": _first(props, "latency_ms", "duration_ms"),
                    "error": _first(props, "error", "error_class"),
                    "args": _redact_val(props.get("args")),
                }
            )
        elif etype == "constraints_merged":
            constraints = _first(props, "keys")
        elif etype == "agent_prompt":  # felia 2 — prezent doar după ce se emite
            retrieval_ids = list(props.get("retrieval_ids") or [])
            if "validator_ok" in props:
                validator = {
                    "ok": props.get("validator_ok"),
                    "reasons": _redact_val(props.get("validator_reasons") or []),
                }
        elif etype == "turn_failed":
            failed = {"reason": _first(props, "reason", "error", "error_class")}

        ti, to, cu = ev.get("tokens_in"), ev.get("tokens_out"), ev.get("cost_usd")
        tokens_in += int(ti) if ti else 0
        tokens_out += int(to) if to else 0
        cost_usd += float(cu) if cu else 0.0

    return {
        "turn_id": turn_id,
        "inbound": _redact_pii(inbound) if inbound else None,
        "route": route,
        "stages": stages,
        "tools": tools,
        "constraints": constraints,
        "retrieval_ids": retrieval_ids,
        "reply": _redact_pii(reply) if reply else None,
        "validator": validator,
        "usage": {"tokens_in": tokens_in, "tokens_out": tokens_out, "cost_usd": round(cost_usd, 6)},
        "failed": failed,
        "n_events": len(events),
    }


def render_table(trace: dict[str, Any]) -> str:
    """Randare lizibilă pentru terminal (nu JSON)."""
    lines = [f"═══ Turn Replay: {trace['turn_id']} ═══"]
    lines.append(f"inbound : {trace['inbound']}")
    lines.append(f"route   : {trace['route']}")
    if trace["constraints"]:
        lines.append(f"constr. : {', '.join(trace['constraints'])}")
    for s in trace["stages"]:
        lat = f" ({s['latency_ms']}ms)" if s["latency_ms"] is not None else ""
        lines.append(f"  stage → {s['stage']}{lat}")
    for t in trace["tools"]:
        bits = [
            b
            for b in (t["tool"], f"n={t['n_results']}" if t["n_results"] is not None else None)
            if b
        ]
        err = f" ERROR={t['error']}" if t["error"] else ""
        lines.append(f"  tool  → {' '.join(bits)}{err}")
    if trace["retrieval_ids"]:
        lines.append(f"retriev.: {', '.join(map(str, trace['retrieval_ids']))}")
    if trace["validator"] is not None:
        lines.append(f"validat.: ok={trace['validator']['ok']} {trace['validator']['reasons']}")
    lines.append(f"reply   : {trace['reply']}")
    u = trace["usage"]
    lines.append(f"usage   : in={u['tokens_in']} out={u['tokens_out']} cost=${u['cost_usd']}")
    if trace["failed"]:
        lines.append(f"FAILED  : {trace['failed']['reason']}")
    return "\n".join(lines)


async def _load(business_id: str, turn_id: str) -> dict[str, Any]:
    from src.db.connection import admin_conn, get_pool
    from src.db.queries.analytics import fetch_turn_events
    from src.db.queries.messages import get_recent_messages

    # `bot_runtime` are DOAR INSERT pe analytics_events (append-only, 003) → replay-ul (tool de
    # SUPORT/ops, nu runtime) citește pe `admin_conn` (pool privilegiat). Izolarea rămâne în cod:
    # fiecare query filtrează explicit `business_id` (P7). Excepție documentată, ca lookup-ul
    # canal→business (channels.py); zero PII în output (redactare la asamblare, P12).
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        events = await fetch_turn_events(conn, business_id, turn_id)
        inbound = reply = None
        conv_id = next((e.get("conversation_id") for e in events if e.get("conversation_id")), None)
        if conv_id:
            msgs = await get_recent_messages(conn, business_id, conv_id)
            # heuristică felia 1: mesajele nu poartă turn_id → cel mai recent inbound/outbound.
            for m in reversed(msgs):
                if inbound is None and m.direction.value == "inbound":
                    inbound = m.body
                if reply is None and m.direction.value == "outbound":
                    reply = m.body
    return build_turn_trace(events, turn_id=turn_id, inbound=inbound, reply=reply)


def main() -> int:
    with __import__("contextlib").suppress(Exception):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="Turn Replay din analytics_events (NX-146)")
    ap.add_argument("turn_id")
    ap.add_argument("--business-id", default=_DEMO_BUSINESS_ID)
    ap.add_argument("--json", action="store_true", help="output JSON în loc de tabel")
    args = ap.parse_args()

    trace = asyncio.run(_load(args.business_id, args.turn_id))
    if trace["n_events"] == 0:
        print(f"niciun eveniment pentru turn_id={args.turn_id!r} (business {args.business_id})")
        return 2
    print(
        json.dumps(trace, ensure_ascii=False, indent=2, default=str)
        if args.json
        else render_table(trace)
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
