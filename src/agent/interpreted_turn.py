"""Kernel `kernel.v2.0`, pasul 6 (NX-336) — orchestratorul turului interpretat (rol `orchestrator`).

Pașii 1-5 au construit fiecare strat ca funcție pură, fără apelant. Aici un tur REAL trece prin
lanț: `InterpretInput` din context → UN apel de interpretare (`turn_interpreter`, I13) → sursele
resolverului din starea v2 (+ pagina) → o citire de catalog (`fetch_reference_facts`, `business_id`
server-side) → `resolve_references` → `to_delta` (+ `resolve_question` pe o întrebare vie) →
`reduce_turn` DOAR în memorie (starea porții) → `decide_ambiguity` → `plan_turn`.

**PR A (dark):** lanțul rulează întreg, traceul (`kernel.v1.2`, redactat cu frontiera NX-230 și
plafonat la 16 KB) și evenimentele se scriu. Orice eșec (interpretare, citire, excepție) se scrie ca
`kernel_fallback{reason}`, fără trace.

**PR B (starea):** după lanț, orchestratorul scrie vederea de citire a turului
(`_apply_turn_view`: câmpurile v1 derivate din nevoi și subiect + `ctx.kernel_view`), ÎNAINTEA
executorilor, fiindcă un tur servit își compune răspunsul în executor; apoi `execute_plans`
(seam-ul executorilor, gol până la PR C: întoarce `None`, deci turul rămâne `dark` și ramura
întoarce `False` în producție). Când un executor servește turul, ultimul se scrie `ctx.kernel_turn`,
intrarea commit-ului (`worker/kernel_commit.py`): starea se persistă DOAR prin reducer.

Ciclul de viață (§2): rezultatul lanțului e o variabilă LOCALĂ. Contextul se fotografiază la
intrare (`ContextSnapshot`) și se restaurează pe orice ieșire neservită, ca executorii, care scriu
în context înainte ca orchestratorul să știe dacă turul e servit, să nu lase urme căii v1. Testul
compară TOATE câmpurile contextului, nu lista de aici.

Textul clientului se citește în DOI cititori declarați (`_turn_words`, `_redact`); deciziile
ramifică pe structuri. Modulul nu construiește `SearchArgs` (I2) și nu scrie starea (I3), în afara
celor două situri declarate: restaurarea (`_restore_state_fields`) și vederea de citire
(`_apply_turn_view`)."""

from __future__ import annotations

import copy
import logging
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from src.agent.deterministic import page_source
from src.agent.turn_planner import PlannedTurn, plan_turn
from src.catalog.reference_facts import fetch_reference_facts
from src.catalog.vocabulary_cache import get_vocabulary
from src.config import get_settings
from src.conversation.ambiguity_gate import (
    GateOutcome,
    decide_ambiguity,
    lookup_attributes,
    memory_proposal,
    question_answered,
)
from src.conversation.answer_policy import answer_policy
from src.conversation.clarification_policy import ClarificationPolicy
from src.conversation.delta import TurnDelta, to_delta
from src.conversation.interpretation import (
    KERNEL_CONTRACT_VERSION,
    AnswerPolicy,
    ResolvedRef,
    TurnInterpretation,
)
from src.conversation.interpretation_check import OUTCOMES
from src.conversation.kernel_trace import KernelTrace, cap_trace, redact_trace, state_view
from src.conversation.needs import NeedVocabulary
from src.conversation.references import (
    ReferenceFacts,
    plan_lookup,
    resolve_references,
    sources_from_state,
)
from src.conversation.state_reducer import (
    ReducedState,
    ReducerPolicy,
    StateUpdateProposal,
    reduce_turn,
)
from src.conversation.state_v2 import ConversationStateV2, project_v1
from src.conversation.turn_interpreter import (
    InterpretedTurn,
    InterpretInput,
    handles_of,
    interpret_turn,
)
from src.db.queries.catalog import list_category_menu
from src.models import ConversationState, Direction, TurnContext
from src.privacy.boundary import make_safe
from src.worker.kernel_commit import KernelTurn

