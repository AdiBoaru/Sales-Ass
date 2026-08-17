"""NX-246 — puntea către OTLP. Importat LENEȘ, doar când exportul de rețea e configurat.

Modulul ăsta e deliberat subțire și e singurul loc din `src/` care știe că OpenTelemetry există.
Cardul cere ca stagiile de business să nu importe exporterul și să nu decidă samplingul; regula
se ține cel mai bine dacă vendorul are UN singur punct de contact, la marginea de ieșire.

De ce convertim `SpanRecord` → `ReadableSpan` în loc să folosim tracer-ul SDK direct: spans-urile
noastre sunt deja închise când ajung aici (eșantionare pe coadă — decizia se ia la finalul turului,
vezi `tracing._flush_trace`). Un tracer SDK ar cere să deschidem/închidem spans în momentul real,
adică să renunțăm exact la proprietatea care ne dă traces ÎNTREGI pentru turele eșuate.
"""

from __future__ import annotations

import logging
from typing import Any

from src.observability.config import ObservabilityConfig
from src.observability.contract import STATUS_ERROR, STATUS_OK
from src.observability.export import SpanRecord

log = logging.getLogger(__name__)


class OtelSink:
    """Sink OTLP/HTTP. Bufferul propriu e al SDK-ului (`BatchSpanProcessor`) — noi nu așteptăm."""

    def __init__(self, cfg: ObservabilityConfig) -> None:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource

        self._cfg = cfg
        self._resource = Resource.create(
            {
                "service.name": cfg.service,
                "deployment.environment": cfg.env,
                "service.version": cfg.release_sha,
            }
        )
        self._exporter = OTLPSpanExporter(
            endpoint=cfg.otlp_endpoint,
            headers=dict(cfg.otlp_headers) or None,
            timeout=max(1, cfg.otlp_timeout_ms // 1000),
        )

    def emit_spans(self, spans: list[SpanRecord]) -> None:
        converted = [self._convert(s) for s in spans]
        self._exporter.export([c for c in converted if c is not None])

    def emit_metrics(self, snapshot: dict[str, Any]) -> None:
        # Metricile nu merg pe OTLP în felia asta: contractul lor (nume, unități, bucket-uri) e
        # deja definit în `contract.METRICS`, iar `scripts/slo_report.py` le citește din snapshot.
        # Un pipeline de metrici OTLP e util abia când există un colector — NX-248.
        return None

    def shutdown(self, timeout_s: float) -> None:
        try:
            self._exporter.shutdown()
        except Exception:  # noqa: BLE001 — shutdown-ul nu are voie să blocheze procesul
            log.warning("otel sink: shutdown eșuat")

    def _convert(self, record: SpanRecord):
        """`SpanRecord` → `ReadableSpan`. `None` la orice conversie eșuată (numărată de apelant)."""
        from opentelemetry.sdk.trace import ReadableSpan
        from opentelemetry.sdk.util.instrumentation import InstrumentationScope
        from opentelemetry.trace import SpanContext, TraceFlags
        from opentelemetry.trace.status import Status, StatusCode

        try:
            ctx = SpanContext(
                trace_id=int(record.trace_id, 16),
                span_id=int(record.span_id, 16),
                is_remote=False,
                trace_flags=TraceFlags(TraceFlags.SAMPLED),
            )
            parent = None
            if record.parent_span_id:
                parent = SpanContext(
                    trace_id=int(record.trace_id, 16),
                    span_id=int(record.parent_span_id, 16),
                    is_remote=False,
                    trace_flags=TraceFlags(TraceFlags.SAMPLED),
                )
            status = Status(
                StatusCode.ERROR
                if record.status == STATUS_ERROR
                else (StatusCode.OK if record.status == STATUS_OK else StatusCode.UNSET)
            )
            return ReadableSpan(
                name=record.name,
                context=ctx,
                parent=parent,
                resource=self._resource,
                attributes=dict(record.attributes),
                status=status,
                start_time=record.start_unix_ns,
                end_time=record.start_unix_ns + record.duration_ns,
                instrumentation_scope=InstrumentationScope("nativx.web", "1.0.0"),
            )
        except Exception:  # noqa: BLE001 — un span nevalid nu are voie să pice batch-ul
            log.warning("otel sink: conversie eșuată pentru span %s", record.name)
            return None
