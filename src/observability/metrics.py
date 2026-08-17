"""NX-246 — metrici cu cardinalitate MĂRGINITĂ prin construcție.

O metrică nu se strică pentru că lipsește, ci pentru că explodează. `web_turn_terminal_total`
cu eticheta `business_id` are atâtea serii câți tenanți; cu `turn_id`, atâtea câte ture — adică
un backend care costă mai mult decât produsul, ținut în viață de o convenție pe care o respectă
toți până când n-o mai respectă cineva. De aceea aici nu există „emite ce vrei":

  • metrica trebuie să existe în `contract.METRICS` (nume DECLARAT);
  • eticheta trebuie să fie una dintre cele declarate pentru acea metrică;
  • valoarea etichetei trebuie să fie fie din setul închis, fie sub bugetul de valori distincte.

Ce cade în afară nu ridică în producție — devine `other` și se numără în
`web_observability_dropped_total{signal,reason}`. Un bug de instrumentare trebuie să fie VIZIBIL
(aceeași alegere ca `unknown_phases` din NX-241), nu să oprească turul (P6) și nici să fie tăcut.

În teste, `reset(strict=True)` transformă aceleași abateri în excepții: la momentul în care scrii
instrumentarea vrei să afli imediat, nu într-un dashboard peste o lună.

Agregarea e în proces (contoare + histograme cu bucket-uri explicite). Nu ținem serii temporale
și nu facem push per-eveniment: `snapshot()` e o citire, la fel ca un scrape Prometheus.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any

from src.observability import config as obs_config
from src.observability.contract import (
    METRICS,
    OTHER,
    ContractViolation,
    MetricSpec,
    assert_contract,
)
from src.observability.sanitize import safe_label_value

log = logging.getLogger(__name__)

# Poarta de coerență a registrului rulează la IMPORT: un registru stricat trebuie să pice la boot,
# nu la primul `record_counter` de pe calea fierbinte (aceeași doctrină ca registrul de tool-uri).
assert_contract()

_DROPPED = "web_observability_dropped_total"

LabelKey = tuple[tuple[str, str], ...]


@dataclass
class Histogram:
    """Histogramă cu bucket-uri EXPLICITE (cumulative la citire, ca în OTel/Prometheus)."""

    buckets: tuple[float, ...]
    counts: list[int] = field(default_factory=list)
    count: int = 0
    total: float = 0.0

    def __post_init__(self) -> None:
        if not self.counts:
            self.counts = [0] * (len(self.buckets) + 1)  # +1 = +Inf

    def observe(self, value: float) -> None:
        self.count += 1
        self.total += value
        for i, edge in enumerate(self.buckets):
            if value <= edge:
                self.counts[i] += 1
                return
        self.counts[-1] += 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "sum": round(self.total, 6),
            "buckets": list(self.buckets),
            "counts": list(self.counts),
        }


class _Registry:
    """Starea de măsurare a procesului. Un lock simplu: workerul e asyncio (un fir), dar
    dispatcher-ul/joburile pot rula în thread-uri, iar un contor pierdut e mai greu de explicat
    decât un lock necontestat."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.counters: dict[tuple[str, LabelKey], float] = {}
        self.histograms: dict[tuple[str, LabelKey], Histogram] = {}
        #: (metrică, cheie de etichetă) → valorile distincte văzute (bugetul de cardinalitate).
        self.distinct: dict[tuple[str, str], set[str]] = {}
        self.strict = False
        self._in_drop = False


_registry = _Registry()


def reset(*, strict: bool = False) -> None:
    """Golește starea (teste). `strict=True` transformă abaterile de contract în excepții."""
    global _registry
    _registry = _Registry()
    _registry.strict = strict


def is_strict() -> bool:
    return _registry.strict


def _enabled() -> bool:
    cfg = obs_config.current()
    return cfg.enabled and cfg.metrics_enabled


def _drop(signal: str, reason: str) -> None:
    """Numără o abatere. Re-entrant-safe: un drop în timp ce numărăm un drop nu buclează."""
    reg = _registry
    if reg._in_drop:
        return
    reg._in_drop = True
    try:
        key = (_DROPPED, (("signal", signal), ("reason", reason)))
        with reg._lock:
            reg.counters[key] = reg.counters.get(key, 0.0) + 1.0
    finally:
        reg._in_drop = False


