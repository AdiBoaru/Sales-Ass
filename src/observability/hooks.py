"""NX-246 — hook-urile NEUTRE pe care le cheamă restul codului. Zero vendor, zero business logic.

Cardul cere ca „stagiile de business să nu importe exporterul și să nu decidă samplingul", și ca
runnerul/adaptoarele comune să primească „hooks neutre". Modulul ăsta e chiar acel contract: un
apelant vede `hooks.on_model_call(...)`, nu OpenTelemetry, nu un registru de metrici, nu o decizie
de eșantionare. Dacă mâine schimbăm backendul, se schimbă `src/observability/`, nu `src/agent/`.

Toate funcțiile de aici respectă aceleași trei reguli:

  • **nu ridică** (P6: observabilitatea nu rupe turul) — excepția e modul strict din teste, unde
    o abatere de contract TREBUIE să pice, fiindcă atunci o repari ieftin;
  • **nu așteaptă** — nimic din ce e aici nu face I/O; exportul e treaba cozii din `export.py`;
  • **nu decid nimic despre tur** — nu schimbă rute, nu ating `ctx`, nu întorc valori de control.
"""

from __future__ import annotations

from contextlib import contextmanager
from time import perf_counter
from typing import Any

from src.observability import metrics
from src.observability.contract import UNKNOWN

# ── Margine: acceptul unui turn web ─────────────────────────────────────────────────────────


def on_turn_request(outcome: str, *, release_track: str, duration_s: float | None = None) -> None:
    """Un request v2 la margine, cu verdictul lui de accept (`accepted|replayed|conflict|...`).

    Sursa unică pentru „accept availability": un request respins nu creează rând de ledger, deci
    ledgerul nu îl poate număra (vezi `slo._accept_availability`).
    """
    metrics.record_counter("web_turn_requests_total", outcome=outcome, release_track=release_track)
    if duration_s is not None:
        metrics.record_histogram("web_turn_accept_duration_seconds", duration_s, outcome=outcome)


def accept_outcome(status_code: int) -> str:
    """Status HTTP → verdict de accept, din vocabularul ÎNCHIS.

    Derivarea din status e deliberată: e singura formă de instrumentare care nu poate rămâne în
    urma codului. O rută cu 15 ieșiri capătă mâine a 16-a, iar aceasta va fi numărată corect fără
    ca cineva să-și amintească să adauge un apel.
    """
    if status_code == 202:
        return "accepted"
    if status_code == 200:
        return "replayed"
    if status_code == 409:
        return "conflict"
    if 400 <= status_code < 500:
        return "rejected"
    if status_code >= 500:
        return "error"
    return "accepted"


def on_replay(result_status: str) -> None:
    metrics.record_counter("web_turn_replay_total", result_status=result_status)


def on_idempotency_conflict() -> None:
    metrics.record_counter("web_turn_idempotency_conflict_total")


# ── Executor: coadă, attempt, terminal ──────────────────────────────────────────────────────


def on_queue_wait(seconds: float, *, attempt_bucket: str) -> None:
    metrics.record_histogram("web_turn_queue_wait_seconds", seconds, attempt_bucket=attempt_bucket)


def on_reclaim(attempt_bucket: str) -> None:
    metrics.record_counter("web_turn_reclaim_total", attempt_bucket=attempt_bucket)


def on_fenced(phase: str) -> None:
    metrics.record_counter("web_turn_fenced_completion_total", phase=phase)


def on_execution(seconds: float, *, outcome: str, route_mode: str = UNKNOWN) -> None:
    metrics.record_histogram(
        "web_turn_execution_seconds", seconds, route_mode=route_mode, outcome=outcome
    )


def on_terminal(
    status: str,
    *,
    safe_error_code_: str | None,
    release_track: str,
    end_to_end_s: float | None = None,
    turn_class: str = UNKNOWN,
) -> None:
    """Terminalul durabil al unui turn. `end_to_end_s` e timpul pe care îl simte clientul
    (accept → terminal), nu doar execuția — de aceea îl raportează executorul, nu pipeline-ul."""
    metrics.record_counter(
        "web_turn_terminal_total",
        status=status,
        safe_error_code=safe_error_code_ or "none",
        release_track=release_track,
    )
    if end_to_end_s is not None:
        metrics.record_histogram(
            "web_turn_end_to_end_seconds",
            end_to_end_s,
            turn_class=turn_class,
            outcome=status,
            release_track=release_track,
        )


def on_deadline(stage: str, reason: str) -> None:
    """Deadline epuizat, pe faza NX-241 în care s-a întâmplat. Vocabularul e deja închis acolo —
    aici doar îl transportăm, fără să-l reinterpretăm."""
    metrics.record_counter("web_turn_deadline_total", stage=stage, reason=reason)


# ── Adaptoare comune: model, tool, retrieval, validare ──────────────────────────────────────


def on_model_call(
    model_id: str,
    *,
    model_role: str,
    outcome: str = "ok",
    latency_s: float | None = None,
    tokens_in: int = 0,
    tokens_out: int = 0,
    cost_usd: float = 0.0,
) -> None:
    """Un apel de model. `model_id` e exact (buget de 24 valori distincte — vezi contract)."""
    metrics.record_counter(
        "web_model_calls_total", model_role=model_role, model_id=model_id, outcome=outcome
    )
    if latency_s is not None:
        metrics.record_histogram(
            "web_model_latency_seconds", latency_s, model_role=model_role, model_id=model_id
        )
    if tokens_in:
        metrics.record_histogram(
            "web_model_tokens", tokens_in, model_role=model_role, direction="in"
        )
    if tokens_out:
        metrics.record_histogram(
            "web_model_tokens", tokens_out, model_role=model_role, direction="out"
        )
    if cost_usd:
        metrics.record_histogram("web_model_cost_usd", cost_usd, model_role=model_role)


@contextmanager
def tool_call(name: str):
    """Măsoară un apel de tool. Excepția se propagă NEATINSĂ, marcată `error`.

    Numele tool-ului vine din registrul de tool-uri (mulțime mărginită), deci e o etichetă
    acceptabilă — dar tot sub buget de cardinalitate, fiindcă „mărginit azi" nu e o garanție.
    """
    started = perf_counter()
    outcome = "ok"
    try:
        yield
    except BaseException:
        # `outcome` rămâne din vocabularul ÎNCHIS (`error`), nu tipul excepției: tipul e util pe
        # span (unde e un atribut), nu pe metrică (unde ar fi o etichetă cu cardinalitate deschisă).
        outcome = "error"
        raise
    finally:
        elapsed = perf_counter() - started
        metrics.record_counter("web_tool_calls_total", tool_name=name, outcome=outcome)
        metrics.record_histogram("web_tool_latency_seconds", elapsed, tool_name=name)


def on_retrieval(mode: str, outcome: str) -> None:
    metrics.record_counter("web_retrieval_outcomes_total", mode=mode, outcome=outcome)


def on_validation(check: str, outcome: str) -> None:
    metrics.record_counter("web_validation_total", check=check, outcome=outcome)


def snapshot() -> dict[str, Any]:
    """Fotografia metricilor procesului (raport local, drive, teste)."""
    return metrics.snapshot()