if TYPE_CHECKING:
    from src.worker.runner import PipelineDeps

log = logging.getLogger(__name__)

#: Motivele pentru care turul NU e servit de kernel (`kernel_fallback.reason`,
#: `kernel_turn.fallback_reason`). Vocabular ÎNCHIS: `outcome`-urile interpretării (fără `ok`),
#: `exception` (o citire sau un strat care aruncă), `vocabulary_unavailable` (vocabularul nu s-a
#: putut citi: fallback ÎNAINTEA apelului), `snapshot_error` (instantaneul contextului a picat),
#: `executor_refused` (niciun executor n-a servit turul), `no_sentence` (PR C) și `dark` (lanțul a
#: mers, dar niciun executor nu există încă: `execute_plans` întoarce `None` până la PR C).
FALLBACK_REASONS: tuple[str, ...] = (
    *(o for o in OUTCOMES if o != "ok"),
    "exception",
    "vocabulary_unavailable",
    "snapshot_error",
    "executor_refused",
    "no_sentence",
    "dark",
)
#: Etichetele operațiilor de DB ale kernelului: excepția declarată de la suprafața I16 (un tur căzut
#: pe v1 e identic cu flagul stins în afara lor). Toate trei sunt ale kernelului, cu nume proprii
#: (recenzia, constatarea 2): nicio citire a căii v1 nu se exclude din comparație.
MENU_OP = "kernel_category_menu"
FACTS_OP = "kernel_reference_facts"
VOCABULARY_OP = "kernel_load_vocabulary"
KERNEL_READS: frozenset[str] = frozenset({MENU_OP, FACTS_OP, VOCABULARY_OP})
#: Traceul unui tur neservit (`dark`): niciun executor n-a rulat.
NO_EXECUTOR = "none"
_DARK = "dark"
_EXCEPTION = "exception"
_VOCABULARY_UNAVAILABLE = "vocabulary_unavailable"
_SNAPSHOT_ERROR = "snapshot_error"
_EXECUTOR_REFUSED = "executor_refused"

_ROLES: Mapping[Direction, str] = {Direction.INBOUND: "user", Direction.OUTBOUND: "bot"}


# --- instantaneul contextului (§2) ----------------------------------------------------------------

#: Câmpurile `TurnContext` pe care un executor le poate scrie înainte de verdictul „servit" și
#: care se restaurează prin `setattr`. NICIUN câmp de stare (`state*`): un `setattr` e invizibil
#: porții I3, deci `state_patch` și `state_proposals` se restaurează în `_restore_state_fields`,
#: vizibil (recenzia, constatarea 8). Lista nu e garanția: testul compară TOATE câmpurile.
EXECUTOR_WRITABLE: tuple[str, ...] = (
    "reply",
    "retrieval",
    "safety_decision",
    "routine",
    "match_set",
    "answer_plan",
    "grounded",
    "fast_path",
    "brain_signals",
)
#: Câmpurile ORCHESTRATORULUI, restaurate tot prin `setattr`: vederea de citire a turului
#: (`_apply_turn_view`) și intrarea commit-ului (scrisă ultima, deci niciodată pe fallback).
ORCHESTRATOR_WRITABLE: tuple[str, ...] = ("kernel_view", "kernel_turn")
#: Câmpurile vederii v1 pe care le scrie `_apply_turn_view` (și le restaurează
#: `_restore_state_fields`): cele DERIVATE din nevoi și subiect pe care le citește compunerea
#: (`compose._allowed_client_numbers`, `finalize._known_facets`,
#: `compare_narrative.comparison_needs`, `subject.spoken_needs`, `clarify_menu.menu_for_turn`).
#: Restul vederii v1 (ecranul, sesiunea, clarificarea, chips-urile) nu e al nevoilor sau al
#: subiectului și nu se rescrie în tur.
VIEW_FIELDS: tuple[str, ...] = ("constraints", "search_constraints")


@dataclass(frozen=True)
class KernelView:
    """Vederea de CITIRE a unui tur servit de kernel (`ctx.kernel_view`): starea PORȚII (după
    reducerea turului, înaintea executorilor) și dacă turul e primul al subiectului curent."""

    gate_state: ConversationStateV2
    subject_is_new: bool


