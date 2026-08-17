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
from dataclasses import dataclass, field
from time import perf_counter

import asyncpg
from redis.asyncio import Redis

from src.agent import fallbacks, response_quality, usage
from src.agent.llm import LLMClient
from src.agent.pricing import savings_for
from src.channels.base import MediaFetcherRegistry
from src.config import get_settings
from src.db import op_metrics
from src.db.provider import DbProvider, static_db
from src.models import TurnContext, TurnUsage
from src.observability import hooks, turn_latency
from src.runtime import deadline, turn_budget
from src.runtime.deadline import TurnDeadline
from src.runtime.turn_budget import BudgetLedger, TurnClass
from src.safety import compose as safety_compose

log = logging.getLogger(__name__)


@dataclass
class PipelineDeps:
    """Resursele pe care le primesc stagiile.

    NX-231: `db` (provider tenant-scoped) e SINGURUL contract de acces la DB —
    `async with deps.db("nume_operație") as conn` ia o conexiune DOAR cât ține operația, iar
    între operații (LLM/embed/HTTP) nu se ține nimic. Eticheta alimentează
    `db_checkout_ms{operation}` / `db_hold_ms{operation}`.

    `conn` a rămas EXCLUSIV ca punte pentru teste: cele ~140 de `PipelineDeps(conn=fake)` din
    suită primesc automat un provider static care yield-uiește fake-ul. În `src/` nu-l mai
    citește nimeni (guard mecanic: `scripts/check_no_raw_conn.py --strict`).
    `llm` poate fi None (fără cheie OpenAI) → stagiile LLM degradează grațios.
    `media` poate fi None (fără canal cu download configurat) → media routing fail-soft (NX-76)."""

    conn: asyncpg.Connection | None = None  # TEST-ONLY: puntea spre `db` (vezi __post_init__)
    redis: Redis | None = None
    llm: LLMClient | None = None
    media: MediaFetcherRegistry | None = None
    db: DbProvider | None = None
    # NX-233: hook PUR de observabilitate — runner-ul îl cheamă cu numele stagiului la START.
    # Stagiile nu știu de el (P10); marginea (executorul web) îl mapează pe faza de sârmă
    # (working/validating). Sincron și best-effort: nu are voie să blocheze sau să ridice.
    stage_hook: Callable[[str], None] | None = None

    def __post_init__(self) -> None:
        # Puntea de compat: `PipelineDeps(conn=...)` capătă un provider static → `deps.db()`
        # yield-uiește conn-ul injectat, fără checkout nou. `db` explicit are întâietate.
        #
        # NX-231: puntea se construiește ȘI când `conn` e None. Înainte, `db` rămânea None fiindcă
        # nimeni nu-l apela; acum stagiile apelează `deps.db(...)` întotdeauna, iar testele care
        # construiesc `PipelineDeps(llm=...)` și monkeypatch-uiesc query-ul trebuie să primească
        # exact ce primeau prin `deps.conn`: None. Un provider static peste None face asta;
        # `db=None` ar da `TypeError` pe o cale care înainte mergea.
        if self.db is None:
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
    runtime = _open_runtime(ctx)  # NX-241: deadline + buget + spans (no-op cu flagurile stinse)
    turn_started = perf_counter()
    by_stage: dict[str, dict] = {}
    stage_latencies: dict[str, float] = {}  # P0-budget: latența TUTUROR stagiilor (și non-LLM)
    try:
        for stage in stages:
            name = getattr(stage, "__name__", "stage")
            if _deadline_stop(ctx, runtime, name):
                break
            if deps.stage_hook is not None:
                try:
                    deps.stage_hook(name)  # NX-233: fază de sârmă, derivată — nu blochează
                except Exception:  # noqa: BLE001 — observabilitatea nu rupe turul (P6)
                    log.warning("stage_hook a eșuat pe %s — ignorat", name)
            before = acc.snapshot()
            started = perf_counter()
            await stage(ctx, deps)
            latency_ms = round((perf_counter() - started) * 1000, 1)
            _record_phase(name, latency_ms)
            if name == "gates_stage":
                runtime.gates_done = True  # de aici încolo știm dacă botul are voie să vorbească
            _rebind_turn_class(ctx, runtime, name)
            ctx.emit("stage_completed", stage=name, latency_ms=latency_ms)
            stage_latencies[name] = stage_latencies.get(name, 0.0) + latency_ms
            _record_stage_delta(by_stage, name, before, acc.snapshot(), latency_ms)
            # early-exit pe reply (răspuns) SAU halt (tăcere intenționată — Gates).
            if ctx.reply is not None or ctx.halt:
                # NX-239 (DOAR sub `single_brain_enabled`, altfel byte-identic): un reply care NU
                # e un fast path COMPLET (mesaj mixt, writer LLM concurent) nu încheie turul —
                # control plane-ul îl retrage în semnal pentru MainBrain și pipeline-ul continuă.
                # `halt` (tăcerea Gates) nu se gate-uiește: e decizia de autoritate, nu un răspuns.
                if (
                    ctx.reply is not None
                    and not ctx.halt
                    and getattr(get_settings(), "single_brain_enabled", False)
                    and not control_plane.gate_early_exit(ctx, name).complete
                ):
                    continue
                ctx.emit("pipeline_early_exit", stage=name)
                if ctx.reply is not None:  # halt (tăcere) n-are reply de măsurat
                    safety_compose.enforce(ctx)  # NX-173: vezi mai jos
                    _emit_response_shape(ctx, name)
                break
        else:
            ctx.emit("pipeline_complete")
        # NX-173 (P0) — contractul de siguranță se aplică pe reply-ul FINAL, aici, fiindcă runner-ul
        # e singurul loc prin care trec TOATE căile: early-exit din orice stagiu (inclusiv
        # `try_pre_intents`, care iese înainte de agent) și pipeline-ul complet. Pus în agent_stage
        # ar fi ratat exact căile care au scăpat gate-ului prima dată. Idempotent + no-op fără
        # context de siguranță. (Stagiile nu știu că sunt măsurate — nici că sunt gate-uite, P10.)
        if ctx.reply is not None:
            safety_compose.enforce(ctx)
    finally:
        usage.pop(token)
        latency_ms = round((perf_counter() - turn_started) * 1000, 1)
        _emit_turn_latency(ctx, runtime, latency_ms)
        _close_runtime(runtime)
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


