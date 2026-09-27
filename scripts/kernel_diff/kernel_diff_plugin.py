"""NX-328 felia 1d — plugin pytest care înregistrează SUPRAFAȚA OBSERVABILĂ a fiecărui tur (I16).

Rulează peste suita golden (`tests/test_golden.py`) a UNEI versiuni de cod, fără să ceară nimic de
la ea: `scripts/kernel_differential.py record` îl încarcă cu `-p`, dintr-un director pus pe
`PYTHONPATH`, iar suita rulează neschimbată. Așa poate înregistra și `main`, pe care pluginul nu
există (problema oului și a găinii din cardul NX-328).

Ce se înregistrează, per tur, exact lista din contract (I16): răspunsul (text + JSON-ul lui),
apelurile de unealtă cu argumente, starea persistată (inclusiv `selected_product` și
`active_search`) și accesele la DB. Excluse: timestamp, latență, telemetrie (`events`),
diagnostic, `KernelTrace`.

Folosește DOAR API-uri care există pe `main` și pe branch: `src.evals.golden.run_pipeline`,
`src.tools.base.run_tool`, `src.agent.tool_executor.run_tool`, `src.worker.runner.PipelineDeps`,
`src.worker.processor._build_new_state`. Dacă unul dintre ele se schimbă, pluginul pică la
configurare cu numele lui, nu înregistrează tăcut altceva."""

from __future__ import annotations

import dataclasses
import enum
import json
import os
from contextvars import ContextVar
from decimal import Decimal
from pathlib import Path
from typing import Any

OUT_ENV = "KERNEL_DIFF_OUT"
#: Doar pentru auto-testul harnessului: `setare=valoare` aplicat peste setări înaintea suitei, ca
#: să dovedească faptul că o schimbare de suprafață CHIAR produce un diff.
FLIP_ENV = "KERNEL_DIFF_FLIP"

#: Chei excluse la orice adâncime: telemetrie, diagnostic, ceas.
EXCLUDED_KEYS: frozenset[str] = frozenset(
    {"events", "diagnostics", "trace", "kernel_trace", "latency_ms", "created_at", "updated_at"}
)

_current_test: ContextVar[str | None] = ContextVar("kernel_diff_test", default=None)
_tool_calls: ContextVar[list | None] = ContextVar("kernel_diff_tools", default=None)
_db_access: ContextVar[list | None] = ContextVar("kernel_diff_db", default=None)
_records: dict[str, list[dict[str, Any]]] = {}


def plain(value: Any) -> Any:
    """Forma canonică, JSON, a oricărei valori de suprafață. Deterministă: mulțimile se sortează."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, enum.Enum):
        return plain(value.value)
    if hasattr(value, "model_dump"):
        return plain(value.model_dump(mode="json"))
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return plain({f.name: getattr(value, f.name) for f in dataclasses.fields(value)})
    if isinstance(value, dict):
        return {
            str(k): plain(v)
            for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))
            if str(k) not in EXCLUDED_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted((plain(v) for v in value), key=lambda v: json.dumps(v, sort_keys=True))
    return repr(value)


class RecordingConn:
    """Stă în locul lui `object()` din `PipelineDeps(conn=object())`: notează numele fiecărui acces
    și apoi se comportă EXACT ca `object()` (ridică `AttributeError`), ca înregistrarea să nu
    schimbe comportamentul pe care îl măsoară."""

    def __getattr__(self, name: str) -> Any:
        log = _db_access.get()
        if log is not None and not name.startswith("__"):
            log.append(name)
        raise AttributeError(name)


def _snapshot(ctx: Any) -> dict[str, Any]:
    from src.worker.processor import _build_new_state  # noqa: PLC0415

    reply = getattr(ctx, "reply", None)
    is_rich = reply is not None and getattr(reply, "rich", None) is not None
    has_products = reply is not None and bool(getattr(reply, "products", None))
    try:
        state = _build_new_state({}, ctx, is_rich=is_rich, has_products=has_products)
    except Exception as exc:  # noqa: BLE001 — un răspuns fără reply e tot o suprafață
        state = {"_error": type(exc).__name__}
    return {
        "reply": plain(reply),
        "tool_calls": plain(list(_tool_calls.get() or [])),
        "state": plain(state),
        "db": list(_db_access.get() or []),
    }


def _install() -> None:
    import src.agent.tool_executor as tool_executor  # noqa: PLC0415
    import src.evals.golden as golden  # noqa: PLC0415
    import src.tools.base as tools_base  # noqa: PLC0415
    from src.worker.runner import PipelineDeps  # noqa: PLC0415

    for owner, name in (
        (golden, "run_pipeline"),
        (tools_base, "run_tool"),
        (tool_executor, "run_tool"),
    ):
        if not hasattr(owner, name):
            raise RuntimeError(f"kernel_diff: API stabil lipsă: {owner.__name__}.{name}")

    original_pipeline = golden.run_pipeline

    async def recording_pipeline(ctx, deps, stages, *args, **kwargs):
        tools_token = _tool_calls.set([])
        db_token = _db_access.set([])
        try:
            result = await original_pipeline(ctx, deps, stages, *args, **kwargs)
            test = _current_test.get() or "<outside-test>"
            _records.setdefault(test, []).append(_snapshot(ctx))
            return result
        finally:
            _tool_calls.reset(tools_token)
            _db_access.reset(db_token)

    original_tool = tools_base.run_tool

    async def recording_tool(ctx, deps, name, args):
        log = _tool_calls.get()
        if log is not None:
            log.append({"name": name, "args": plain(args)})
        return await original_tool(ctx, deps, name, args)

    golden.run_pipeline = recording_pipeline
    tools_base.run_tool = recording_tool
    tool_executor.run_tool = recording_tool

    original_post_init = PipelineDeps.__post_init__

    def recording_post_init(self):
        if type(self.conn) is object:
            self.conn = RecordingConn()
        original_post_init(self)

    PipelineDeps.__post_init__ = recording_post_init


def _apply_flip() -> None:
    flip = os.environ.get(FLIP_ENV)
    if not flip:
        return
    from src.config import get_settings  # noqa: PLC0415

    name, _, raw = flip.partition("=")
    value = {"true": True, "false": False}.get(raw.lower(), raw)
    object.__setattr__(get_settings(), name, value)


def pytest_configure(config) -> None:
    if not os.environ.get(OUT_ENV):
        return
    _install()
    _apply_flip()


def pytest_runtest_setup(item) -> None:
    _current_test.set(item.nodeid)


def pytest_sessionfinish(session, exitstatus) -> None:
    out = os.environ.get(OUT_ENV)
    if not out:
        return
    doc = {test: turns for test, turns in sorted(_records.items())}
    Path(out).write_text(
        json.dumps(doc, indent=1, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8"
    )
