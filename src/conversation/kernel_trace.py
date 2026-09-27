"""Kernel `kernel.v1.0` — traceul canonic al unui tur interpretat (pasul 1, NX-328).

Contractul, §„Canonical turn trace": fiecare tur interpretat scrie UN `KernelTrace`, adică ieșirea
fiecărui strat, în ordine. E unitatea pe care o compară replay-ul și locul din care se citește
„de ce a recomandat produse pentru corp aici?": deschizi turul și citești primul ✗.

Trei piese, toate PURE (poarta I13):

- `KernelTrace`: modelul. Textul clientului apare doar ca citatele pe care interpretarea le poartă
  deja (redactarea existentă a `conversation_traces.diagnostics`).
- `render`: forma lizibilă, o linie pe strat.
- `first_divergence`: comparatorul pe straturi. Un journey își etichetează doar straturile care îl
  interesează; se raportează PRIMUL strat diferit, fiindcă cele de după el sunt așteptate să fie
  greșite (contractul, §„First-divergence debugging")."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator

from src.conversation.interpretation import (
    KERNEL_CONTRACT_VERSION,
    AmbiguityDecision,
    AnswerPolicy,
    CheckedChange,
    ResolvedRef,
    TurnInterpretation,
    TurnPlan,
)


class KernelTrace(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: str  # I18: fiecare trace poartă versiunea contractului
    vocabulary_snapshot: str  # id-ul pachetului/vocabularului folosit în acest tur
    interpretation: TurnInterpretation
    checked_changes: list[CheckedChange]
    resolved_refs: list[ResolvedRef]
    state_before: dict[str, Any]  # nevoi + subiect + parcat + referințe, ca handle-uri
    proposals: list[dict[str, Any]]  # intrarea reducerului
    rejected: list[dict[str, Any]]  # respingerile reducerului, cu motiv
    state_after: dict[str, Any]
    ambiguity: AmbiguityDecision
    plan: TurnPlan
    executor: str
    answer_policy: AnswerPolicy | None

    @field_validator("contract_version")
    @classmethod
    def _versioned(cls, value: str) -> str:
        # I18: replay-urile se despart pe versiune; un string oarecare le-ar amesteca tăcut.
        if not _VERSION_RE.fullmatch(value):
            raise ValueError(f"contract_version invalid: {value!r}")
        return value


_VERSION_RE = re.compile(r"kernel\.v\d+\.\d+")

#: Straturile, în ORDINEA contractului, ca (nume raportat, câmp din trace). Ordinea e întregul
#: design al comparatorului: un strat greșit îi face greșiți pe toți cei de după el.
LAYERS: tuple[tuple[str, str], ...] = (
    ("interpretation", "interpretation"),
    ("checked", "checked_changes"),
    ("resolver", "resolved_refs"),
    ("reducer", "state_after"),
    ("ambiguity", "ambiguity"),
    ("plan", "plan"),
    ("executor", "executor"),
    ("answer_policy", "answer_policy"),
)
LAYER_NAMES: tuple[str, ...] = tuple(name for name, _ in LAYERS)


def _plain(value: Any) -> Any:
    """Forma comparabilă a unei valori de strat: modelele devin dict-uri JSON, recursiv."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    return value


@dataclass(frozen=True)
class Divergence:
    layer: str
    expected: Any
    actual: Any
    passed: tuple[str, ...]  # straturile etichetate care au trecut, în ordine

    def report(self) -> str:
        """„interpretation ✓ → checked ✓ → resolver ✗ (...)", forma din contract."""
        ok = " → ".join(f"{name} ✓" for name in self.passed)
        head = f"{ok} → " if ok else ""
        return f"{head}{self.layer} ✗ (expected {self.expected!r}, got {self.actual!r})"


def first_divergence(expected: dict[str, Any], actual: KernelTrace) -> Divergence | None:
    """Primul strat etichetat în `expected` care diferă de `actual`, sau None.

    `expected` are chei din `LAYER_NAMES`; o cheie necunoscută e o eroare de etichetare, nu un
    strat ignorat tăcut (altfel un journey cu o greșeală de tipar ar trece mereu)."""
    unknown = set(expected) - set(LAYER_NAMES)
    if unknown:
        raise ValueError(f"straturi necunoscute în etichete: {sorted(unknown)}")
    passed: list[str] = []
    for name, attr in LAYERS:
        if name not in expected:
            continue
        want, got = _plain(expected[name]), _plain(getattr(actual, attr))
        if want != got:
            return Divergence(name, want, got, tuple(passed))
        passed.append(name)
    return None