@dataclass(frozen=True)
class ContextSnapshot:
    """Contextul de dinaintea ramurii, pe câmpurile pe care executorii și orchestratorul le pot
    scrie."""

    fields: Mapping[str, Any]
    patch: dict[str, Any]
    proposals: list[Any]
    view: Mapping[str, Any]

    @classmethod
    def take(cls, ctx: TurnContext) -> ContextSnapshot:
        return cls(
            fields={
                name: copy.deepcopy(getattr(ctx, name))
                for name in (*EXECUTOR_WRITABLE, *ORCHESTRATOR_WRITABLE)
            },
            patch=copy.deepcopy(ctx.state_patch),
            proposals=list(ctx.state_proposals),
            view={name: copy.deepcopy(getattr(ctx.state, name)) for name in VIEW_FIELDS},
        )

    def restore(self, ctx: TurnContext) -> tuple[str, ...]:
        """Aduce înapoi ce s-a schimbat; întoarce numele câmpurilor restaurate. Evenimentele și
        traceul (declarate ale kernelului) nu se ating."""
        changed = [name for name, value in self.fields.items() if getattr(ctx, name) != value]
        for name in changed:
            setattr(ctx, name, copy.deepcopy(self.fields[name]))
        return (*changed, *_restore_state_fields(ctx, self.patch, self.proposals, self.view))


def _restore_state_fields(
    ctx: TurnContext, patch: dict[str, Any], proposals: list[Any], view: Mapping[str, Any]
) -> tuple[str, ...]:
    """Unul din cele DOUĂ situri în care orchestratorul atinge câmpuri de stare ale contextului: le
    readuce la valoarea de DINAINTEA ramurii (anularea scrierilor unui executor și a vederii de
    citire pe fallback, nu o scriere nouă). Scris explicit, câmp cu câmp, ca poarta I3 să-l vadă;
    excepțiile ei sunt declarate în allowlist."""
    restored: list[str] = []
    if ctx.state_patch != patch:
        restored.append("state_patch")
        ctx.state_patch.clear()
        ctx.state_patch.update(copy.deepcopy(patch))
    if ctx.state_proposals != proposals:
        restored.append("state_proposals")
        del ctx.state_proposals[:]
        ctx.state_proposals.extend(proposals)
    if ctx.state.constraints != view["constraints"]:
        restored.append("state.constraints")
        ctx.state.constraints = copy.deepcopy(view["constraints"])
    if ctx.state.search_constraints != view["search_constraints"]:
        restored.append("state.search_constraints")
        ctx.state.search_constraints = copy.deepcopy(view["search_constraints"])
    return tuple(restored)


def _subject_is_new(before: ConversationStateV2, gate: ConversationStateV2, thread: str) -> bool:
    """„Primul tur al subiectului": subiectul porții (raft, tip) diferă de cel de dinaintea
    turului, sau nu există (în dubiu, ca `subject.subject_is_new`: comportamentul de azi). Un
    subiect RELUAT (`resume`) nu e nou: clientul l-a mai discutat, deci nu primește raftul vecin
    oferit ca la începutul unui subiect."""
    if thread == "resume":
        return False
    now = (gate.topic.category_key, gate.topic.product_type)
    if now == (None, None):
        return True
    return now != (before.topic.category_key, before.topic.product_type)


def _apply_turn_view(
    ctx: TurnContext, before: ConversationStateV2, gate: ConversationStateV2, thread: str
) -> None:
    """Al doilea sit declarat (§3): vederea de CITIRE a turului, pentru compunere. Obiectul
    `ctx.state` NU se înlocuiește (ar șterge scrierile de mai devreme din tur: contextul de
    siguranță, setul curățat, vederea lui `clarify_resume`); se copiază doar `VIEW_FIELDS`, din
    proiecția stării porții. Persistența trece prin `reduce_turn` (`kernel_commit`); cu scrierea v2
    aprinsă (cerută de flag), `_build_new_state` întoarce documentul v2 înainte să citească
    `ctx.state`. Chemat ÎNAINTEA executorilor (compunerea rulează în executor și o citește), după
    instantaneu, deci orice ieșire neservită o anulează."""
    view = ConversationState.from_jsonb(project_v1(gate))
    ctx.state.constraints = view.constraints
    ctx.state.search_constraints = view.search_constraints
    ctx.kernel_view = KernelView(
        gate_state=gate, subject_is_new=_subject_is_new(before, gate, thread)
    )


