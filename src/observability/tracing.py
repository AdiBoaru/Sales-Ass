"""NX-246 — traces: un turn = un trace, care supraviețuiește restartului FĂRĂ nicio migrare.

Problema centrală a unui trace pe o cale asincronă e continuitatea: requestul acceptă și pleacă,
executorul reia turul minute mai târziu, poate în alt proces, poate după un reclaim. Soluția
clasică e să persiști `traceparent`-ul în ledger — adică o coloană nouă, o migrare, și o a doua
sursă de adevăr care poate rămâne în urmă.

Noi nu persistăm nimic. `web_turns.id` E un UUID, adică **exact 128 de biți, exact dimensiunea
unui trace-id W3C**. Derivăm determinist:

    trace_id = HMAC-SHA256(secret, "nx246.trace:" + turn_id)[:16 octeți]

Consecințele sunt tocmai invariantele cerute de card:
  • orice proces, oricând, recalculează ACELAȘI trace pentru același turn — continuitate prin
    construcție, nu prin propagare (nimic de pierdut, nimic de sincronizat, zero DDL);
  • un reclaim e un span de attempt NOU (span-id derivat din `turn_id:attempt`) în ACELAȘI trace;
  • clientul NU poate deriva trace-ul din `turn_id`-ul pe care îl cunoaște — secretul e
    server-owned. Corelarea de suport rămâne pe ID-urile publice NX-228, cum cere cardul.

**Contextul de trace venit din browser se REFUZĂ.** Un `traceparent` public nesemnat care ar
deveni părinte ar lăsa pe oricine să lipească spans în traceul altui tenant, sau să umfle un trace
până devine inutilizabil. Îl numărăm (`web_observability_dropped_total{trace_context,
untrusted_inbound}`) și mergem mai departe cu rădăcina noastră.

**Eșantionare pe COADĂ, nu pe cap.** Spans-urile unui tur se acumulează într-un buffer mărginit;
la închiderea rădăcinii decidem: dacă traceul a fost eșantionat SAU dacă vreun span a eșuat, iese
tot; altfel nu iese nimic. Așa nu plătești pentru traficul sănătos, dar ai traceul ÎNTREG exact
pentru turele care au mers prost — care sunt singurele pe care le deschide cineva.
"""

from __future__ import annotations

import contextvars
import hashlib
import hmac
import logging
import re
import secrets
from contextlib import contextmanager
from dataclasses import dataclass, field
from time import perf_counter_ns, time_ns
from typing import Any

from src.observability import config as obs_config
from src.observability import metrics
from src.observability.contract import (
    SPAN_ATTRIBUTES,
    SPAN_NAMES,
    STATUS_ERROR,
    STATUS_OK,
    STATUS_UNSET,
    ContractViolation,
)
from src.observability.export import SpanExporter, SpanRecord
from src.observability.sanitize import exception_chain, safe_attribute_value, safe_error_code

log = logging.getLogger(__name__)

#: Separare de domeniu: același secret folosit altundeva nu poate produce aceleași ID-uri.
_TRACE_DOMAIN = b"nx246.trace:"
_SPAN_DOMAIN = b"nx246.span:"

#: `00-<trace 32hex>-<span 16hex>-<flags 2hex>` (W3C Trace Context, versiunea 00).
_TRACEPARENT = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")
_ZERO_TRACE = "0" * 32
_ZERO_SPAN = "0" * 16

#: Câte spans ținem în buffer pentru decizia de coadă. Peste cap degradăm la decizia de CAP
#: (numărat): un tur cu 500 de spans e oricum un bug de instrumentare, nu un tur.
MAX_BUFFERED_SPANS = 64


def _digest(domain: bytes, value: str, secret: str) -> bytes:
    key = secret.encode() if secret else domain
    return hmac.new(key, domain + value.encode(), hashlib.sha256).digest()


def trace_id_for(turn_id: str, secret: str = "") -> str:
    """`turn_id` → trace-id W3C (32 hex), determinist și ne-derivabil de client cu secret setat.

    Fără secret (dev/test) rămâne determinist, dar derivabil din `turn_id`. E o pierdere de
    confidențialitate a corelării, nu de izolare: un trace-id nu dă acces la nimic.
    """
    tid = _digest(_TRACE_DOMAIN, turn_id, secret)[:16].hex()
    return tid if tid != _ZERO_TRACE else "f" * 32  # trace-id all-zero e invalid în W3C


def span_id_for(turn_id: str, attempt: int, secret: str = "") -> str:
    """Span-id determinist pentru rădăcina unui ATTEMPT: reclaim-ul e alt span, același trace."""
    sid = _digest(_SPAN_DOMAIN, f"{turn_id}:{attempt}", secret)[:8].hex()
    return sid if sid != _ZERO_SPAN else "f" * 16