# ── NX-241: deadline + buget + faze ────────────────────────────────────────────────────────
# Un stagiu NU e o fază. `agent_stage` conține model + tools + validare + projection, deci ar
# ascunde exact defalcarea de care avem nevoie; el se măsoară pe DINĂUNTRU (spans în `llm.py`,
# `tool_executor.py`, `validator.py`, `turn_events.py`). Aici mapăm doar stagiile care SUNT o
# singură fază — altfel am dubla timpul (o dată ca stagiu, o dată ca fază internă).
_PHASE_BY_STAGE: dict[str, str] = {
    "gates_stage": "gates",
    "language_stage": "gates",
    "action_kernel_stage": "gates",
    "clarify_resume_stage": "gates",
    "greeting_stage": "gates",
    "alias_stage": "gates",
    "cache_stage": "gates",
    "faq_stage": "gates",
    "handoff_stage": "gates",
    "triage_stage": "model",
}


@dataclass
class TurnRuntime:
    """Ce a deschis runner-ul pentru turul ăsta (și trebuie să închidă la final)."""

    deadline: TurnDeadline | None = None
    ledger: BudgetLedger | None = None
    latency: turn_latency.TurnLatencyAccumulator | None = None
    _tokens: list[tuple[str, object]] = field(default_factory=list)
    owns_deadline: bool = False
    #: A rulat poarta de AUTORITATE (`gates_stage`)? Cât timp nu a rulat, nu știm dacă botul are
    #: voie să vorbească (bot_active / handoff activ) — deci fallback-ul de deadline TACE.
    gates_done: bool = False