def _asked_in_reply(reply: Any, question: str | None) -> bool:
    """Cititorul DECLARAT al răspunsului servit, pentru memoria întrebării: întrebarea de
    confirmare a porții e chiar în textul trimis clientului (în `text` sau în `intro`-ul
    răspunsului bogat, unde o pune compunerea). Iese un bool; nimic nu ramifică pe cuvinte."""
    if reply is None or not question:
        return False
    rich = reply.rich
    shown = [reply.text or "", (rich.intro if rich is not None else "") or ""]
    return any(question in part for part in shown)


# --- cititorii declarați ai textului -------------------------------------------------------------


def _turn_words(ctx: TurnContext) -> tuple[str, tuple[tuple[str, str], ...]]:
    """Cititorul DECLARAT al textului turului: mesajul curent (brut, ca pentru agent, D6) și
    istoricul DINAINTEA lui (ultimul mesaj din `ctx.history` e cel curent, inserat de `turn_uow`),
    redactat la citire ca în `conversation_transcript` (NX-230). Nimic nu ramifică pe cuvinte:
    ies ca `InterpretInput`, iar adaptorul le randează."""
    prior = list(ctx.history[:-1]) if ctx.history else []
    history: list[tuple[str, str]] = []
    for m in prior:
        role = _ROLES.get(m.direction)
        body = (m.body or "").strip()
        if role is None or not body:
            continue
        history.append((role, make_safe(body).text))
    return (ctx.message.body or "").strip(), tuple(history)


def _redact(text: str) -> str:
    """Cititorul DECLARAT al frontierei NX-230 pentru trace: aceleași categorii ca
    `apply_boundary` (profilul `persist`), fail-safe (un detector picat dă un substituent, nu
    textul). Folosit doar de `redact_trace`."""
    return make_safe(text).text


# --- politicile, aceleași ca pe calea de azi ------------------------------------------------------


def reducer_policy(pack: Any | None) -> ReducerPolicy:
    """Politica reducerului, construită exact ca `processor._reducer_policy` (test de egalitate):
    starea porții trebuie să fie cea pe care o va scrie commit-ul."""
    s = get_settings()
    return ReducerPolicy(
        vocabulary=NeedVocabulary.from_pack(pack),
        sensitive_consent=s.conversation_sensitive_memory_enabled,
        max_clarification_attempts=s.clarify_max_attempts,
    )


def _gate_policy(pack: Any | None) -> ClarificationPolicy:
    """Pragurile porții, ca în `clarify_resume` (`stages/clarify.py`)."""
    s = get_settings()
    return ClarificationPolicy(
        vocabulary=NeedVocabulary.from_pack(pack),
        min_information_gain=s.clarification_min_information_gain,
        max_attempts_per_key=s.clarify_max_attempts,
    )


# --- lanțul (§2) ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Chain:
    interpreted: InterpretedTurn
    state: ConversationStateV2
    resolved: tuple[ResolvedRef, ...]
    delta: TurnDelta
    reduced: ReducedState
    outcome: GateOutcome
    policy: AnswerPolicy | None
    planned: PlannedTurn
    primary: str | None = None


class _VocabularyUnavailable(Exception):
    """Vocabularul turului nu s-a putut citi (recenzia, constatarea 7)."""


