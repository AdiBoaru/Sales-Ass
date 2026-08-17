"""NX-241 — unde s-a dus timpul într-un tur: spans pe FAZE, contoare, bucket-uri.

Runner-ul măsoară, stagiile nu știu (P10) — la fel ca `llm_usage` (NX-103) și `db_ops` (NX-231).
Diferența față de `stage_completed` (care există deja): un stagiu NU e o fază. `agent_stage`
conține model + tools + validare + projection; ca să poți spune „p90 a crescut din cauza
retrievalului", ai nevoie de defalcarea pe faze, aceeași pe toate căile (fast path, creier unic,
free layers). Vocabularul de faze e ÎNCHIS (`src/runtime/deadline.PHASES`) — o etichetă de metrică
construită din date de client e o scurgere de cardinalitate, nu observabilitate (P12).

Un singur eveniment per tur (`turn_latency`), ca `llm_usage`: N evenimente per fază ar înmulți
volumul de analytics fix pe calea fierbinte pe care încercăm să o facem mai rapidă.
"""

from __future__ import annotations

import contextvars
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any

from src.runtime.deadline import PHASES

#: Rezultatul unei faze — vocabular ÎNCHIS (etichetă de metrică).
OUTCOME_OK = "ok"
OUTCOME_DEGRADED = "degraded"  # a răspuns, dar mai slab (fallback lexical, rerank sărit)
OUTCOME_TIMEOUT = "timeout"
OUTCOME_ERROR = "error"
OUTCOME_SKIPPED = "skipped"  # n-a mai pornit (deadline/buget) — diferit de „a eșuat"

#: Bandă de milisecunde. Histogramele reale (OTel) se derivă din asta; în analytics rămâne banda.
_MS_EDGES: tuple[int, ...] = (100, 300, 800, 1_500, 3_000, 6_000, 10_000, 15_000)


def ms_bucket(ms: float) -> str:
    """Milisecunde → bandă low-cardinality (`web_turn_e2e_ms` are 9 valori posibile, nu 10.000)."""
    prev = 0
    for edge in _MS_EDGES:
        if ms < edge:
            return f"{prev}-{edge}"
        prev = edge
    return f"{_MS_EDGES[-1]}+"


@dataclass
class PhaseStats:
    """Agregat per fază, într-un tur (o fază poate rula de mai multe ori: 2 runde de model)."""

    n: int = 0
    ms: float = 0.0
    outcomes: dict[str, int] = field(default_factory=dict)

    def record(self, ms: float, outcome: str) -> None:
        self.n += 1
        self.ms += ms
        self.outcomes[outcome] = self.outcomes.get(outcome, 0) + 1

    def as_dict(self) -> dict[str, Any]:
        outcomes = dict(sorted(self.outcomes.items()))
        return {"n": self.n, "ms": round(self.ms, 1), "outcomes": outcomes}


@dataclass
class TurnLatencyAccumulator:
    """Acumulatorul unui tur. `unknown_phases` numără (nu numește) etichetele din afara
    vocabularului: un bug de instrumentare trebuie să fie VIZIBIL, nu să umfle metricile."""

    by_phase: dict[str, PhaseStats] = field(default_factory=dict)
    unknown_phases: int = 0
    #: Degradări cu cod fix (retrieval fără embed, rerank sărit, tool truncat) — vocabular închis.
    degradations: dict[str, int] = field(default_factory=dict)
    turn_class: str | None = None
    exhausted: dict[str, str] = field(default_factory=dict)  # fază → motiv (prima epuizare/fază)

    def record(self, phase: str, ms: float, outcome: str = OUTCOME_OK) -> None:
        if phase not in PHASES:
            self.unknown_phases += 1
            return
        self.by_phase.setdefault(phase, PhaseStats()).record(ms, outcome)

    def degrade(self, code: str) -> None:
        self.degradations[code] = self.degradations.get(code, 0) + 1

    def mark_exhausted(self, phase: str, reason: str) -> None:
        """Prima epuizare per fază e cea care contează (restul sunt consecințe)."""
        self.exhausted.setdefault(phase, reason)

    @property
    def total_ms(self) -> float:
        return sum(p.ms for p in self.by_phase.values())

    def as_event_props(self) -> dict[str, Any]:
        props: dict[str, Any] = {
            "phases": {
                k: v.as_dict() for k, v in sorted(self.by_phase.items(), key=lambda kv: kv[0])
            },
            "phase_ms_total": round(self.total_ms, 1),
        }
        if self.turn_class:
            props["turn_class"] = self.turn_class
        if self.degradations:
            props["degradations"] = dict(sorted(self.degradations.items()))
        if self.exhausted:
            props["exhausted"] = dict(sorted(self.exhausted.items()))
        if self.unknown_phases:
            props["unknown_phases"] = self.unknown_phases
        return props


