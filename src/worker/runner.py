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

from src.agent.llm import LLMClient
from src.models import TurnContext


@dataclass
class PipelineDeps:
    """Resursele pe care le primesc stagiile. Conexiunea e DEJA tenant-scoped.
    `llm` poate fi None (fără cheie OpenAI) → stagiile LLM degradează grațios."""

    conn: asyncpg.Connection
    redis: Redis | None = None
    llm: LLMClient | None = None


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
    """Fallback determinist: dacă niciun stagiu LLM n-a produs reply (ex. rută
    sales/order fără agent încă — G4, sau fără cheie OpenAI), tot iese ceva spre
    client (principiul 6: niciodată tăcere). Se restrânge pe măsură ce G4 acoperă
    rutele rămase."""
    body = (ctx.message.body or "").strip()
    if body:
        ctx.set_reply(f"Am primit mesajul tău: „{body}”. Revenim imediat.")
    else:
        ctx.set_reply("Am primit mesajul tău. Revenim imediat.")


# Pipeline-ul curent: Triaj (nano) → Agent (mini, doar route=sales) → echo fallback.
# Triaj setează reply pt simple/clarify (early exit). Agentul răspunde pt sales.
# Restul (gates, context, free layers) se adaugă în ordine ulterior. Importate jos
# ca să evităm un ciclu (stagiile referă PipelineDeps doar sub TYPE_CHECKING).
from src.worker.stages.agent import agent_stage  # noqa: E402
from src.worker.stages.triage import triage_stage  # noqa: E402

DEFAULT_STAGES: list[Stage] = [triage_stage, agent_stage, echo_stage]
