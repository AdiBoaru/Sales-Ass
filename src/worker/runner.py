"""Pipeline runner — execută stagiile în ordine fixă, măsoară, oprește la reply.

Contractul (CLAUDE.md): un singur `TurnContext` curge prin stagii; orice stagiu
poate seta `ctx.reply` → early exit. Stagiile NU știu că sunt măsurate — runner-ul
emite evenimentele de observabilitate (principiul 10). Niciun loop de orchestrare,
nicio săritură înapoi (principiul 1).

Pentru G2b există un singur stagiu real (`echo_stage`, determinist, fără LLM) ca
să dovedim fluxul cap-coadă. Stagiile adevărate (gates → free_layers → triaj →
context → agent → validator → sender) se adaugă în ordine în G3+.
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from time import perf_counter

import asyncpg
from redis.asyncio import Redis

from src.agent import response_quality, usage
from src.agent.llm import LLMClient
from src.agent.pricing import savings_for
from src.channels.base import MediaFetcherRegistry
from src.config import get_settings
from src.db.provider import DbProvider, static_db
from src.models import TurnContext, TurnUsage

log = logging.getLogger(__name__)


@dataclass
class PipelineDeps:
    """Resursele pe care le primesc stagiile.

    NX-161 Felia 0B: `db` (provider tenant-scoped) e noul contract — `async with deps.db() as conn`
    ia o conexiune DOAR cât ține operația. `conn` (DEPRECATED, scos la Felia 7) rămâne pentru compat
    cât timp stagiile nu-s migrate. Puntea de compat (`__post_init__`): `PipelineDeps(conn=...)` —
    cele 114 usage-uri din teste — primesc automat un provider static, fără rescriere.
    `llm` poate fi None (fără cheie OpenAI) → stagiile LLM degradează grațios.
    `media` poate fi None (fără canal cu download configurat) → media routing fail-soft (NX-76)."""

    conn: asyncpg.Connection | None = None  # DEPRECATED (Felia 7 îl scoate) — vezi `db`
    redis: Redis | None = None
    llm: LLMClient | None = None
    media: MediaFetcherRegistry | None = None
    db: DbProvider | None = None

    def __post_init__(self) -> None:
        # Puntea de compat: `PipelineDeps(conn=...)` capătă un provider static → `deps.db()`
        # yield-uiește conn-ul injectat, fără checkout nou. `db` explicit are întâietate (nu-l
        # suprascriem). `conn=None` (multe teste) → `db` rămâne None (stagiul nu-l atinge oricum).
        if self.db is None and self.conn is not None:
            self.db = static_db(self.conn)


# Un stagiu: mutează `ctx` pe loc; poate seta ctx.reply pentru early exit.
Stage = Callable[[TurnContext, PipelineDeps], Awaitable[None]]


async def run_pipeline(ctx: TurnContext, deps: PipelineDeps, stages: list[Stage]) -> None:
    """Rulează stagiile în ordine. Se oprește la primul care setează `reply`.

    Măsoară latența fiecărui stagiu și o pune în `ctx.events` (persistarea în
    analytics_events e treaba unui pas ulterior — observabilitatea nu blochează
    turul). Tot aici se acumulează usage-ul LLM al turului (tokeni + cached + cost),
    defalcat pe STAGIU și pe MODEL, și se emite UN event `llm_usage` la final —
    stagiile nu știu că sunt măsurate (principiul 10); adaptorul raportează, runner-ul
    agregă. `ctx.usage` (TurnUsage) e pus la dispoziția processor-ului (cost/mesaj)."""
    acc, token = usage.push()
    turn_started = perf_counter()
    by_stage: dict[str, dict] = {}
    stage_latencies: dict[str, float] = {}  # P0-budget: latența TUTUROR stagiilor (și non-LLM)
    try:
        for stage in stages:
            name = getattr(stage, "__name__", "stage")
            before = acc.snapshot()
            started = perf_counter()
            await stage(ctx, deps)
            latency_ms = round((perf_counter() - started) * 1000, 1)
            ctx.emit("stage_completed", stage=name, latency_ms=latency_ms)
            stage_latencies[name] = stage_latencies.get(name, 0.0) + latency_ms
            _record_stage_delta(by_stage, name, before, acc.snapshot(), latency_ms)
            # early-exit pe reply (răspuns) SAU halt (tăcere intenționată — Gates).
            if ctx.reply is not None or ctx.halt:
                ctx.emit("pipeline_early_exit", stage=name)
                if ctx.reply is not None:  # halt (tăcere) n-are reply de măsurat
                    _emit_response_shape(ctx, name)
                break
        else:
            ctx.emit("pipeline_complete")
    finally:
        usage.pop(token)
        latency_ms = round((perf_counter() - turn_started) * 1000, 1)
        savings = (
            sum(savings_for(model, row["cached_tokens"]) for model, row in acc.by_model.items())
            if acc.calls
            else 0.0
        )
        # CONV-COMMERCE: ORICE tur primește `TurnUsage` (nu doar cele cu LLM) → fiecare mesaj
        # outbound salvează timp + tokeni + cost în DB (tokeni/cost = 0 când n-a fost apel LLM:
        # cache/free-layer/welcome). Latența e wall-clock-ul real al turului.
        ctx.usage = TurnUsage(
            tokens_in=acc.tokens_in,
            tokens_out=acc.tokens_out,
            cached_tokens=acc.cached_tokens,
            cost_usd=acc.cost_usd,
            calls=acc.calls,
            savings_usd=savings,
            latency_ms=latency_ms,
            models=sorted(acc.by_model),
            by_stage=by_stage,
            by_model=acc.by_model,
        )
        if acc.calls:
            # Event `llm_usage` DOAR la apel LLM real → rollup/billing fără zero-rows. tokens/cost
            # pe coloane dedicate (insert_events); cached/savings/defalcări în properties (P10).
            ctx.emit("llm_usage", phase="turn", **ctx.usage.as_event_props())
        # P0-budget: alertă per-tur (latență end-to-end SAU cost LLM peste buget) — pt ORICE tur
        # (inclusiv cache/free-layer), nu doar cele cu LLM. Observabilitate, nu schimbă turul (P6).
        _emit_turn_budget(ctx, latency_ms, acc.cost_usd, stage_latencies)


def _record_stage_delta(
    by_stage: dict[str, dict],
    name: str,
    before: tuple[int, int, int, int, float],
    after: tuple[int, int, int, int, float],
    latency_ms: float,
) -> None:
    """Diff-ul acumulatorului în jurul unui stagiu → cât a consumat stagiul ăsta (defalcare
    pe stagiu, NX-103). Doar stagiile care chiar au apelat LLM-ul apar (delta de calls > 0).
    Un stagiu apelat de mai multe ori (n-ar trebui în pipeline-ul liniar) s-ar aduna."""
    d_calls = after[0] - before[0]
    if d_calls <= 0:
        return
    row = by_stage.setdefault(
        name, {"calls": 0, "tokens_in": 0, "tokens_out": 0, "cached_tokens": 0, "cost_usd": 0.0}
    )
    row["calls"] += d_calls
    row["tokens_in"] += after[1] - before[1]
    row["tokens_out"] += after[2] - before[2]
    row["cached_tokens"] += after[3] - before[3]
    row["cost_usd"] += round(after[4] - before[4], 6)
    row["latency_ms"] = row.get("latency_ms", 0.0) + latency_ms


def _emit_turn_budget(
    ctx: TurnContext, latency_ms: float, cost_usd: float, stage_latencies: dict[str, float]
) -> None:
    """CONV-COMMERCE P0: emite `turn_over_budget` când turul depășește bugetul de latență
    (wall-clock end-to-end) SAU cost (LLM). Pur observabilitate (P10): runner-ul măsoară, stagiile
    nu știu. NU schimbă turul (P6) — doar alertează + loghează, cu stagiul cel mai lent (din TOATE
    stagiile, inclusiv non-LLM, ex. retrieval/Vision). Gated de `turn_budget_alerts_enabled`."""
    s = get_settings()
    if not s.turn_budget_alerts_enabled:
        return
    over_latency = latency_ms > s.turn_latency_budget_ms
    over_cost = cost_usd > s.turn_cost_budget_usd
    if not (over_latency or over_cost):
        return
    slow_name, slow_ms = max(stage_latencies.items(), key=lambda kv: kv[1], default=(None, 0.0))
    ctx.emit(
        "turn_over_budget",
        latency_ms=round(latency_ms),
        cost_usd=round(cost_usd, 6),
        over_latency=over_latency,
        over_cost=over_cost,
        budget_latency_ms=s.turn_latency_budget_ms,
        budget_cost_usd=s.turn_cost_budget_usd,
        slowest_stage=slow_name,
        slowest_stage_ms=round(slow_ms),
    )
    log.warning(
        "tur peste buget: %dms (buget %dms), cost $%.4f (buget $%.4f), stagiu lent=%s (%dms)",
        round(latency_ms),
        s.turn_latency_budget_ms,
        cost_usd,
        s.turn_cost_budget_usd,
        slow_name,
        round(slow_ms),
    )


def _emit_response_shape(ctx: TurnContext, stage: str) -> None:
    """NX-159 felia 1: telemetrie de CALITATE a formei răspunsului, GLOBAL post-reply. Emis din
    runner (P10: runner măsoară, stagiile nu știu) pe TOATE căile cu reply. `response_shape` = forma
    (lungimi/booleeni/rută/stagiu, ZERO text/PII, P12); `completeness_gap` doar când lipsește ceva
    (sales/order/clarify). Pur observabilitate, NON-blocant: o excepție NU pică turul (P6)."""
    if not get_settings().response_telemetry_enabled:
        return
    try:
        ctx.emit("response_shape", **response_quality.reply_shape(ctx, stage))
        gaps = response_quality.completeness_gaps(ctx)
        if gaps:
            intent = ctx.route.route.value if ctx.route and ctx.route.route else None
            ctx.emit("completeness_gap", intent=intent, missing=gaps)
    except Exception as e:  # noqa: BLE001 — telemetria nu blochează livrarea
        log.warning("runner: response_shape a eșuat (%s)", type(e).__name__)


async def fallback_stage(ctx: TurnContext, deps: PipelineDeps) -> None:
    """Fallback grațios: dacă niciun stagiu n-a produs reply (rută order/handoff
    neacoperită încă, triaj fără răspuns, sau fără cheie OpenAI), iese o întrebare
    de clarificare — NU tăcere (principiul 6) și NU text de schelet."""
    ctx.set_reply(
        "Hmm, n-am înțeles exact 🙂 Cauți un produs anume, ai o întrebare despre o "
        "comandă, sau altceva?",
        cacheable=False,  # non-răspuns specific contextului → nu se cache-uiește (G5b)
    )


# Pipeline-ul: Gates (3) → Limbă (3) → Welcome (4) → Alias (4) → Cache (4) → FAQ (4) →
# Triaj (5) → Agent (7) → fallback.
# Gates (G5a) decide PRIMUL dacă botul răspunde (bot_active/handoff/risc) — poate opri
# cu reply (risc) sau tăcere intenționată (halt). Limbă (G5c) refină ctx.language ÎNAINTE
# de straturile locale-keyed (principiul 11). Welcome întâmpină DETERMINIST un pur salut
# (free layer, fără LLM). Alias (NX-73) face match EXACT pe `intent_aliases` aprobate (index
# B-tree, zero token) ÎNAINTE de cache — cel mai ieftin strat. Cache (G5b) servește query-uri
# statice repetate fără LLM; FAQ (NX-74) răspunde la întrebări de cunoștințe din `faqs` (un
# embed, fără generare). Triaj setează reply pt simple/clarify; agentul răspunde pt sales.
# Importate jos ca să evităm un ciclu (stagiile referă PipelineDeps sub TYPE_CHECKING).
from src.worker.stages.agent import agent_stage  # noqa: E402
from src.worker.stages.alias import alias_stage  # noqa: E402
from src.worker.stages.cache import cache_stage  # noqa: E402
from src.worker.stages.clarify import clarify_resume_stage  # noqa: E402
from src.worker.stages.faq import faq_stage  # noqa: E402
from src.worker.stages.gates import gates_stage  # noqa: E402
from src.worker.stages.greeting import greeting_stage  # noqa: E402
from src.worker.stages.handoff import handoff_stage  # noqa: E402
from src.worker.stages.language import language_stage  # noqa: E402
from src.worker.stages.triage import triage_stage  # noqa: E402

# clarify_resume (NX-130) rulează după `language` și ÎNAINTE de greeting/cache/triage:
# dacă un slot e în așteptare, răspunsul scurt al clientului e consumat determinist
# (rută + constraint), nu tratat ca salut / cache / re-triat de la zero.
# alias (NX-73) e IMEDIAT ÎNAINTE de cache: match exact pe index, mai ieftin și mai sigur decât
# embed-ul semantic din cache. Un hit FAQ early-exit-ează; un hit route/category setează ctx.route,
# iar cache/FAQ/triaj îl respectă (skip dacă ctx.route e setat) → agentul servește.
DEFAULT_STAGES: list[Stage] = [
    gates_stage,
    language_stage,
    clarify_resume_stage,
    greeting_stage,
    alias_stage,
    cache_stage,
    faq_stage,
    triage_stage,
    handoff_stage,  # NX-123: consumă Route.HANDOFF (escaladare) înainte ca agentul să-l ignore
    agent_stage,
    fallback_stage,
]
