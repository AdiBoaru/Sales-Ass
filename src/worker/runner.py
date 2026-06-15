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


async def fallback_stage(ctx: TurnContext, deps: PipelineDeps) -> None:
    """Fallback grațios: dacă niciun stagiu n-a produs reply (rută order/handoff
    neacoperită încă, triaj fără răspuns, sau fără cheie OpenAI), iese o întrebare
    de clarificare — NU tăcere (principiul 6) și NU text de schelet."""
    ctx.set_reply(
        "Hmm, n-am înțeles exact 🙂 Cauți un produs anume, ai o întrebare despre o "
        "comandă, sau altceva?",
        cacheable=False,  # non-răspuns specific contextului → nu se cache-uiește (G5b)
    )


# Pipeline-ul curent: Cache (4) → Triaj (nano) → Agent (mini, route=sales) → fallback.
# Cache-ul (G5b) servește din cache query-uri statice repetate ÎNAINTE de orice LLM.
# Triaj setează reply pt simple/clarify; agentul răspunde pt sales. Gates (G5a) +
# context se adaugă ulterior (cache vine după gates când G5a e în main). Importate jos
# ca să evităm un ciclu (stagiile referă PipelineDeps doar sub TYPE_CHECKING).
from src.worker.stages.agent import agent_stage  # noqa: E402
from src.worker.stages.cache import cache_stage  # noqa: E402
from src.worker.stages.triage import triage_stage  # noqa: E402

DEFAULT_STAGES: list[Stage] = [cache_stage, triage_stage, agent_stage, fallback_stage]
