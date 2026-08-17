"""NX-246 — pornirea observabilității: O SINGURĂ dată, la boot, explicit.

De ce nu lazy: dacă providerul s-ar construi la prima folosire, prima folosire ar fi pe calea
fierbinte a unui request, iar o configurație invalidă ar deveni o eroare în mijlocul unui tur —
adică exact ce evită poarta de boot din `Settings._observability_relations`. Aici doar montăm
ce a fost deja validat.

Ordinea contează și e fixă: config → sink → exporter → provider global. Un exporter instalat
înaintea configurației ar trimite spans cu resource-ul gol; un provider instalat înaintea
exporterului ar arunca primele spans în tăcere.

Sink-ul OTLP se importă LENEȘ, în funcția asta, și numai când configurația o cere. Motivul e
practic: `opentelemetry-sdk` nu e o dependență a căii fierbinți și nu are ce căuta în graful de
import al workerului când exportul e stins (default). Dacă pachetul lipsește dar endpointul e
configurat, refuzăm boot-ul cu un cod de fix — „export activat, bibliotecă absentă" trebuie să
fie o eroare la pornire, nu o telemetrie care nu ajunge nicăieri.
"""

from __future__ import annotations

import logging

from src.observability import config as obs_config
from src.observability import metrics, tracing
from src.observability.config import EXPORTER_CAPTURE, EXPORTER_OTLP, ObservabilityConfig
from src.observability.export import CaptureSink, NullSink, Sink, SpanExporter

log = logging.getLogger(__name__)

_capture: CaptureSink | None = None


def _build_sink(cfg: ObservabilityConfig) -> Sink:
    global _capture
    if not cfg.enabled or not cfg.traces_enabled:
        return NullSink()
    if cfg.exporter == EXPORTER_CAPTURE:
        _capture = CaptureSink()
        return _capture
    if cfg.exporter == EXPORTER_OTLP:
        try:
            from src.observability.otel_sink import OtelSink
        except ImportError as e:  # pachetul OTel lipsește din imagine
            raise RuntimeError(
                "OBSERVABILITY_EXPORTER=otlp dar opentelemetry-sdk lipsește "
                "(fix: instalează `opentelemetry-sdk` + `opentelemetry-exporter-otlp-proto-http`, "
                "sau setează OBSERVABILITY_EXPORTER=none)"
            ) from e
        return OtelSink(cfg)
    return NullSink()


def setup(settings) -> ObservabilityConfig:
    """Montează observabilitatea pentru procesul curent. Idempotentă (re-apelabilă în teste)."""
    cfg = obs_config.from_settings(settings)
    obs_config.configure(cfg)
    exporter = SpanExporter(
        _build_sink(cfg),
        queue_max=cfg.queue_max,
        batch=cfg.export_batch,
        flush_timeout_ms=cfg.flush_timeout_ms,
        on_drop=metrics.record_drop,
    )
    tracing.set_exporter(exporter)
    exporter.start()
    if cfg.enabled:
        log.info(
            "observability: pornit (exporter=%s traces=%s metrics=%s sample=%.3f release=%s/%s)",
            cfg.exporter,
            cfg.traces_enabled,
            cfg.metrics_enabled,
            cfg.sample_ratio,
            cfg.release_track,
            cfg.release_sha,
        )
    return cfg


async def shutdown() -> None:
    """Flush final MĂRGINIT. Un proces care refuză să moară din cauza telemetriei e o pană de
    disponibilitate cauzată chiar de instrumentul care ar trebui să o măsoare."""
    exporter = tracing.get_exporter()
    if exporter is not None:
        await exporter.shutdown()
    tracing.set_exporter(None)


def capture_sink() -> CaptureSink | None:
    """Sink-ul de memorie, când `OBSERVABILITY_EXPORTER=capture` (drive local + teste)."""
    return _capture