_current: contextvars.ContextVar[TurnLatencyAccumulator | None] = contextvars.ContextVar(
    "turn_latency", default=None
)


def push() -> tuple[TurnLatencyAccumulator, contextvars.Token]:
    acc = TurnLatencyAccumulator()
    return acc, _current.set(acc)


def pop(token: contextvars.Token) -> None:
    _current.reset(token)


def current() -> TurnLatencyAccumulator | None:
    return _current.get()


def record(phase: str, ms: float, outcome: str = OUTCOME_OK) -> None:
    """No-op în afara unui tur instrumentat (job, script, boot) — nu ținem registru global."""
    acc = _current.get()
    if acc is not None:
        acc.record(phase, ms, outcome)


def degrade(code: str) -> None:
    acc = _current.get()
    if acc is not None:
        acc.degrade(code)


def mark_exhausted(phase: str, reason: str) -> None:
    acc = _current.get()
    if acc is not None:
        acc.mark_exhausted(phase, reason)


class _Span:
    """Handle-ul unui span deschis: apelantul poate schimba `outcome` înainte de închidere."""

    __slots__ = ("outcome",)

    def __init__(self) -> None:
        self.outcome = OUTCOME_OK


#: NX-246 — fazele care au un nume de span în taxonomia din `contract.SPAN_NAMES`.
#:
#: Puntea trăiește AICI, nu în apelanți, dintr-un motiv de arhitectură: `turn_latency.span()` e
#: deja seam-ul comun prin care trec model, tools, validare și proiecție (`llm.py`,
#: `tool_executor.py`, `validator.py`, `brain.py`). Bridge-ul de aici le acoperă pe toate cu ZERO
#: import nou în codul de business — exact ce cere cardul („stagiile nu importă exporterul").
#:
#: Fazele fără corespondent nu se mapează: `gates`/`queue`/`load` sunt măsurate de runner/executor
#: la nivelul lor, iar `retrieval` e o singură fază peste trei spans distincte
#: (`lexical`/`embedding`/`fusion`) — a o publica sub unul dintre ele ar fi o etichetă falsă.
_SPAN_BY_PHASE: dict[str, str] = {
    "model": "web.agent.call",
    "tools": "web.tool.call",
    "validation": "web.validate",
    "projection": "web.view_project",
    "commit": "web.result.commit",
    "aftercare": "web.aftercare.schedule",
}


@contextmanager
def span(phase: str, *, outcome: str = OUTCOME_OK):
    """Măsoară o fază. O excepție marchează `error` și se propagă NEATINSĂ — observabilitatea nu
    schimbă fluxul (P6). `with span("model") as s: s.outcome = OUTCOME_DEGRADED` pentru degradări.

        with span("retrieval") as s:
            ...
            s.outcome = OUTCOME_TIMEOUT

    NX-246: dacă faza are un nume de span în taxonomie ȘI există un trace de tur activ, se deschide
    și un span. Fără trace activ (flag stins, job, script) e strict același cod ca înainte.
    """
    handle = _Span()
    handle.outcome = outcome
    started = perf_counter()
    span_name = _SPAN_BY_PHASE.get(phase)
    with ExitStack() as stack:
        if span_name is not None:
            stack.enter_context(_obs_span(span_name, stage=phase))
        try:
            yield handle
        except BaseException:
            record(phase, (perf_counter() - started) * 1000.0, OUTCOME_ERROR)
            raise
        else:
            record(phase, (perf_counter() - started) * 1000.0, handle.outcome)


@contextmanager
def _obs_span(name: str, *, stage: str):
    """Import LOCAL, deliberat: `tracing` cheamă `metrics`, iar `metrics` are o poartă de contract
    la import. Un import de modul aici ar face ca simpla încărcare a NX-241 să depindă de NX-246."""
    from src.observability import tracing

    with tracing.span(name, stage=stage):
        yield
