"""NX-246 — configurația de observabilitate: validată o dată, la boot, fail-closed.

Trei principii, în ordinea în care contează:

1. **Implicit nu iese nimic din proces.** `OBSERVABILITY_ENABLED=false` (default) înseamnă că nici
   măcar nu se acumulează nimic în memorie: `record_*` verifică un singur bool și se întoarce.
   Cardul cere „landează SDK/config cu exporter OFF; verifică zero schimbare funcțională" — modul
   în care dovedești asta e ca ramura activată să nu existe, nu ca ea să fie ieftină.

2. **Config invalidă = eroare la BOOT, nu degradare la runtime.** Un endpoint scris greșit care
   eșuează tăcut la fiecare export produce exact patologia pe care cardul o numește: un dashboard
   verde peste un sistem care nu raportează. Poarta de aici e la fel cu cea a NX-241: combinație
   imposibilă ⇒ procesul nu pornește, cu un cod de fix.

3. **Kill-switch-uri INDEPENDENTE** (cerință explicită de rollout): traces și metrics se sting
   separat, iar exportul de rețea se stinge separat de acumulare. Într-un incident cauzat de
   instrumentare, ordinea corectă e „taie exportul, păstrează măsurarea locală" — dacă cele două
   ar fi același flag, ai pierde exact datele care explică incidentul.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from src.observability.contract import RELEASE_TRACKS

#: Numele exporterului local (in-process, pentru teste/staging) — nu atinge rețeaua niciodată.
EXPORTER_NONE = "none"
EXPORTER_CAPTURE = "capture"
EXPORTER_OTLP = "otlp"
EXPORTERS: frozenset[str] = frozenset({EXPORTER_NONE, EXPORTER_CAPTURE, EXPORTER_OTLP})


class ObservabilityConfigError(RuntimeError):
    """Config imposibilă. Mesajul conține codul de fix — la fel ca poarta de boot NX-241."""


@dataclass(frozen=True, slots=True)
class ObservabilityConfig:
    """Configurația EFECTIVĂ a procesului. Imutabilă: se citește o dată, la boot.

    `enabled=False` e starea de default și e absorbantă — celelalte câmpuri devin irelevante.
    """

    enabled: bool = False
    traces_enabled: bool = True
    metrics_enabled: bool = True
    exporter: str = EXPORTER_NONE
    otlp_endpoint: str = ""
    otlp_headers: tuple[tuple[str, str], ...] = ()
    otlp_timeout_ms: int = 2000
    #: Fracțiunea de ture de SUCCES păstrate în traces. Erorile/deadline-urile/reclaim-urile se
    #: păstrează ÎNTOTDEAUNA (vezi `tracing.should_sample`) — eșantionarea nu are voie să ascundă
    #: exact evenimentele rare pe care le investighezi.
    sample_ratio: float = 0.05
    queue_max: int = 2048
    export_batch: int = 256
    flush_timeout_ms: int = 2000
    service: str = "nativx-assistant"
    env: str = "dev"
    release_sha: str = "unknown"
    release_track: str = "champion"
    trace_secret: str = ""

    @property
    def exports(self) -> bool:
        """Iese ceva din proces? (`capture` NU iese — e memoria testelor.)"""
        return self.enabled and self.exporter == EXPORTER_OTLP

    def resource_attributes(self) -> dict[str, str]:
        """Atributele constante ale procesului — puse pe FIECARE span, o singură dată în cod."""
        return {
            "service": self.service,
            "env": self.env,
            "release_sha": self.release_sha,
            "release_track": self.release_track,
        }


def _parse_headers(raw: str) -> tuple[tuple[str, str], ...]:
    """`k=v,k2=v2` → pereche ordonată. Valorile sunt SECRETE (token OTLP): nu se loghează."""
    out: list[tuple[str, str]] = []
    for chunk in (raw or "").split(","):
        if not chunk.strip():
            continue
        key, sep, value = chunk.partition("=")
        if not sep or not key.strip():
            raise ObservabilityConfigError(
                "OBSERVABILITY_OTLP_HEADERS invalid (fix: format `cheie=valoare,cheie2=valoare2`)"
            )
        out.append((key.strip(), value.strip()))
    return tuple(out)


def from_settings(s: Any) -> ObservabilityConfig:
    """Settings → config validată. Ridică `ObservabilityConfigError` pe orice combinație imposibilă.

    `getattr` cu default peste tot: suita injectează settings parțiale (`SimpleNamespace`) în zeci
    de teste, iar un câmp nou n-are voie să transforme asta în `AttributeError` (aceeași precauție
    ca în `worker/runner._open_runtime`).
    """
    enabled = bool(getattr(s, "observability_enabled", False))
    endpoint = (getattr(s, "observability_otlp_endpoint", "") or "").strip()
    exporter = (getattr(s, "observability_exporter", "") or "").strip() or (
        EXPORTER_OTLP if endpoint else EXPORTER_NONE
    )
    if exporter not in EXPORTERS:
        raise ObservabilityConfigError(
            f"OBSERVABILITY_EXPORTER={exporter!r} necunoscut (fix: unul din {sorted(EXPORTERS)})"
        )
    if exporter == EXPORTER_OTLP:
        if not endpoint:
            raise ObservabilityConfigError(
                "OBSERVABILITY_EXPORTER=otlp fără OBSERVABILITY_OTLP_ENDPOINT "
                "(fix: setează endpointul sau exporter=none)"
            )
        parts = urlsplit(endpoint)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ObservabilityConfigError(
                f"OBSERVABILITY_OTLP_ENDPOINT invalid: {endpoint!r} "
                "(fix: URL absolut http(s)://host:port)"
            )
        if not enabled:
            # Un endpoint configurat cu master switch stins e aproape sigur o greșeală de deploy:
            # cineva crede că exportă. Preferăm să nu pornim decât să raportăm în gol.
            raise ObservabilityConfigError(
                "OBSERVABILITY_OTLP_ENDPOINT setat, dar OBSERVABILITY_ENABLED=false "
                "(fix: pornește flagul sau scoate endpointul)"
            )
    ratio = float(getattr(s, "observability_sample_ratio", 0.05))
    if not 0.0 <= ratio <= 1.0:
        raise ObservabilityConfigError(
            f"OBSERVABILITY_SAMPLE_RATIO={ratio} în afara [0,1] (fix: fracțiune, nu procent)"
        )
    queue_max = int(getattr(s, "observability_queue_max", 2048))
    batch = int(getattr(s, "observability_export_batch", 256))
    if queue_max < 1 or batch < 1 or batch > queue_max:
        raise ObservabilityConfigError(
            f"OBSERVABILITY_QUEUE_MAX={queue_max} / EXPORT_BATCH={batch} imposibile "
            "(fix: 1 <= batch <= queue_max)"
        )
    track = (getattr(s, "release_track", "") or "champion").strip()
    if track not in RELEASE_TRACKS:
        raise ObservabilityConfigError(
            f"RELEASE_TRACK={track!r} necunoscut (fix: unul din {sorted(RELEASE_TRACKS)})"
        )
    return ObservabilityConfig(
        enabled=enabled,
        traces_enabled=bool(getattr(s, "observability_traces_enabled", True)),
        metrics_enabled=bool(getattr(s, "observability_metrics_enabled", True)),
        exporter=exporter,
        otlp_endpoint=endpoint,
        otlp_headers=_parse_headers(getattr(s, "observability_otlp_headers", "") or ""),
        otlp_timeout_ms=int(getattr(s, "observability_otlp_timeout_ms", 2000)),
        sample_ratio=ratio,
        queue_max=queue_max,
        export_batch=batch,
        flush_timeout_ms=int(getattr(s, "observability_flush_timeout_ms", 2000)),
        service=(getattr(s, "service_name", "") or "nativx-assistant").strip(),
        env=(getattr(s, "env", "") or "dev").strip(),
        release_sha=(getattr(s, "release_sha", "") or "unknown").strip()[:40],
        release_track=track,
        trace_secret=getattr(s, "observability_trace_secret", "") or "",
    )


# ── Config de proces (una singură, ca `get_settings`) ───────────────────────────────────────
_config: ObservabilityConfig | None = None


def configure(config: ObservabilityConfig | None) -> None:
    """Setează configurația procesului. Chemată O DATĂ la boot (și de teste, cu `None` la final).

    Nu e `get_settings()`-style lazy: dacă am construi-o la prima folosire, prima folosire ar putea
    fi pe calea fierbinte a unui request, iar o config invalidă ar deveni o eroare de runtime în
    mijlocul unui tur — exact ce evită poarta de boot.
    """
    global _config
    _config = config


def current() -> ObservabilityConfig:
    """Configurația activă. Fără `configure()` (job, script, test) → totul stins."""
    return _config if _config is not None else ObservabilityConfig()
