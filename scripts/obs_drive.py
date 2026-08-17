"""NX-246 — proba reproductibilă a stratului de observabilitate. Zero DB, zero LLM, zero rețea.

    PYTHONPATH=. python scripts/obs_drive.py

Simulează UN turn complet prin API-ul REAL (`tracing` + `hooks`), cu sink de captură în memorie,
și tipărește ce ar fi ieșit din proces. Arată patru lucruri care nu se văd dintr-un test verde:

  1. forma traceului (rădăcină + copii, cu părinți) și faptul că un reclaim rămâne în ACELAȘI trace;
  2. metricile rezultate, cu etichetele lor — se vede pe loc dacă a scăpat cardinalitate;
  3. ce se întâmplă cu un tur EȘUAT sub `sample_ratio=0` (iese oricum: eșantionare pe coadă);
  4. că un canary de PII injectat în tot ce se poate NU apare nicăieri în ce a ieșit.

Analogul lui `scripts/action_drive.py` (NX-236) pentru telemetrie.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from src.observability import bootstrap, hooks, metrics, tracing  # noqa: E402
from src.observability import config as obs_config  # noqa: E402

TURN = "0a1b2c3d-4e5f-6071-8293-a4b5c6d7e8f9"

# Canary: exact formele pe care le-ar putea injecta un client sau o bibliotecă terță.
TELEFON = "0721 345 678"
EMAIL = "maria.ionescu@example.ro"
CHEIE = "sk-proj-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"
CANARY = (TELEFON, EMAIL, CHEIE)


def _settings(ratio: float = 1.0):
    return SimpleNamespace(
        observability_enabled=True,
        observability_traces_enabled=True,
        observability_metrics_enabled=True,
        observability_exporter="capture",
        observability_otlp_endpoint="",
        observability_otlp_headers="",
        observability_sample_ratio=ratio,
        observability_queue_max=2048,
        observability_export_batch=256,
        observability_flush_timeout_ms=100,
        observability_trace_secret="drive-secret",
        service_name="nativx-assistant",
        env="drive",
        release_sha="a1b2c3d",
        release_track="candidate",
    )


def _turn(*, attempt: int, esueaza: bool, otraveste: bool = False) -> None:
    """Un turn ca la executor: accept → queue → execute → model/tool/validate → terminal."""
    hooks.on_turn_request("accepted", release_track="candidate", duration_s=0.012)
    hooks.on_queue_wait(0.34, attempt_bucket=str(attempt))
    if attempt > 1:
        hooks.on_reclaim(str(attempt))
    try:
        with tracing.turn_trace(TURN, attempt=attempt, attempt_bucket=str(attempt)):
            with tracing.span("web.agent.call", stage="model", model_id="gpt-5.4-mini"):
                hooks.on_model_call(
                    "gpt-5.4-mini",
                    model_role="agent",
                    latency_s=1.4,
                    tokens_in=2100,
                    tokens_out=340,
                    cost_usd=0.0021,
                )
            with tracing.span("web.tool.call", stage="tools", tool_name="search_products"):
                with hooks.tool_call("search_products"):
                    pass
            with tracing.span("web.validate", stage="validation") as sp:
                if otraveste:
                    # Toate ușile prin care ar putea intra conținut, deodată:
                    sp.set_attribute("prompt", f"caut ceva, sună-mă la {TELEFON}")
                    sp.set_attribute("model_id", CHEIE)
                    tracing.set_attribute("tool_name", f"search?q={EMAIL}")
                hooks.on_validation("grounding", "ok")
            with tracing.span("web.result.commit", stage="commit"):
                if esueaza:
                    raise TimeoutError(f"commit blocat pentru {EMAIL}")
    except TimeoutError:
        hooks.on_terminal(
            "failed",
            safe_error_code_="processing_error",
            release_track="candidate",
            end_to_end_s=3.9,
        )
        return
    hooks.on_execution(2.1, outcome="completed")
    hooks.on_terminal(
        "completed", safe_error_code_=None, release_track="candidate", end_to_end_s=2.4
    )


def _arata_spans(sink) -> None:
    by_id = {s.span_id: s for s in sink.spans}
    for s in sorted(sink.spans, key=lambda s: s.start_unix_ns):
        parinte = by_id.get(s.parent_span_id or "")
        indent = "    " if parinte is not None else "  "
        print(
            f"{indent}{s.name:<24} span={s.span_id} parent={s.parent_span_id or '—':<16} "
            f"status={s.status:<5} {s.duration_ns / 1e6:.2f}ms"
        )


def main() -> int:
    print("=" * 96)
    print("NX-246 — drive de observabilitate (sink în memorie, zero rețea)")
    print("=" * 96)

    # ── 1. Turn sănătos + reclaim: același trace, spans de attempt diferite ──────────────────
    bootstrap.setup(_settings(ratio=1.0))
    metrics.reset()
    sink = bootstrap.capture_sink()
    _turn(attempt=1, esueaza=False)
    _turn(attempt=2, esueaza=False)
    tracing.get_exporter().flush()

    print("\n[1] Trace: două încercări ale ACELUIAȘI turn (reclaim), fără nimic persistat\n")
    _arata_spans(sink)
    trace_ids = {s.trace_id for s in sink.spans}
    roots = sink.by_name("web.turn.execute")
    print(f"\n  trace_id-uri distincte: {len(trace_ids)} (așteptat: 1) → {trace_ids.pop()}")
    print(
        f"  rădăcini (attempt-uri): {len(roots)}, span_id-uri distincte: "
        f"{len({r.span_id for r in roots})}"
    )
    print(f"  derivat din turn_id, fără coloană nouă: {tracing.trace_id_for(TURN, 'drive-secret')}")

    print("\n[2] Metrici (etichete + valori)\n")
    snap = metrics.snapshot()
    for key, value in snap["counters"].items():
        print(f"  {key} = {value:g}")
    for key, hist in snap["histograms"].items():
        print(f"  {key} n={hist['count']} sum={hist['sum']}")

    # ── 3. Eșantionare pe coadă: ratio 0, dar turul eșuează ⇒ iese TOT ───────────────────────
    bootstrap.setup(_settings(ratio=0.0))
    metrics.reset()
    sink = bootstrap.capture_sink()
    _turn(attempt=1, esueaza=False)
    tracing.get_exporter().flush()
    sanatos = len(sink.spans)
    _turn(attempt=1, esueaza=True)
    tracing.get_exporter().flush()
    print(f"\n[3] sample_ratio=0.0 → tur sănătos: {sanatos} spans exportate (așteptat 0)")
    print(f"    tur EȘUAT: {len(sink.spans)} spans exportate (traceul întreg, nu doar eroarea)")
    for s in sink.spans:
        print(f"      {s.name:<24} status={s.status}")

    # ── 4. Canary ───────────────────────────────────────────────────────────────────────────
    bootstrap.setup(_settings(ratio=1.0))
    metrics.reset()
    sink = bootstrap.capture_sink()
    _turn(attempt=1, esueaza=True, otraveste=True)
    tracing.get_exporter().flush()
    sink.emit_metrics(metrics.snapshot())
    text = sink.all_text()
    print("\n[4] Canary PII/secret injectat în atribute, etichete și mesaje de excepție\n")
    scapari = [c for c in CANARY if c in text or c.replace(" ", "") in text.replace(" ", "")]
    for c in CANARY:
        stare = "SCURS ✗" if c in scapari else "curat ✓"
        print(f"  {stare}  {c}")
    print("\n  Ce a ieșit în loc, pe spanul de validare:")
    for s in sink.by_name("web.validate"):
        for k, v in sorted(s.attributes.items()):
            print(f"    {k} = {v}")
    print("\n  Drop-uri numărate (o poartă tăcută ar fi la fel de rea ca o scurgere):")
    for key, value in metrics.snapshot()["counters"].items():
        if key.startswith("web_observability_dropped_total"):
            print(f"    {key} = {value:g}")

    obs_config.configure(None)
    tracing.set_exporter(None)
    metrics.reset()
    print("\n" + "=" * 96)
    print("VERDICT:", "SCURGERE — vezi [4]" if scapari else "curat — niciun canary nu a ieșit")
    print("=" * 96)
    return 1 if scapari else 0


if __name__ == "__main__":
    raise SystemExit(main())
