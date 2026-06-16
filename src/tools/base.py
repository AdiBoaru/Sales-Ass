"""Framework de tool-uri pentru agent (epicul G7).

Contract uniform: `async def tool(ctx, deps, args: dict) -> ToolResult`. Tool-urile sunt
cod DETERMINIST scoped pe `business_id` (din `ctx`, NU din argumentele modelului — izolare).
Modelul (mini) ALEGE ce tool cu ce argumente; bucla de execuție stă în adaptor
(`src.agent.llm.run_tool_loop`). Vezi docs/agent-tools-architecture.md.

`ToolResult.products` = produsele COMPLETE (pt `ctx.retrieval` + validator); `llm_view` = ce
vede modelul (COMPACT, ≤6×8, fără PII — principiul 8).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.models import TurnContext
    from src.worker.runner import PipelineDeps

log = logging.getLogger(__name__)


@dataclass
class ToolResult:
    ok: bool
    products: list[dict[str, Any]] = field(default_factory=list)  # complete → validator
    llm_view: str = ""  # compact → model
    error: str | None = None


ToolFn = Callable[["TurnContext", "PipelineDeps", dict[str, Any]], Awaitable[ToolResult]]

TOOL_REGISTRY: dict[str, ToolFn] = {}


def register(name: str) -> Callable[[ToolFn], ToolFn]:
    """Înregistrează o implementare de tool sub `name`."""

    def deco(fn: ToolFn) -> ToolFn:
        TOOL_REGISTRY[name] = fn
        return fn

    return deco


# Faza 1: read core. Activarea per-business (settings) = ulterior.
_PHASE1 = ("search_products", "get_product_details", "compare_products")


def enabled_tools(business: Any) -> list[str]:  # noqa: ARG001 — per-business vine ulterior
    """Numele tool-urilor active pentru un business (Faza 1: cele 3 read, dacă-s înregistrate)."""
    return [name for name in _PHASE1 if name in TOOL_REGISTRY]


async def run_tool(
    ctx: TurnContext, deps: PipelineDeps, name: str, args: dict[str, Any]
) -> ToolResult:
    """Dispatch + protecție: un tool inexistent sau care aruncă → `ToolResult(ok=False)`,
    NU rupe turul (principiul 6). `business_id` se ia din `ctx` în fiecare tool."""
    fn = TOOL_REGISTRY.get(name)
    if fn is None:
        return ToolResult(ok=False, error=f"tool necunoscut: {name}", llm_view="Tool inexistent.")
    try:
        return await fn(ctx, deps, args or {})
    except Exception as e:  # noqa: BLE001 — tool eșuat (DB/validare) → degradare grațioasă
        log.warning("tool %s a eșuat (%s)", name, type(e).__name__)
        return ToolResult(ok=False, error=type(e).__name__, llm_view="Unealta a eșuat.")