async def _load_input(ctx: TurnContext, deps: PipelineDeps) -> InterpretInput:
    """Citirile declarate de dinaintea apelului: vocabularul (cache-ul existent, cu eticheta
    kernelului și FĂRĂ degradarea la un vocabular gol) și meniul de rafturi, ambele cu
    `business_id` server-side. Ridică `_VocabularyUnavailable` / eroarea meniului (fallback fără
    apel)."""
    try:
        vocab = await get_vocabulary(deps, ctx.business.id, op=VOCABULARY_OP, fail_open=False)
    except Exception as e:  # noqa: BLE001 — orice eroare a citirii: vocabular indisponibil
        raise _VocabularyUnavailable from e
    async with deps.db(MENU_OP) as conn:
        menu = await list_category_menu(conn, ctx.business.id)
    message, history = _turn_words(ctx)
    return InterpretInput(
        locale=ctx.language,
        pack=getattr(ctx.business, "domain_pack", None),
        vocab=vocab,
        category_menu=tuple(menu),
        state=ctx.state_v2,
        history=history,
        message=message,
    )


def _answer_policy(
    interp: TurnInterpretation,
    resolved: tuple[ResolvedRef, ...],
    known: ReferenceFacts,
    outcome: GateOutcome,
    inp: InterpretInput,
) -> AnswerPolicy | None:
    """Politica de răspuns pe actul principal rămas după poartă. În PR A niciun executor n-a
    rulat, deci faptele sunt cele ale țintelor rezolvate (ca `fixture_catalog.kernel_step`)."""
    remaining = [a for i, a in enumerate(interp.acts) if i not in outcome.skipped_acts]
    if not remaining:
        return None
    act = remaining[-1]
    by_id = {r.ref_id: r for r in resolved}
    targeted = {
        pid: known.products[pid]
        for t in act.targets
        if t in by_id
        for pid in by_id[t].product_ids
        if pid in known.products
    }
    return answer_policy(
        act,
        resolved,
        targeted,
        vocab=inp.vocab,
        pack=inp.pack,
        references=interp.references,
        ambiguity=outcome.decision,
        locale=inp.locale,
    )


async def _chain(
    ctx: TurnContext, deps: PipelineDeps, inp: InterpretInput, interpreted: InterpretedTurn
) -> _Chain:
    interp = interpreted.interpretation
    validated = interpreted.validated
    assert interp is not None and validated is not None  # `outcome == "ok"`
    state = inp.state
    pack, vocab, locale = inp.pack, inp.vocab, inp.locale
    needs = NeedVocabulary.from_pack(pack)
    handles = handles_of(inp)
    sources = sources_from_state(state, interp.thread)
    page = page_source(ctx)
    if page is not None:
        sources = replace(sources, page=page)
    lookup = plan_lookup(
        interp.references,
        sources,
        pack=pack,
        locale=locale,
        extra_attributes=lookup_attributes(interp, vocab=vocab, pack=pack),
    )
    known = await fetch_reference_facts(deps, ctx.business.id, lookup, op=FACTS_OP)
    resolved = tuple(
        resolve_references(interp.references, sources, known, vocab=vocab, pack=pack, locale=locale)
    )
    delta = to_delta(
        interp,
        validated.checked,
        resolved,
        known,
        handles=handles,
        needs=needs,
        turn_id=ctx.turn_id,
    )
    # „Schimbări în tur" = propunerile de nevoi; închiderea întrebării nu e o schimbare (plannerul
    # adaugă singur `resume` și semnalele `inferred`, contractul v1.1).
    changed = bool(delta.proposals)
    answered = question_answered(state, delta.thread, ctx.turn_id)
    if answered is not None:
        delta = replace(delta, proposals=(answered, *delta.proposals))
    primary = interp.acts[-1].targets[0] if interp.acts and interp.acts[-1].targets else None
    reduced = reduce_turn(
        state,
        delta,
        (),
        resolved,
        primary,
        interp.corrects_previous_turn,
        reducer_policy(pack),
    )
    outcome = decide_ambiguity(
        interp,
        validated.checked,
        resolved,
        reduced.state,
        known,
        vocab=vocab,
        pack=pack,
        locale=locale,
        policy=_gate_policy(pack),
    )
    planned = plan_turn(
        interp,
        reduced.state,
        delta.ranking,
        resolved,
        outcome,
        changed=changed,
        pack=pack,
        vocab=vocab,
        locale=locale,
    )
    return _Chain(
        interpreted=interpreted,
        state=state,
        resolved=resolved,
        delta=delta,
        reduced=reduced,
        outcome=outcome,
        policy=_answer_policy(interp, resolved, known, outcome, inp),
        planned=planned,
        primary=primary,
    )


