"""NX-239 — control plane determinist: `FastPathDecision` + poarta de early-exit a runner-ului.

Un fast path poate ÎNCHEIA turul numai dacă un recognizer determinist dovedește că reply-ul lui
acoperă TOATE obligațiile mesajului (extrase din cod, nu de model). Un reply incomplet nu se
aruncă: devine SEMNAL (`BrainSignal`) pentru MainBrain — răspunsul FAQ pe un mesaj mixt ajunge în
contextul brain-ului, nu la client pe post de „primul reply câștigă".

Cine are voie să finalizeze (copy authored/canonic sau gate de corectitudine) e o mulțime ÎNCHISĂ,
declarată aici; ce ACOPERĂ fiecare stagiu e declarat în fișierul stagiului (`FAST_PATH_COVERS`).
Triajul NU e fast path: reply-urile lui `simple`/`clarify` sunt compuse de un LLM (writer
concurent) — sub single-brain ele devin semnale, iar brain-ul rămâne singurul writer semantic.

Totul rulează DOAR sub `single_brain_enabled` (runner-ul nici nu ne cheamă altfel) — OFF e
byte-identic cu pipeline-ul de azi.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.agent.brain_models import BrainSignal, DetectedObligation, obligations_from_ctx

CONTROL_PLANE_VERSION = "control_plane.v1"

#: Stagiile care POT finaliza turul (copy authored/canonic sau gate de corectitudine/terminal).
#: `gates` (auth/risc/tăcere), `action_kernel` (decizie deja luată, mesaj gol prin construcție),
#: `clarify_resume` (consum determinist de slot), `greeting` (pur salut prin construcție),
#: `alias` (match exact aprobat), `handoff` (escaladare), `fallback` (terminal P6). `faq`/`cache`
#: servesc conținut canonic, dar DOAR pe mesaje cu o singură obligație (nu întrerup un mesaj mixt).
_ALWAYS_COMPLETE: frozenset[str] = frozenset(
    {
        "gates_stage",
        "action_kernel_stage",
        "clarify_resume_stage",
        "handoff_stage",
        "fallback_stage",
        "agent_stage",  # MainBrain / agentul E writerul principal — reply-ul lui e finalul
    }
)
#: Stagiile cu conținut canonic care finalizează DOAR pe o singură obligație (nu pe mesaj mixt).
_SINGLE_OBLIGATION_ONLY: frozenset[str] = frozenset({"faq_stage", "cache_stage", "alias_stage"})
#: Stagii care nu finalizează NICIODATĂ sub single-brain (writer LLM concurent).
_NEVER_COMPLETE: frozenset[str] = frozenset({"triage_stage"})


def _stage_covers() -> dict[str, tuple[str, ...]]:
    """Ce acoperă reply-ul fiecărui stagiu — DECLARAT de stagii (`FAST_PATH_COVERS`), citit aici.
    Import LAZY + tolerant: un stagiu fără declarație cade pe default-ul conservator."""
    try:
        from src.worker.stages import cache, faq, greeting, handoff, triage  # noqa: PLC0415

        return {
            "greeting_stage": greeting.FAST_PATH_COVERS,
            "faq_stage": faq.FAST_PATH_COVERS,
            "cache_stage": cache.FAST_PATH_COVERS,
            "handoff_stage": handoff.FAST_PATH_COVERS,
            "triage_stage": triage.FAST_PATH_COVERS,
            "alias_stage": ("question_0",),
            "action_kernel_stage": ("opaque_action",),
            "clarify_resume_stage": ("pending_clarification",),
        }
    except Exception:  # noqa: BLE001 — fără declarații, folosim doar regulile închise de mai sus
        return {}


@dataclass(frozen=True, slots=True)
class FastPathDecision:
    """Decizia control plane-ului pe un early-exit: cine a răspuns, ce acoperă, ce rămâne."""

    path: str
    complete: bool
    covered_obligations: tuple[str, ...]
    uncovered: tuple[str, ...]
    evidence_refs: tuple[str, ...] = ()
    reason: str = "exact_fast_path"
    version: str = CONTROL_PLANE_VERSION


def _count_bucket(n: int) -> str:
    if n <= 1:
        return str(n)
    if n <= 3:
        return "2-3"
    return "4+"


def decide(ctx: Any, stage_name: str) -> FastPathDecision:
    """Recognizer-ul determinist: reply-ul stagiului acoperă toate obligațiile mesajului?

    PUR pe semnale (nu apelează DB/LLM). `greeting` e complet prin construcție (setul exact de
    saluturi); `faq`/`cache`/`alias` sunt complete DOAR pe o singură obligație de tip răspuns —
    un mesaj mixt (salut+întrebare, FAQ+recomandare, acțiune+text) merge mai departe la brain."""
    obligations: tuple[DetectedObligation, ...] = obligations_from_ctx(ctx)
    keys = tuple(o.key for o in obligations)
    covered = _stage_covers().get(stage_name, keys)

    if stage_name in _ALWAYS_COMPLETE:
        return FastPathDecision(stage_name, True, keys, (), reason="gate_or_terminal")
    if stage_name in _NEVER_COMPLETE:
        return FastPathDecision(stage_name, False, (), keys, reason="competing_llm_writer")
    if stage_name == "greeting_stage":
        # `is_greeting` e match EXACT pe tot mesajul → dacă a servit, mesajul era pur salut.
        uncovered = tuple(k for k in keys if k not in ("greeting", "safety_context"))
        return FastPathDecision(
            stage_name,
            not uncovered,
            ("greeting",),
            uncovered,
            reason="pure_greeting" if not uncovered else "mixed_intent",
        )
    if stage_name in _SINGLE_OBLIGATION_ONLY:
        answerable = [o for o in obligations if o.kind in ("answer", "recommend", "compare")]
        blocking = [o for o in obligations if o.kind in ("action",)]
        greeted = any(o.key == "greeting" for o in obligations)
        mixed = len(answerable) > 1 or bool(blocking) or greeted
        safety = any(o.kind == "safety" for o in obligations)
        complete = not mixed and not safety and len(answerable) <= 1
        uncovered = () if complete else tuple(k for k in keys if k not in covered)
        if complete:
            reason = "single_obligation"
        else:
            reason = "safety_context" if safety else "mixed_intent"
        return FastPathDecision(
            stage_name,
            complete,
            covered if complete else (),
            uncovered,
            reason=reason,
        )
    # Stagiu necunoscut: conservator, îl lăsăm să finalizeze (azi), dar spunem asta explicit.
    return FastPathDecision(stage_name, True, keys, (), reason="unrecognized_stage")


def gate_early_exit(ctx: Any, stage_name: str) -> FastPathDecision:
    """Poarta chemată de runner (DOAR sub flag) când un stagiu a setat `reply`.

    Complet → early-exit ca azi. Incomplet → reply-ul devine `BrainSignal` (context pt MainBrain),
    `ctx.reply` se golește și pipeline-ul continuă. Emite `control_plane_decision` +
    `turn_obligations` (low-cardinality, fără text)."""
    decision = decide(ctx, stage_name)
    ctx.fast_path = decision
    ctx.emit(
        "control_plane_decision",
        path=decision.path,
        reason=decision.reason,
        complete=decision.complete,
    )
    ctx.emit(
        "turn_obligations",
        count_bucket=_count_bucket(len(decision.covered_obligations) + len(decision.uncovered)),
        covered=len(decision.covered_obligations),
        missing=len(decision.uncovered),
    )
    if decision.complete:
        return decision

    reply = ctx.reply
    if reply is not None:
        ctx.brain_signals.append(
            BrainSignal(
                stage=stage_name,
                kind="triage_reply" if stage_name == "triage_stage" else "stage_reply",
                text=reply.text,
                suggestions=tuple(reply.suggestions or ()),
            )
        )
        # Un clarify demote-uit nu are voie să lase slotul pending în state — brain-ul decide
        # clarificarea prin plan; propunerea stagiului se retrage odată cu reply-ul.
        if reply.pending_question:
            ctx.state_proposals = [
                p for p in ctx.state_proposals if getattr(p, "op", None) != "set_pending_question"
            ]
        ctx.reply = None
    return decision


__all__ = ["CONTROL_PLANE_VERSION", "FastPathDecision", "decide", "gate_early_exit"]