def _resolve_labels(spec: MetricSpec, labels: dict[str, Any]) -> LabelKey | None:
    """Etichete brute → cheie canonică, sau `None` dacă metrica trebuie respinsă complet.

    O etichetă necunoscută pentru metrică e o eroare de instrumentare (respinge). O VALOARE
    inacceptabilă e o problemă de date (normalizează la `other`) — distincția contează: prima e
    un bug de cod pe care vrei să-l vezi, a doua e realitatea murdară pe care vrei să o măsori.
    """
    declared = {ls.key: ls for ls in spec.labels}
    unknown = set(labels) - set(declared)
    if unknown:
        if _registry.strict:
            raise ContractViolation(
                f"{spec.name}: etichete nedeclarate {sorted(unknown)} "
                f"(declarate: {sorted(declared)})"
            )
        _drop("label", "not_allowlisted")
        return None
    resolved: list[tuple[str, str]] = []
    for key in sorted(declared):
        ls = declared[key]
        raw = labels.get(key)
        # Poartă în DOUĂ etaje (vezi `sanitize.safe_label_value`): forma (cardinalitate) ȘI
        # conținutul (privacy). O cheie API are formă perfectă de identificator.
        value = safe_label_value(raw)
        if value is None:
            if _registry.strict:
                raise ContractViolation(f"{spec.name}.{key}: valoare inacceptabilă {raw!r}")
            _drop("label", "invalid_value")
            value = OTHER
        elif ls.values is not None:
            if value not in ls.values:
                if _registry.strict:
                    raise ContractViolation(
                        f"{spec.name}.{key}={value!r} în afara setului închis ({sorted(ls.values)})"
                    )
                _drop("label", "not_allowlisted")
                value = OTHER
        else:
            seen = _registry.distinct.setdefault((spec.name, key), set())
            if value not in seen:
                if len(seen) >= ls.max_distinct:
                    # Bugetul consumat: de aici încolo totul e `other`. Seriile deja create rămân
                    # (istoricul lor e valid); noutățile se izolează. Un tenant ostil nu poate
                    # umple backendul, iar noi vedem exact în ce metrică s-a atins plafonul.
                    _drop("label", "cardinality_budget")
                    value = OTHER
                else:
                    seen.add(value)
        resolved.append((key, value))
    return tuple(resolved)


def _spec(name: str) -> MetricSpec | None:
    spec = METRICS.get(name)
    if spec is None:
        if _registry.strict:
            raise ContractViolation(f"metrică nedeclarată: {name!r}")
        _drop("metric", "not_allowlisted")
    return spec


def record_counter(name: str, value: float = 1.0, **labels: Any) -> None:
    """Incrementează un counter DECLARAT. Nu ridică în producție (P6)."""
    if not _enabled():
        return
    try:
        spec = _spec(name)
        if spec is None or spec.kind != "counter":
            if spec is not None and _registry.strict:
                raise ContractViolation(f"{name} nu e counter ({spec.kind})")
            return
        key = _resolve_labels(spec, labels)
        if key is None:
            return
        with _registry._lock:
            _registry.counters[(name, key)] = _registry.counters.get((name, key), 0.0) + value
    except ContractViolation:
        raise
    except Exception as e:  # noqa: BLE001 — telemetria nu rupe turul
        log.warning("observability: record_counter(%s) a eșuat (%s)", name, type(e).__name__)


def record_histogram(name: str, value: float, **labels: Any) -> None:
    """Observă o valoare într-o histogramă DECLARATĂ.

    Valorile NEGATIVE se resping și se numără (`invalid_value`): o latență negativă înseamnă
    clock skew, iar dacă ar intra tăcut în percentile, p50-ul ar deveni fals fără nicio urmă.
    Failure matrix: „clock skew/negative latency ⇒ sample invalid + counter; nu intră silențios".
    """
    if not _enabled():
        return
    try:
        spec = _spec(name)
        if spec is None or spec.kind != "histogram":
            if spec is not None and _registry.strict:
                raise ContractViolation(f"{name} nu e histogramă ({spec.kind})")
            return
        if value < 0 or value != value:  # NaN != NaN
            if _registry.strict:
                raise ContractViolation(f"{name}: valoare invalidă {value!r}")
            _drop("metric", "invalid_value")
            return
        key = _resolve_labels(spec, labels)
        if key is None:
            return
        with _registry._lock:
            hist = _registry.histograms.get((name, key))
            if hist is None:
                hist = Histogram(spec.buckets)
                _registry.histograms[(name, key)] = hist
            hist.observe(value)
    except ContractViolation:
        raise
    except Exception as e:  # noqa: BLE001
        log.warning("observability: record_histogram(%s) a eșuat (%s)", name, type(e).__name__)


def record_drop(signal: str, reason: str) -> None:
    """Contorul public de abateri — folosit și de `tracing`/`export` (span drop, context refuzat).

    Nu trece prin `_enabled()`: dacă exportul e stins dar cineva încă produce drop-uri, vrem să
    le vedem la următoarea activare, nu să pierdem tocmai dovada.
    """
    _drop(signal, reason)


def snapshot() -> dict[str, Any]:
    """Fotografia curentă — pentru export, raport și teste. Ordonată: două citiri identice dau
    aceiași bytes (aceeași disciplină ca projectorul pur NX-240)."""
    with _registry._lock:
        counters = {
            f"{name}{_fmt_labels(key)}": round(value, 6)
            for (name, key), value in sorted(_registry.counters.items())
        }
        histograms = {
            f"{name}{_fmt_labels(key)}": hist.as_dict()
            for (name, key), hist in sorted(_registry.histograms.items())
        }
    return {"counters": counters, "histograms": histograms}


def _fmt_labels(key: LabelKey) -> str:
    if not key:
        return ""
    return "{" + ",".join(f"{k}={v}" for k, v in key) + "}"


def counter_value(name: str, **labels: Any) -> float:
    """Valoarea unui counter (teste/raport). 0.0 dacă seria nu există."""
    spec = METRICS.get(name)
    if spec is None:
        return 0.0
    key = tuple(sorted((k, str(v)) for k, v in labels.items()))
    with _registry._lock:
        return _registry.counters.get((name, key), 0.0)


def histogram_for(name: str, **labels: Any) -> Histogram | None:
    key = tuple(sorted((k, str(v)) for k, v in labels.items()))
    with _registry._lock:
        return _registry.histograms.get((name, key))