# --- traceul și evenimentele (§6) -----------------------------------------------------------------


def _delta_counters(chain: _Chain) -> dict[str, int]:
    """Contoarele deltei (vocabular închis, fără text) + ieșirile reducerului care nu sunt
    `applied` (parcare evacuată, reluare indisponibilă, corecție neconfirmată)."""
    counters = {k: int(v) for k, v in chain.delta.counters.items()}
    counters["proposals"] = len(chain.delta.proposals)
    counters["ranking"] = len(chain.delta.ranking)
    counters["rejected_changes"] = len(chain.delta.rejected)
    counters["reducer_rejected"] = len(chain.reduced.rejected)
    for record in chain.reduced.applied:
        if record.outcome != "applied":
            counters[record.outcome] = counters.get(record.outcome, 0) + 1
    return counters


def build_trace(chain: _Chain, turn_id: str, *, served: bool = False) -> KernelTrace:
    """Traceul lanțului. `state_after` = starea PORȚII (fără ecranul turului: starea de sfârșit
    de tur o scrie commit-ul); `executor` = executorul planului principal când turul e servit,
    altfel `NO_EXECUTOR`."""
    interp = chain.interpreted.interpretation
    validated = chain.interpreted.validated
    assert interp is not None and validated is not None
    planned = chain.planned
    memory = memory_proposal(chain.outcome, turn_id)
    return KernelTrace(
        contract_version=KERNEL_CONTRACT_VERSION,
        vocabulary_snapshot=chain.interpreted.vocabulary_snapshot,
        interpretation=interp,
        checked_changes=list(validated.checked),
        resolved_refs=list(chain.resolved),
        state_before=state_view(chain.state),
        proposals=[{"op": p.op, "key": p.key or p.category_key} for p in chain.delta.proposals],
        rejected=[{"op": r.op, "reason": r.reason} for r in chain.reduced.rejected],
        state_after=state_view(chain.reduced.state),
        ambiguity=chain.outcome.decision,
        plan=planned.plans[planned.primary],
        executor=planned.plans[planned.primary].executor if served else NO_EXECUTOR,
        answer_policy=chain.policy,
        plans=list(planned.plans),
        gaps=list(planned.gaps),
        disclosures=[(index, code) for index, code in planned.disclosures],
        dropped_acts=planned.dropped_acts,
        delta_counters=_delta_counters(chain),
        gate_memory={"op": memory.op, "key": memory.key} if memory is not None else None,
    )


Event = tuple[str, dict[str, Any]]


def _chain_record(
    ctx: TurnContext, chain: _Chain, *, served: bool = False
) -> tuple[dict[str, Any], list[Event]]:
    """Traceul stocat și evenimentele lanțului, CALCULATE fără să atingă contextul: se scriu
    abia când rezultatul e cunoscut (niciun set emis pe jumătate înaintea unei excepții)."""
    trace = cap_trace(redact_trace(build_trace(chain, ctx.turn_id, served=served), _redact))
    decision = chain.outcome.decision
    events: list[Event] = [
        ("ambiguity_decision", {"verdict": decision.verdict, "reason": decision.reason})
    ]
    if chain.policy is not None:
        events.append(("answer_policy", {"verdict_allowed": chain.policy.verdict_allowed}))
    events.append(("kernel_delta", _delta_counters(chain)))
    planned = chain.planned
    events.append(
        (
            "kernel_turn",
            {
                "served": served,
                "executor": planned.plans[planned.primary].executor,
                "plans": len(planned.plans),
                "fallback_reason": None if served else _DARK,
            },
        )
    )
    return trace.model_dump(mode="json"), events


def _record_fallback(ctx: TurnContext, reason: str, snapshot: str) -> None:
    if reason not in FALLBACK_REASONS:
        reason = _EXCEPTION
    ctx.trace["kernel_fallback"] = {"reason": reason, "vocabulary_snapshot": snapshot}
    ctx.emit("kernel_turn", served=False, executor=None, plans=0, fallback_reason=reason)