def format_traceparent(trace_id: str, span_id: str, *, sampled: bool) -> str:
    return f"00-{trace_id}-{span_id}-{'01' if sampled else '00'}"


def parse_traceparent(value: str | None) -> tuple[str, str, bool] | None:
    """Parsează un `traceparent`. `None` = invalid. NU îl face părinte — vezi `reject_inbound`."""
    if not value:
        return None
    m = _TRACEPARENT.match(value.strip().lower())
    if not m:
        return None
    trace_id, span_id, flags = m.groups()
    if trace_id == _ZERO_TRACE or span_id == _ZERO_SPAN:
        return None
    return trace_id, span_id, bool(int(flags, 16) & 0x01)


def reject_inbound_traceparent(value: str | None) -> None:
    """Contextul public nu devine niciodată părinte. Îl numărăm ca să știm dacă cineva încearcă.

    Fără metrică asta ar fi o decizie invizibilă: n-ai ști niciodată dacă un client trimite
    traceparent (integrare legitimă de făcut cândva, cu semnătură) sau dacă cineva sondează.
    """
    if value:
        metrics.record_drop("trace_context", "untrusted_inbound")


@dataclass
class _Span:
    name: str
    trace_id: str
    span_id: str
    parent_span_id: str | None
    start_unix_ns: int
    start_perf_ns: int
    attributes: dict[str, str | int | float | bool] = field(default_factory=dict)
    status: str = STATUS_UNSET

    def set_attribute(self, key: str, value: Any) -> None:
        """Atribut ALLOWLISTAT pe CHEIE **și** pe VALOARE.

        Cheia singură n-ar fi de ajuns: `tool_name` e un atribut legitim, dar
        `tool_name="search_products?q=maria@example.ro"` e o scurgere de PII cu nume aprobat.
        Ce nu trece se aruncă și se numără — un câmp nou adăugat de cineva grăbit nu are voie să
        devină o scurgere tăcută.
        """
        if key not in SPAN_ATTRIBUTES:
            if metrics.is_strict():
                raise ContractViolation(f"atribut de span nedeclarat: {key!r}")
            metrics.record_drop("attribute", "not_allowlisted")
            return
        if value is None:
            return
        safe = safe_attribute_value(key, value)
        if safe is None:
            if metrics.is_strict():
                raise ContractViolation(f"valoare inacceptabilă pe atributul {key!r}: {value!r}")
            metrics.record_drop("attribute", "invalid_value")
            return
        self.attributes[key] = safe

    def record_exception(self, exc: BaseException) -> None:
        """Excepția ca TIPURI, prin sanitizer. Mesajul nu atinge niciodată spanul."""
        self.status = STATUS_ERROR
        self.set_attribute("exception_type", safe_error_code(exc))
        self.set_attribute("exception_chain", exception_chain(exc))
        self.set_attribute("safe_error_code", safe_error_code(exc))


@dataclass
class TraceContext:
    """Traceul UNUI tur: buffer mărginit + decizia de eșantionare pe coadă."""

    trace_id: str
    sampled: bool
    root_span_id: str
    buffer: list[SpanRecord] = field(default_factory=list)
    had_error: bool = False
    overflowed: bool = False
    resource: dict[str, str] = field(default_factory=dict)


_current_trace: contextvars.ContextVar[TraceContext | None] = contextvars.ContextVar(
    "obs_trace", default=None
)
#: Spanul deschis curent — obiectul, nu doar id-ul: adaptoarele (`set_attribute`) trebuie să poată
#: adăuga atribute fără să primească un handle prin toate semnăturile de pe drum.
_current_span: contextvars.ContextVar["_Span | None"] = contextvars.ContextVar(
    "obs_span", default=None
)

_exporter: SpanExporter | None = None


def set_exporter(exporter: SpanExporter | None) -> None:
    """Instalează exporterul procesului (boot / teste). `None` = spans-urile se aruncă."""
    global _exporter
    _exporter = exporter


def get_exporter() -> SpanExporter | None:
    return _exporter


def current_trace() -> TraceContext | None:
    return _current_trace.get()


def _enabled() -> bool:
    cfg = obs_config.current()
    return cfg.enabled and cfg.traces_enabled


def should_sample(trace_id: str, ratio: float) -> bool:
    """Decizie DETERMINISTĂ pe trace-id: același turn dă același verdict în orice proces.

    Un `random()` per span ar produce traces ciobite (jumătate din spans exportate), care sunt
    mai rele decât niciun trace: arată ca o pierdere de date și te trimit să cauți un bug care
    nu există.
    """
    if ratio >= 1.0:
        return True
    if ratio <= 0.0:
        return False
    # Ultimii 8 octeți ai trace-id-ului, ca fracțiune din 2^64.
    bucket = int(trace_id[-16:], 16) / float(1 << 64)
    return bucket < ratio


