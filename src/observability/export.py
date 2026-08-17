"""NX-246 — ieșirea: coadă MĂRGINITĂ, sink pluggable, drop-uri numărate, flush cu plafon.

Cerința pe care o rezolvă modulul e cea din failure matrix: „OTLP lent/down/429 ⇒ buffer bounded,
timeout/drop contabilizat; turnul continuă". Ea are o consecință de design pe care e ușor să o
ratezi: **calea fierbinte nu are voie să aștepte exportul, deci nu are voie nici să-l APELEZE.**
Un `await export(...)` într-un span, oricât de scurt, leagă latența turului de sănătatea unui
serviciu terț. Aici, `enqueue` e sincron, O(1) și nu ridică niciodată; exportul propriu-zis
trăiește într-un task de fundal pe care nimeni nu-l așteaptă.

A doua decizie e politica de drop. Când coada e plină aruncăm **cel mai NOU**, nu cel mai vechi:
într-un incident, ce explică incidentul e ÎNCEPUTUL rafalei, nu coada ei. Păstrând prefixul,
rămâi cu o poveste coerentă plus un contor care spune cât ai pierdut; păstrând sufixul, rămâi cu
mii de spans identice de la capătul cozii și fără cauză. Contorul
(`web_observability_dropped_total`)
e obligatoriu în ambele cazuri: tăcerea unei cozi pline arată exact ca sănătatea.

Sink-uri:
  • `NullSink` — implicit, nu face nimic (și nu alocă nimic);
  • `CaptureSink` — în memorie, mărginit: instrumentul cu care testele DOVEDESC că un canary PII
    nu iese din proces. Nu atinge rețeaua, deci poate rula în CI;
  • OTLP — încărcat LENEȘ în `otel_sink.py`, doar când configurația o cere.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SpanRecord:
    """Un span ÎNCHEIAT, gata de export. Date pure — fără referințe la obiecte vii, ca să poată
    aștepta în coadă fără să țină nimic în viață (o conexiune, un context, un request)."""

    name: str
    trace_id: str
    span_id: str
    parent_span_id: str | None
    start_unix_ns: int
    duration_ns: int
    status: str
    attributes: dict[str, str | int | float | bool] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "start_unix_ns": self.start_unix_ns,
            "duration_ns": self.duration_ns,
            "status": self.status,
            "attributes": dict(self.attributes),
        }


class Sink(Protocol):
    """Destinația. Sincronă prin contract: implementările care fac I/O își fac singure bufferul
    intern (SDK-ul OTel are deja unul) — noi nu le așteptăm."""

    def emit_spans(self, spans: list[SpanRecord]) -> None: ...

    def emit_metrics(self, snapshot: dict[str, Any]) -> None: ...

    def shutdown(self, timeout_s: float) -> None: ...


class NullSink:
    """Destinația implicită: nicăieri. Există ca să nu avem `if sink is None` peste tot."""

    def emit_spans(self, spans: list[SpanRecord]) -> None:  # noqa: D102
        return None

    def emit_metrics(self, snapshot: dict[str, Any]) -> None:  # noqa: D102
        return None

    def shutdown(self, timeout_s: float) -> None:  # noqa: D102
        return None


class CaptureSink:
    """Destinație în MEMORIE, mărginită — instrumentul de probă al testelor de privacy.

    Mărginită deliberat: un test care generează 100k spans n-are voie să umple RAM-ul agentului
    de CI. Ce depășește capul se numără în `overflow`, ca testul să nu creadă că a văzut tot.
    """

    def __init__(self, *, max_spans: int = 10_000) -> None:
        self.spans: list[SpanRecord] = []
        self.metrics: list[dict[str, Any]] = []
        self.overflow = 0
        self._max = max_spans
        self.shutdowns = 0

    def emit_spans(self, spans: list[SpanRecord]) -> None:  # noqa: D102
        room = self._max - len(self.spans)
        if room <= 0:
            self.overflow += len(spans)
            return
        self.spans.extend(spans[:room])
        self.overflow += max(0, len(spans) - room)

    def emit_metrics(self, snapshot: dict[str, Any]) -> None:  # noqa: D102
        self.metrics.append(snapshot)

    def shutdown(self, timeout_s: float) -> None:  # noqa: D102
        self.shutdowns += 1

    # ── ajutoare de test (nu se folosesc în `src/`) ─────────────────────────────────────────
    def names(self) -> list[str]:
        return [s.name for s in self.spans]

    def by_name(self, name: str) -> list[SpanRecord]:
        return [s for s in self.spans if s.name == name]

    def all_text(self) -> str:
        """Tot ce a ieșit, ca UN șir — suprafața pe care testele caută canary-ul PII.

        Un test care verifică doar atributele pe care le cunoaște ar rata exact bug-ul care
        contează: un câmp NOU adăugat de cineva care n-a citit contractul.
        """
        chunks: list[str] = []
        for s in self.spans:
            chunks.append(s.name)
            chunks.append(s.trace_id)
            chunks.append(s.span_id)
            chunks.append(s.status)
            for k, v in s.attributes.items():
                chunks.append(str(k))
                chunks.append(str(v))
        chunks.append(repr(self.metrics))
        return "\n".join(chunks)


@dataclass
class ExportStats:
    """Sănătatea exportului. Se citește din raport și din testele de fault injection."""

    enqueued: int = 0
    dropped_queue_full: int = 0
    exported: int = 0
    export_errors: int = 0
    export_timeouts: int = 0
    flushes: int = 0
    max_depth: int = 0


class SpanExporter:
    """Coada + bucla. Un exemplar per proces (deținut de `tracing`), oprit la shutdown.

    Contractul cu apelantul e minimal și rigid: `enqueue` nu ridică, nu așteaptă și nu alocă
    decât un append. Tot restul (batch, timeout, erori, backpressure) e problema buclei.
    """

    def __init__(
        self,
        sink: Sink,
        *,
        queue_max: int = 2048,
        batch: int = 256,
        flush_timeout_ms: int = 2000,
        on_drop: Callable[[str, str], None] | None = None,
    ) -> None:
        self._sink = sink
        self._queue: list[SpanRecord] = []
        self._queue_max = queue_max
        self._batch = batch
        self._flush_timeout_s = max(0.001, flush_timeout_ms / 1000.0)
        self._on_drop = on_drop
        self._task: asyncio.Task | None = None
        self._wake: asyncio.Event | None = None
        self._stopping = False
        self.stats = ExportStats()

    # ── producător (calea fierbinte) ────────────────────────────────────────────────────────
    def enqueue(self, span: SpanRecord) -> None:
        """Pune un span în coadă. Sincron, O(1), NU ridică. Coadă plină ⇒ drop numărat."""
        if len(self._queue) >= self._queue_max:
            self.stats.dropped_queue_full += 1
            if self._on_drop is not None:
                try:
                    self._on_drop("span", "queue_full")
                except Exception:  # noqa: BLE001 — un contor stricat nu rupe turul
                    pass
            return
        self._queue.append(span)
        self.stats.enqueued += 1
        self.stats.max_depth = max(self.stats.max_depth, len(self._queue))
        if self._wake is not None and not self._wake.is_set():
            self._wake.set()

    @property
    def depth(self) -> int:
        return len(self._queue)

    # ── consumator (fundal) ────────────────────────────────────────────────────────────────
    def start(self) -> None:
        """Pornește bucla de export. Fără loop activ (script sincron) rămâne pe `flush()` manual."""
        if self._task is not None:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return  # fără event loop: exportul e manual (teste, CLI)
        self._wake = asyncio.Event()
        self._stopping = False
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        assert self._wake is not None
        while not self._stopping:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                raise
            self._wake.clear()
            self.flush()

    def flush(self) -> int:
        """Golește coada spre sink, în batch-uri. Întoarce câte spans au plecat.

        Sincronă și tolerantă: un sink care ridică pierde BATCH-ul curent (numărat), nu coada
        întreagă și cu siguranță nu bucla. Un sink care blochează e problema sink-ului — de aceea
        implementările de rețea folosesc bufferul lor intern, non-blocant.
        """
        if not self._queue:
            return 0
        sent = 0
        while self._queue:
            batch = self._queue[: self._batch]
            del self._queue[: len(batch)]
            try:
                self._sink.emit_spans(batch)
                self.stats.exported += len(batch)
                sent += len(batch)
            except TimeoutError:
                self.stats.export_timeouts += 1
                if self._on_drop is not None:
                    self._on_drop("span", "export_timeout")
            except Exception as e:  # noqa: BLE001 — exportul nu are voie să iasă din el însuși
                self.stats.export_errors += 1
                log.warning("observability: export eșuat (%s)", type(e).__name__)
                if self._on_drop is not None:
                    self._on_drop("span", "export_error")
        self.stats.flushes += 1
        return sent

    async def shutdown(self) -> None:
        """Oprire ORDONATĂ, cu plafon: un flush final care nu are voie să întârzie shutdown-ul.

        Dacă sink-ul nu răspunde în `flush_timeout_ms`, renunțăm. Un proces care refuză să moară
        din cauza telemetriei e o pană de disponibilitate cauzată de instrumentul care ar trebui
        să o măsoare.
        """
        self._stopping = True
        if self._wake is not None:
            self._wake.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 — oprire garantată
                pass
            self._task = None
        try:
            await asyncio.wait_for(asyncio.to_thread(self.flush), timeout=self._flush_timeout_s)
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001
            self.stats.export_timeouts += 1
        try:
            self._sink.shutdown(self._flush_timeout_s)
        except Exception:  # noqa: BLE001
            log.warning("observability: shutdown de sink eșuat")