# --- executorii și commit-ul (PR B) ---------------------------------------------------------------


async def execute_plans(
    ctx: TurnContext, deps: PipelineDeps, planned: PlannedTurn, outcome: GateOutcome
) -> bool | None:
    """Seam-ul executorilor: rulează planurile turului (cu decizia porții, a cărei întrebare o pune
    executorul `ask` sau compunerea, la confirmare). `None` = niciun executor pentru plan (turul
    rămâne `dark`), `False` = executorul a refuzat, `True` = a servit. Compunerea rulează în
    executor, deci citește vederea de citire deja scrisă (`_apply_turn_view`).

    PR B: niciun executor nu e legat încă (citirile sunt ale PR-ului C, mutațiile ale PR-ului D),
    deci întoarce mereu `None` și ramura întoarce `False` în producție. Testele commit-ului îl
    înlocuiesc cu un executor sintetic."""
    return None


def _question_memory(ctx: TurnContext, outcome: GateOutcome) -> tuple[StateUpdateProposal, ...]:
    """Memoria întrebării, DOAR dacă întrebarea a fost chiar pusă: `set_pending_question` când
    răspunsul servit poartă întrebarea în așteptare (`ask`), `note_asked` când confirmarea e în
    textul trimis. Altfel anti-bucla ar număra o întrebare pe care clientul n-a văzut-o."""
    memory = memory_proposal(outcome, ctx.turn_id)
    if memory is None:
        return ()
    reply = ctx.reply
    if outcome.asked_kind == "pending":
        asked = reply is not None and bool(reply.pending_question)
    else:
        asked = _asked_in_reply(reply, outcome.decision.question)
    return (memory,) if asked else ()


def _executor_added(ctx: TurnContext, saved: ContextSnapshot) -> tuple[StateUpdateProposal, ...]:
    """Propunerile adăugate de executori în `ctx.state_proposals` în tur (I20: trec prin commit ca
    propuneri de executor, deci ce nu e referință sau sesiune se respinge CU înregistrare)."""
    before = saved.proposals
    now = list(ctx.state_proposals)
    if now[: len(before)] == before:
        return tuple(now[len(before) :])
    return tuple(p for p in now if p not in before)


def kernel_turn_of(chain: _Chain, ctx: TurnContext, saved: ContextSnapshot) -> KernelTurn:
    """Intrarea commit-ului (`kernel_commit.commit_kernel_turn`) din lanțul turului și din ce au
    făcut executorii: delta (cu `resolve_question` pe o întrebare vie, §1), referințele rezolvate,
    ținta principală, corecția, memoria întrebării (doar dacă a fost pusă) și propunerile
    executorilor."""
    interp = chain.interpreted.interpretation
    assert interp is not None
    return KernelTurn(
        delta=chain.delta,
        resolved=chain.resolved,
        primary=chain.primary,
        corrects_previous_turn=interp.corrects_previous_turn,
        memory=_question_memory(ctx, chain.outcome),
        executor_proposals=_executor_added(ctx, saved),
    )


_SERVED = "served"


async def _serve(
    ctx: TurnContext, deps: PipelineDeps, chain: _Chain, saved: ContextSnapshot
) -> str:
    """Vederea de citire, apoi executorii (care compun și o citesc). Întoarce `_SERVED`, `_DARK`
    (niciun executor) sau `_EXECUTOR_REFUSED`. Turul e servit doar dacă executorul a pus un
    răspuns NOU (P6: un `True` fără răspuns ar fi tăcere; un `False` CU răspuns e servit, §4); un
    răspuns rămas neschimbat de dinaintea ramurii nu contează."""
    _apply_turn_view(ctx, chain.state, chain.reduced.state, chain.delta.thread)
    verdict = await execute_plans(ctx, deps, chain.planned, chain.outcome)
    if verdict is None:
        return _DARK
    if ctx.reply is None or ctx.reply == saved.fields.get("reply"):
        return _EXECUTOR_REFUSED
    return _SERVED