# --- afișare -------------------------------------------------------------------------------------

_WIDTH = 17


def _line(label: str, body: str) -> str:
    return f"{label:<{_WIDTH}}{body}".rstrip()


def _change(c: Any) -> str:
    parts = [c.op]
    if c.target:
        parts.append(c.target)
    if c.dimension:
        parts.append(c.dimension)
    if c.relation:
        parts.append(c.relation)
    value = c.value if c.value is not None else c.number
    if value is not None:
        parts.append(f"{value}{' ' + c.unit if c.unit else ''}")
    if c.relative_to:
        parts.append(f"vs {c.relative_to}")
    return " ".join(str(p) for p in parts) + f" · quote „{c.quote}”"


def _checked(c: CheckedChange) -> str:
    head = f"{c.dimension} {c.change.relation or c.change.op} {c.canonical_value}"
    tail = f"rejected={c.rejected}" if c.rejected else f"strength={c.strength}"
    return f"{head}  provenance={c.provenance}  {tail}"


def _ref(r: ResolvedRef) -> str:
    ids = ",".join(r.product_ids) or "—"
    why = f" ({r.reason})" if r.reason else ""
    return f"{r.ref_id} {r.kind} → {r.outcome} [{ids}] from {r.source}{why}"


def _state(state: dict[str, Any]) -> str:
    if not state:
        return "—"
    return "  ".join(f"{k}={_plain(state[k])}" for k in sorted(state))


def _plan(plan: TurnPlan) -> str:
    body = plan.executor
    if plan.product_ids:
        body += f"  ids=[{','.join(plan.product_ids)}]"
    if plan.search_args is not None:
        args = plan.search_args.model_dump(exclude_defaults=True)
        body += "  SearchArgs(" + ", ".join(f"{k}={args[k]!r}" for k in sorted(args)) + ")"
    if plan.depends_on is not None:
        body += f"  depends_on={plan.depends_on}"
    return body


def render(trace: KernelTrace, *, user_text: str | None = None) -> str:
    """Traceul, o linie pe strat (forma din contract). `user_text` nu stă în trace: îl dă cine
    citește turul (`scripts/kernel_trace.py`), fiindcă traceul poartă doar citatele."""
    i = trace.interpretation
    acts = ", ".join(a.kind + (f" {'/'.join(a.targets)}" if a.targets else "") for a in i.acts)
    changes = "; ".join(_change(c) for c in i.changes) or "—"
    lines = []
    if user_text is not None:
        lines.append(_line("USER", f"„{user_text}”"))
    lines += [
        _line("CONTRACT", f"{trace.contract_version}  vocabulary={trace.vocabulary_snapshot}"),
        _line("INTERPRETATION", f"thread={i.thread}  acts=[{acts}]  changes=[{changes}]"),
        _line("CHECKED", "; ".join(_checked(c) for c in trace.checked_changes) or "—"),
        _line("REFERENCES", "; ".join(_ref(r) for r in trace.resolved_refs) or "—"),
        _line("STATE BEFORE", _state(trace.state_before)),
        _line("PROPOSALS", "; ".join(_state(p) for p in trace.proposals) or "—"),
        _line("REJECTED", "; ".join(_state(r) for r in trace.rejected) or "—"),
        _line("STATE AFTER", _state(trace.state_after)),
        _line("AMBIGUITY", f"{trace.ambiguity.verdict}  ({trace.ambiguity.reason})"),
        _line("PLAN", _plan(trace.plan)),
        _line("EXECUTOR", trace.executor),
    ]
    if trace.answer_policy is not None:
        p = trace.answer_policy
        missing = f"  missing={','.join(p.missing)}" if p.missing else ""
        lines.append(_line("ANSWER POLICY", f"verdict_allowed={p.verdict_allowed}{missing}"))
    return "\n".join(lines)


__all__ = [
    "KERNEL_CONTRACT_VERSION",
    "LAYERS",
    "LAYER_NAMES",
    "Divergence",
    "KernelTrace",
    "first_divergence",
    "render",
]