def _open_runtime(ctx: TurnContext) -> TurnRuntime:
    """Deschide deadline-ul, bugetul și acumulatorul de faze. Cu flagurile stinse, tot ce rămâne
    e (cel mult) măsurarea — comportamentul turului e byte-identic.

    Deadline-ul poate fi DEJA în context: executorul web (NX-233) îl împinge din `deadline_at`, ca
    așteptarea în coadă să consume același buget. Runner-ul nu îl suprascrie niciodată — ar
    însemna un al doilea proprietar al aceluiași câmp (P3)."""
    # `getattr` peste tot pe câmpurile NOI: suita injectează settings parțiale (SimpleNamespace)
    # în ~zeci de teste de stagiu, iar un câmp nou nu are voie să transforme asta în AttributeError.
    s = get_settings()
    rt = TurnRuntime()
    if getattr(s, "turn_latency_spans_enabled", True):
        rt.latency, tok = turn_latency.push()
        rt._tokens.append(("latency", tok))
    if not getattr(s, "turn_deadline_enabled", False):
        return rt
    rt.deadline = deadline.current()
    if rt.deadline is None:
        # Cale fără executor (worker de canal, sim): tot vrem un plafon TOTAL sigur, nu nelimitat.
        budget = turn_budget.budget_for(TurnClass.RECOMMENDATION, s)
        rt.deadline = TurnDeadline(
            total_ms=min(budget.total_ms, getattr(s, "turn_hard_deadline_ms", 15_000)),
            terminal_reserve_ms=budget.terminal_reserve_ms,
        )
        rt._tokens.append(("deadline", deadline.push(rt.deadline)))
        rt.owns_deadline = True
    rt.ledger = turn_budget.current()
    if rt.ledger is None:
        rt.ledger = BudgetLedger(
            turn_budget.budget_for(TurnClass.RECOMMENDATION, s),
            enforced=getattr(s, "turn_budget_enforced", False),
        )
        rt._tokens.append(("budget", turn_budget.push(rt.ledger)))
    if rt.latency is not None:
        rt.latency.turn_class = rt.ledger.budget.turn_class.value
    return rt


def _close_runtime(rt: TurnRuntime) -> None:
    for kind, tok in reversed(rt._tokens):
        if kind == "latency":
            turn_latency.pop(tok)
        elif kind == "deadline":
            deadline.pop(tok)
        else:
            turn_budget.pop(tok)
    rt._tokens.clear()


def _record_phase(stage_name: str, latency_ms: float) -> None:
    phase = _PHASE_BY_STAGE.get(stage_name)
    if phase is not None:
        turn_latency.record(phase, latency_ms)


def _rebind_turn_class(ctx: TurnContext, rt: TurnRuntime, stage_name: str) -> None:
    """Clasa de tur se știe abia după ce ruta e decisă (triaj / kernel de acțiune). Re-legăm o
    dată, PĂSTRÂND contoarele consumate — un tur nu-și șterge istoria fiindcă s-a reclasificat."""
    if rt.ledger is None or stage_name not in ("triage_stage", "action_kernel_stage"):
        return
    route = ctx.route.route.value if ctx.route and ctx.route.route else None
    turn_class = turn_budget.classify(
        route,
        # `ctx.action` e comanda opacă a clientului (NX-236) — un tur care EXECUTĂ ceva e mutație,
        # oricât de simplu ar arăta textul.
        has_action=getattr(ctx, "action", None) is not None,
        purchase_intent=bool(ctx.route.purchase_intent) if ctx.route else False,
    )
    if turn_class is rt.ledger.budget.turn_class:
        return
    rt.ledger.rebind(turn_budget.budget_for(turn_class, get_settings()))
    if rt.latency is not None:
        rt.latency.turn_class = turn_class.value
    ctx.emit("turn_class_bound", turn_class=turn_class.value, stage=stage_name)