# --- ramura ---------------------------------------------------------------------------------------


async def run_interpreted_turn(ctx: TurnContext, deps: PipelineDeps) -> bool:
    """Turul pe calea interpretată. Întoarce `True` doar când un executor a servit turul; atunci
    `ctx.kernel_turn` e scris (ultimul), iar commit-ul trece doar prin reducer. PR B: niciun
    executor nu e legat (`execute_plans` ⇒ `None`), deci în producție turul rămâne DARK și ramura
    întoarce `False` (calea v1 răspunde și persistă ca azi). Nu aruncă (P6): orice eșec e un
    `kernel_fallback{reason}`, iar contextul predat căii v1 e cel de dinaintea ramurii (o
    restaurare picată se numără, `kernel_restore_failed`, iar calea v1 continuă)."""
    try:
        saved = ContextSnapshot.take(ctx)
    except Exception as e:  # noqa: BLE001 — P6: fără instantaneu nu se atinge nimic
        log.warning("interpreted_turn: instantaneu (%s)", type(e).__name__)
        _record_fallback(ctx, _SNAPSHOT_ERROR, "")
        return False
    reason, snapshot, chain = _EXCEPTION, "", None
    try:
        inp = await _load_input(ctx, deps)
    except _VocabularyUnavailable:
        log.warning("interpreted_turn: vocabularul indisponibil")
        inp, reason = None, _VOCABULARY_UNAVAILABLE
    except Exception as e:  # noqa: BLE001 — P6: citirea de intrare picată ⇒ v1, zero apeluri
        log.warning("interpreted_turn: intrarea (%s)", type(e).__name__)
        inp = None
    if inp is not None:
        interpreted = await interpret_turn(deps.llm, inp, business_id=ctx.business.id)
        ctx.emit("turn_interpretation", **interpreted.event)
        reason, snapshot = interpreted.outcome, interpreted.vocabulary_snapshot
        if interpreted.outcome == "ok":
            try:
                chain = await _chain(ctx, deps, inp, interpreted)
            except Exception as e:  # noqa: BLE001 — P6: după apel, tot un fallback
                log.warning("interpreted_turn: lanțul (%s)", type(e).__name__)
                reason = _EXCEPTION
    served, record, kernel_turn = False, None, None
    if chain is not None:
        try:
            verdict = await _serve(ctx, deps, chain, saved)
            if verdict == _EXECUTOR_REFUSED:
                reason = _EXECUTOR_REFUSED
            else:
                served = verdict == _SERVED
                record = _chain_record(ctx, chain, served=served)
                kernel_turn = kernel_turn_of(chain, ctx, saved) if served else None
        except Exception as e:  # noqa: BLE001 — P6: un executor sau traceul picat ⇒ fallback
            log.warning("interpreted_turn: executorii/traceul (%s)", type(e).__name__)
            served, record, kernel_turn, reason = False, None, None, _EXCEPTION
    if not served:
        try:
            saved.restore(ctx)
        except Exception as e:  # noqa: BLE001 — P6: numărat, calea v1 continuă (declarat)
            log.warning("interpreted_turn: restaurarea (%s)", type(e).__name__)
            ctx.emit("kernel_restore_failed", error=type(e).__name__)
    if record is None:
        _record_fallback(ctx, reason, snapshot)
        return False
    trace, events = record
    ctx.trace["kernel"] = trace
    for name, properties in events:
        ctx.emit(name, **properties)
    if not served:
        return False
    # Ultimul: intrarea commit-ului există DOAR pe un tur servit (§2).
    ctx.kernel_turn = kernel_turn
    return True


__all__ = [
    "EXECUTOR_WRITABLE",
    "FACTS_OP",
    "FALLBACK_REASONS",
    "KERNEL_READS",
    "MENU_OP",
    "NO_EXECUTOR",
    "ORCHESTRATOR_WRITABLE",
    "VIEW_FIELDS",
    "VOCABULARY_OP",
    "ContextSnapshot",
    "KernelView",
    "build_trace",
    "execute_plans",
    "kernel_turn_of",
    "reducer_policy",
    "run_interpreted_turn",
]
