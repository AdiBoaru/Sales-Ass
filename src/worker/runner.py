"""Pipeline runner — execută stagiile în ordine fixă, măsoară, oprește la reply.

Contractul (CLAUDE.md): un singur `TurnContext` curge prin stagii; orice stagiu
poate seta `ctx.reply` → early exit. Stagiile NU știu că sunt măsurate — runner-ul
emite evenimentele de observabilitate (principiul 10). Niciun loop de orchestrare,
nicio săritură înapoi (principiul 1).

Pentru G2b există un singur stagiu real (`echo_stage`, determinist, fără LLM) ca
să dovedim fluxul cap-coadă. Stagiile adevărate (gates → free_layers → triaj →
context → agent → validator → sender) se adaugă în ordine în G3+.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from time import perf_counter

import asyncpg
from redis.asyncio import Redis

from src.models import TurnContext


@dataclass
class PipelineDeps:
    """Resursele pe care le primesc stagiile. Conexiunea e DEJA tenant-scoped."""

    conn: asyncpg.Connection
    redis: Redis | None = None


# Un stagiu: mutează `ctx` pe loc; poate seta ctx.reply pentru early exit.
Stage = Callable[[TurnContext, PipelineDeps], Awaitable[None]]


async def run_pipeline(ctx: TurnContext, deps: PipelineDeps, stages: list[Stage]) -> None:
    """Rulează stagiile în ordine. Se oprește la primul care setează `reply`.

    Măsoară latența fiecărui stagiu și o pune în `ctx.events` (persistarea în
    analytics_events e treaba unui pas ulterior — observabilitatea nu blochează
    turul)."""
    for stage in stages:
        name = getattr(stage, "__name__", "stage")
        started = perf_counter()
        await stage(ctx, deps)
        latency_ms = round((perf_counter() - started) * 1000, 1)
        ctx.emit("stage_completed", stage=name, latency_ms=latency_ms)
        if ctx.reply is not None:
            ctx.emit("pipeline_early_exit", stage=name)
            return
    ctx.emit("pipeline_complete")


async def echo_stage(ctx: TurnContext, deps: PipelineDeps) -> None:
    """SCAFFOLD G2b: răspuns determinist care dovedește fluxul inbound→outbox.
    Se înlocuiește cu stagiile reale în G3+ (acesta nu rămâne în producție)."""
    body = (ctx.message.body or "").strip()
    if body:
        ctx.set_reply(f"Am primit mesajul tău: „{body}”. Revenim imediat.")
    else:
        ctx.set_reply("Am primit mesajul tău. Revenim imediat.")


# Setul implicit de stagii pentru G2b (un singur stagiu).
DEFAULT_STAGES: list[Stage] = [echo_stage]