def _deadline_stop(ctx: TurnContext, rt: TurnRuntime, stage_name: str) -> bool:
    """Mai are turul timp pentru stagiul următor? `True` = oprim pipeline-ul ACUM.

    Oprirea nu e tăcere: dacă nu există deja un reply, punem unul determinist — produsele deja
    validate dacă le avem (fapte reale, nu draft de model), altfel un mesaj onest (P6). Rezerva
    terminală există exact ca pasul ăsta + commitul să aibă timp garantat."""
    d = rt.deadline
    if d is None or d.unbounded:
        return False
    phase = _PHASE_BY_STAGE.get(stage_name, "model")
    cp = d.has_room_for(phase, minimum_ms=0)
    if not cp.exhausted:
        return False
    reason = cp.reason or "expired"
    turn_latency.mark_exhausted(phase, reason)
    hooks.on_deadline(phase, reason)  # NX-246: contor operațional; vocabularul e deja închis
    ctx.emit(
        "turn_deadline_exhausted",
        phase=phase,
        reason=reason,
        stage=stage_name,
        elapsed_ms=d.elapsed_ms(),
        budget_ms=d.total_ms,
    )
    log.warning(
        "tur oprit de deadline în %s (%s): %dms din %dms",
        stage_name,
        reason,
        d.elapsed_ms(),
        d.total_ms,
    )
    # Tăcerea rămâne a Gates, nu a deadline-ului. Două situații în care NU punem niciun reply:
    #   • `ctx.halt` — Gates a decis tăcere intenționată (bot oprit / om a preluat conversația);
    #   • gates n-a apucat să ruleze — nu ȘTIM încă dacă botul are voie să vorbească, iar un mesaj
    #     de „n-am apucat" într-o conversație preluată de un operator ar fi mai rău decât nimic.
    # În ambele cazuri turul iese fără reply, iar marginea web îl terminalizează onest cu
    # error-view randabil (`empty_result`/`deadline_exceeded`) — P6 e respectat acolo, nu aici.
    if ctx.reply is None and rt.gates_done and not ctx.halt:
        products = list(ctx.retrieval.products) if ctx.retrieval else []
        ctx.set_reply(
            fallbacks._deadline_msg(ctx.language, partial=bool(products)),
            products=products[:4] or None,
            cacheable=False,  # un răspuns de epuizare nu are ce căuta în cache (otrăvire)
        )
        _emit_response_shape(ctx, stage_name)  # paritate de telemetrie cu early-exit-ul normal
    return True


def _emit_turn_latency(ctx: TurnContext, rt: TurnRuntime, latency_ms: float) -> None:
    """UN event per tur (ca `llm_usage` / `db_ops`): defalcarea pe faze + ce a consumat din buget.
    Pur observabilitate — o excepție aici nu are voie să atingă răspunsul (P6)."""
    if rt.latency is None:
        return
    try:
        props: dict = {
            "e2e_ms": round(latency_ms),
            "e2e_bucket": turn_latency.ms_bucket(latency_ms),
            **rt.latency.as_event_props(),
        }
        # Query-urile se NUMĂRĂ din contabilitatea NX-231 (checkout-uri per operație), nu dintr-un
        # contor paralel: două surse de adevăr pentru „câte query-uri a făcut turul" ar diverge.
        db_acc = op_metrics.current()
        if db_acc is not None:
            props["query_count_bucket"] = turn_budget.count_bucket(db_acc.checkouts)
            if rt.ledger is not None:
                rt.ledger.consume("query_calls", db_acc.checkouts)
        if rt.ledger is not None:
            props.update(rt.ledger.as_event_props())
            props["model_calls_bucket"] = turn_budget.count_bucket(
                int(rt.ledger.spent.get("model_rounds", 0))
            )
            props["tool_calls_bucket"] = turn_budget.count_bucket(
                int(rt.ledger.spent.get("tool_calls", 0))
            )
        if rt.deadline is not None:
            props.update(rt.deadline.as_event_props())
        ctx.emit("turn_latency", **props)
    except Exception as e:  # noqa: BLE001 — telemetria nu blochează livrarea
        log.warning("runner: turn_latency a eșuat (%s)", type(e).__name__)


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
from src.agent import control_plane  # noqa: E402 — NX-239: poarta de fast-path (doar sub flag)
from src.agent.action_kernel import action_kernel_stage  # noqa: E402
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
# action_kernel (NX-236) rulează IMEDIAT după `language` și înaintea tuturor straturilor care
# interpretează TEXT: o acțiune opacă e o decizie deja luată, nu o intenție de ghicit, iar mesajul
# ei e gol prin construcție (eticheta butonului nu e input). Un `Handled` iese cu reply; un
# `Continue` setează `ctx.route`, pe care alias/cache/FAQ/triaj îl respectă (skip). Fără acțiune pe
# tur, stagiul e un no-op — pipeline-ul de text rămâne byte-identic.
DEFAULT_STAGES: list[Stage] = [
    gates_stage,
    language_stage,
    action_kernel_stage,
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
