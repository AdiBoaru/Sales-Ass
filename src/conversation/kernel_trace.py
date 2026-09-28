"""Kernel `kernel.v1.2` — traceul canonic al unui tur interpretat (pasul 1, NX-328; v1.2, NX-336).

Contractul, §„Canonical turn trace": fiecare tur interpretat scrie UN `KernelTrace`, adică ieșirea
fiecărui strat, în ordine. E unitatea pe care o compară replay-ul și locul din care se citește
„de ce a recomandat produse pentru corp aici?": deschizi turul și citești primul ✗.

Trei piese, toate PURE (poarta I13):

- `KernelTrace`: modelul. Textul clientului apare doar ca citatele pe care interpretarea le poartă
  deja (redactarea existentă a `conversation_traces.diagnostics`).
- `render`: forma lizibilă, o linie pe strat.
- `first_divergence`: comparatorul pe straturi. Un journey își etichetează doar straturile care îl
  interesează; se raportează PRIMUL strat diferit, fiindcă cele de după el sunt așteptate să fie
  greșite (contractul, §„First-divergence debugging").

NX-336 (`kernel.v1.2`, minor): câmpuri ADITIVE, toate cu default (un tur multi-act, golurile,
dezvăluirile, contoarele, memoria porții), plus cele două operații de dinaintea stocării în
`conversation_traces.diagnostics["kernel"]`: `redact_trace` (textul clientului trece prin
redactorul NX-230, primit ca funcție, ca modulul să rămână pur) și `cap_trace` (plafon 16 KB, cu
ordinea de tăiere declarată și rezumatul ca ultimă formă). `state_view` e forma compactă a stării
din trace, aceeași pe care o etichetează journey-urile."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.conversation.interpretation import (
    KERNEL_CONTRACT_VERSION,
    AmbiguityDecision,
    AnswerPolicy,
    CheckedChange,
    ResolvedRef,
    TurnInterpretation,
    TurnPlan,
)
from src.conversation.state_v2 import ConversationStateV2, Need


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
    plan: TurnPlan  # planul actului PRINCIPAL
    executor: str
    answer_policy: AnswerPolicy | None
    # --- v1.2 (NX-336), aditive: constructorii de dinainte rămân valizi ---------------------------
    plans: list[TurnPlan] = Field(default_factory=list)  # toate planurile turului (multi-act)
    gaps: list[str] = Field(default_factory=list)  # `turn_planner.GAPS`: nu devin text
    disclosures: list[tuple[int, str]] = Field(default_factory=list)  # (plan, cod)
    dropped_acts: int = 0
    delta_counters: dict[str, int] = Field(default_factory=dict)
    gate_memory: dict[str, str | None] | None = None  # ce ar scrie memoria întrebării
    truncated: bool = False  # plafonul a tăiat ceva (`cap_trace`)

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


# --- starea, redactarea, plafonul (v1.2, NX-336) -------------------------------------------------


def _need_label(need: Need) -> str:
    value = need.normalized_value
    shown_value = f"{value:g}" if isinstance(value, float) else value
    return f"{need.key} {need.operator} {shown_value}"


def state_view(state: ConversationStateV2) -> dict[str, Any]:
    """Forma COMPACTĂ a stării în trace și în eticheta stratului `reducer`: subiectul, nevoile
    ACTIVE ca „cheie operator valoare", ecranul, parcatul, seturile de mai devreme și focusul.
    Câmpurile goale lipsesc. Mutată din `tests/kernel/fixture_catalog.py` (NX-336): producția și
    replay-ul scriu aceeași formă."""
    view: dict[str, Any] = {
        "topic": state.topic.category_key,
        "needs": sorted(_need_label(n) for n in state.active_needs()),
        "shown": [d.product_id for d in state.references.displayed_products],
    }
    if state.parked is not None:
        view["parked"] = {
            "topic": state.parked.topic.category_key,
            "needs": sorted(_need_label(n) for n in state.parked.needs),
            "shown": [d.product_id for d in state.parked.shown],
        }
    if state.references.recent_sets:
        view["recent"] = [[d.product_id for d in s] for s in state.references.recent_sets]
    if state.references.selected_product:
        view["selected"] = state.references.selected_product
    return view


def _redacted(trace: KernelTrace, redact: Callable[[str], str]) -> KernelTrace:
    """Cititorul DECLARAT al textului din trace (`raw_text_readers`): fiecare câmp text pe care
    l-a scris MODELUL (citatul, valoarea, unitatea, dimensiunea, țintele, textul/numele/valoarea
    referinței, cererea actului, lecturile), valoarea canonică a schimbării verificate (pentru
    `unmapped` e chiar cuvântul clientului), etichetele nevoilor din stări (valoarea unei nevoi
    poate fi textul clientului) și TOATE câmpurile text ale argumentelor de căutare trec prin
    `redact`. Id-urile de produs, codurile și cheile nu (un id nu e text de client, iar un
    detector pe cifre l-ar putea strica). Nimic nu decide pe ele: se înlocuiesc, nu se compară.

    Recenzia NX-336 (constatarea 1): `canonical_value` scăpa; testul caută acum semănătura în
    TOATE frunzele text ale traceului stocat, prin stagiul real."""

    def text(value: Any) -> Any:
        return redact(value) if isinstance(value, str) and value else value

    def deep(value: Any) -> Any:
        if isinstance(value, str):
            return text(value)
        if isinstance(value, list | tuple):
            return type(value)(deep(v) for v in value)
        if isinstance(value, dict):
            return {k: deep(v) for k, v in value.items()}
        return value

    def change(c: Any) -> Any:
        return c.model_copy(
            update={
                "quote": text(c.quote),
                "value": text(c.value),
                "unit": text(c.unit),
                "dimension": text(c.dimension),
                "target": text(c.target),
                "relative_to": text(c.relative_to),
            }
        )

    def plan(p: TurnPlan) -> TurnPlan:
        args = p.search_args
        if args is None:
            return p
        cleaned = args.model_copy(
            update={k: deep(getattr(args, k)) for k in type(args).model_fields}
        )
        return p.model_copy(update={"search_args": cleaned})

    def state(view: dict[str, Any]) -> dict[str, Any]:
        out = dict(view)
        if "needs" in out:
            out["needs"] = deep(out["needs"])
        if isinstance(out.get("parked"), dict) and "needs" in out["parked"]:
            out["parked"] = {**out["parked"], "needs": deep(out["parked"]["needs"])}
        return out

    i = trace.interpretation
    interpretation = i.model_copy(
        update={
            "acts": [
                a.model_copy(update={"query": text(a.query), "targets": deep(a.targets)})
                for a in i.acts
            ],
            "changes": [change(c) for c in i.changes],
            "references": [
                r.model_copy(
                    update={
                        "id": text(r.id),
                        "text": text(r.text),
                        "name": text(r.name),
                        "value": text(r.value),
                        "dimension": text(r.dimension),
                    }
                )
                for r in i.references
            ],
            "ambiguities": [
                a.model_copy(update={"readings": deep(a.readings), "target": text(a.target)})
                for a in i.ambiguities
            ],
        }
    )
    checked = [
        c.model_copy(
            update={
                "change": change(c.change),
                "canonical_value": text(c.canonical_value),
                "dimension": text(c.dimension),
            }
        )
        for c in trace.checked_changes
    ]
    # A doua recenzie: `ResolvedRef.ref_id` e id-ul referinței SCRIS DE MODEL (fără tipar în
    # schemă), copiat de resolver; `product_ids` sunt ale catalogului și nu se ating.
    resolved = [r.model_copy(update={"ref_id": text(r.ref_id)}) for r in trace.resolved_refs]
    return trace.model_copy(
        update={
            "interpretation": interpretation,
            "checked_changes": checked,
            "resolved_refs": resolved,
            "state_before": state(trace.state_before),
            "state_after": state(trace.state_after),
            "plan": plan(trace.plan),
            "plans": [plan(p) for p in trace.plans],
        }
    )


def redact_trace(trace: KernelTrace, redact: Callable[[str], str]) -> KernelTrace:
    """Traceul cu textul clientului redactat (P12), ÎNAINTE de `model_dump`: `diagnostics` se
    stochează verbatim, deci fără pasul ăsta traceul ar ocoli frontiera NX-230. `redact` e funcția
    frontierei (aceleași categorii ca `apply_boundary`), dată de apelant: modulul rămâne pur."""
    return _redacted(trace, redact)


#: Plafonul traceului stocat (octeți UTF-8 ai JSON-ului). Măsurat pe fixture: 0,7-2,8 KB, p50
#: 1,2 KB, deci plafonul prinde doar cazurile patologice (un mesaj uriaș, un set enorm).
TRACE_CAP_BYTES = 16 * 1024
#: Câte id-uri păstrează o referință rezolvată după primul pas de tăiere.
_CAP_IDS = 6
#: Cheile stării păstrate după al doilea pas: subiectul și nevoile (handle-urile).
_CAP_STATE_KEYS = ("topic", "needs")
#: Actele păstrate în rezumat: ultimele (actul principal e ultimul), cât plafonul de runtime.
_SUMMARY_ACTS = 3


def trace_size(trace: KernelTrace) -> int:
    """Mărimea traceului așa cum se stochează (JSON compact, UTF-8)."""
    doc = trace.model_dump(mode="json")
    return len(json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _cap_ids(t: KernelTrace) -> KernelTrace:
    refs = [r.model_copy(update={"product_ids": r.product_ids[:_CAP_IDS]}) for r in t.resolved_refs]
    return t.model_copy(update={"resolved_refs": refs})


def _cap_states(t: KernelTrace) -> KernelTrace:
    def keep(state: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in state.items() if k in _CAP_STATE_KEYS}

    return t.model_copy(
        update={"state_before": keep(t.state_before), "state_after": keep(t.state_after)}
    )


def _cap_quotes(t: KernelTrace) -> KernelTrace:
    checked = [
        c.model_copy(update={"change": c.change.model_copy(update={"quote": ""})})
        for c in t.checked_changes
    ]
    return t.model_copy(update={"checked_changes": checked})


def _cap_interpretation(t: KernelTrace) -> KernelTrace:
    lean = t.interpretation.model_copy(update={"changes": [], "references": [], "ambiguities": []})
    return t.model_copy(update={"interpretation": lean})


def _summary(t: KernelTrace, *, with_args: bool) -> KernelTrace:
    """Ultima formă: versiunea, amprenta, actele (fără cerere), planurile, executorul."""

    def plan(p: TurnPlan) -> TurnPlan:
        return p if with_args else p.model_copy(update={"search_args": None})

    acts = [a.model_copy(update={"query": None}) for a in t.interpretation.acts[-_SUMMARY_ACTS:]]
    return KernelTrace(
        contract_version=t.contract_version,
        vocabulary_snapshot=t.vocabulary_snapshot,
        interpretation=TurnInterpretation(
            thread=t.interpretation.thread,
            acts=acts,
            changes=[],
            references=[],
            ambiguities=[],
            corrects_previous_turn=t.interpretation.corrects_previous_turn,
        ),
        checked_changes=[],
        resolved_refs=[],
        state_before={},
        proposals=[],
        rejected=[],
        state_after={},
        ambiguity=t.ambiguity,
        plan=plan(t.plan),
        executor=t.executor,
        answer_policy=None,
        plans=[plan(p) for p in t.plans],
        dropped_acts=t.dropped_acts,
        truncated=True,
    )


def cap_trace(trace: KernelTrace, limit: int = TRACE_CAP_BYTES) -> KernelTrace:
    """Traceul sub `limit` octeți, tăiat în ORDINEA declarată (contractul v1.2): id-urile peste 6
    din referințele rezolvate, stările la subiect + nevoi, citatele din `checked_changes`, apoi
    interpretarea la acte + `thread`. Dacă tot depășește, rămâne doar rezumatul (fără argumentele
    căutării, la nevoie), cu `truncated=True`. Rezultatul rămâne valid pentru `render`."""
    current = trace
    for step in (_cap_ids, _cap_states, _cap_quotes, _cap_interpretation):
        if trace_size(current) <= limit:
            return current
        current = step(current).model_copy(update={"truncated": True})
    if trace_size(current) <= limit:
        return current
    summary = _summary(current, with_args=True)
    if trace_size(summary) <= limit:
        return summary
    return _summary(current, with_args=False)


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
    # v1.2 (NX-336): liniile noi apar doar când au ce spune, ca traceurile de dinainte să se
    # afișeze exact ca înainte.
    if len(trace.plans) > 1:
        lines.append(_line("PLANS", " | ".join(_plan(p) for p in trace.plans)))
    if trace.gaps:
        lines.append(_line("GAPS", ", ".join(trace.gaps)))
    if trace.disclosures:
        lines.append(_line("DISCLOSURES", ", ".join(f"{i}:{c}" for i, c in trace.disclosures)))
    if trace.dropped_acts:
        lines.append(_line("DROPPED ACTS", str(trace.dropped_acts)))
    if trace.gate_memory:
        lines.append(_line("GATE MEMORY", _state(trace.gate_memory)))
    if trace.truncated:
        lines.append(_line("TRUNCATED", "yes"))
    return "\n".join(lines)


__all__ = [
    "KERNEL_CONTRACT_VERSION",
    "LAYERS",
    "LAYER_NAMES",
    "TRACE_CAP_BYTES",
    "Divergence",
    "KernelTrace",
    "cap_trace",
    "first_divergence",
    "redact_trace",
    "render",
    "state_view",
    "trace_size",
]