@contextmanager
def turn_trace(turn_id: str, *, attempt: int = 1, **attributes: Any):
    """Rădăcina traceului unui tur (`web.turn.execute` sau `web.turn.accept`, după apelant).

    No-op complet cu observabilitatea stinsă: nu se alocă nimic, nu se măsoară nimic. Cu ea
    aprinsă, împinge un `TraceContext` peste care toate spans-urile copil se atașează prin
    ContextVar — deci `asyncio.create_task` creat ÎNĂUNTRU moștenește contextul, exact ca la
    `deadline`/`turn_budget` (NX-241).
    """
    if not _enabled():
        yield None
        return
    cfg = obs_config.current()
    trace_id = trace_id_for(turn_id, cfg.trace_secret)
    ctx = TraceContext(
        trace_id=trace_id,
        sampled=should_sample(trace_id, cfg.sample_ratio),
        root_span_id=span_id_for(turn_id, attempt, cfg.trace_secret),
        resource=cfg.resource_attributes(),
    )
    token = _current_trace.set(ctx)
    try:
        with span(
            "web.turn.execute", _span_id=ctx.root_span_id, turn_id=turn_id, **attributes
        ) as sp:
            yield sp
    finally:
        _flush_trace(ctx)
        _current_trace.reset(token)


def _flush_trace(ctx: TraceContext) -> None:
    """Decizia de coadă: exportăm tot traceul dacă a fost eșantionat SAU dacă a existat o eroare."""
    exporter = _exporter
    if exporter is None or not ctx.buffer:
        ctx.buffer.clear()
        return
    if ctx.sampled or ctx.had_error:
        for record in ctx.buffer:
            exporter.enqueue(record)
    else:
        metrics.record_drop("span", "disabled")
    ctx.buffer.clear()


@contextmanager
def span(name: str, *, _span_id: str | None = None, **attributes: Any):
    """Un span copil. Numele trebuie să fie din `SPAN_NAMES` (set închis).

    Excepțiile se propagă NEATINSE — observabilitatea nu schimbă fluxul (P6, aceeași regulă ca
    `turn_latency.span`). Ce se schimbă e că spanul iese marcat `error`, cu tipul, nu cu mesajul.
    """
    if not _enabled():
        yield None
        return
    ctx = _current_trace.get()
    if ctx is None:
        # Span fără trace de tur (job, script, cale neinstrumentată): măsurarea n-are unde să
        # se lege, deci nu inventăm un trace orfan — l-am plăti fără să-l poată citi cineva.
        yield None
        return
    if name not in SPAN_NAMES:
        if metrics.is_strict():
            raise ContractViolation(f"nume de span nedeclarat: {name!r}")
        metrics.record_drop("span", "not_allowlisted")
        yield None
        return
    parent = _current_span.get()
    sp = _Span(
        name=name,
        trace_id=ctx.trace_id,
        span_id=_span_id or secrets.token_hex(8),
        parent_span_id=parent.span_id if parent is not None else None,
        start_unix_ns=time_ns(),
        start_perf_ns=perf_counter_ns(),
        attributes=dict(ctx.resource),
    )
    for key, value in attributes.items():
        sp.set_attribute(key, value)
    span_token = _current_span.set(sp)
    try:
        yield sp
    except BaseException as exc:
        sp.record_exception(exc)
        raise
    finally:
        _current_span.reset(span_token)
        if sp.status == STATUS_UNSET:
            sp.status = STATUS_OK
        if sp.status == STATUS_ERROR:
            ctx.had_error = True
        _buffer(ctx, sp)


def _buffer(ctx: TraceContext, sp: _Span) -> None:
    if len(ctx.buffer) >= MAX_BUFFERED_SPANS:
        if not ctx.overflowed:
            ctx.overflowed = True
            metrics.record_drop("span", "queue_full")
        return
    ctx.buffer.append(
        SpanRecord(
            name=sp.name,
            trace_id=sp.trace_id,
            span_id=sp.span_id,
            parent_span_id=sp.parent_span_id,
            start_unix_ns=sp.start_unix_ns,
            duration_ns=max(0, perf_counter_ns() - sp.start_perf_ns),
            status=sp.status,
            attributes=dict(sp.attributes),
        )
    )


def set_attribute(key: str, value: Any) -> None:
    """Atribut pe spanul CURENT, din cod care nu ține handle-ul (adaptoare, hook-uri).

    Trece prin ACELAȘI allowlist ca `_Span.set_attribute` — nu există o poartă din spate. No-op
    în afara unui span deschis, ca `turn_latency.record` în afara unui tur.
    """
    sp = _current_span.get()
    if sp is not None:
        sp.set_attribute(key, value)


def mark_error(exc: BaseException) -> None:
    """Marchează spanul curent ca eșuat, fără să ridici. Pentru căile care PRIND excepția și
    degradează onest (P6): turul continuă, dar traceul nu are voie să arate verde."""
    sp = _current_span.get()
    if sp is not None:
        sp.record_exception(exc)
