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

import asyncio
import copy
import logging
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from src.agent.deterministic import page_source
from src.agent.turn_planner import PlannedTurn, disclosure_memory, plan_turn
from src.catalog.reference_facts import facts_from_row, fetch_reference_facts
from src.catalog.subject_pairs import mark_pairs, needs_pairs, subject_type_pairs
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
from src.conversation.delta import TurnDelta, accepted_changes, to_delta
from src.conversation.interpretation import (
    KERNEL_CONTRACT_VERSION,
    AnswerPolicy,
    ResolvedRef,
    TurnInterpretation,
)
from src.conversation.interpretation_check import OUTCOMES
from src.conversation.kernel_trace import KernelTrace, cap_trace, redact_trace, state_view
from src.conversation.needs import UNMAPPED_KEY, NeedVocabulary
from src.conversation.references import (
    MUTATING_ACTS,
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
from src.conversation.state_v2 import ConversationStateV2, Topic, project_v1
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
    from collections.abc import Sequence

    from src.agent.kernel_executors import PolicyFor
    from src.worker.runner import PipelineDeps

log = logging.getLogger(__name__)

#: Motivele pentru care turul NU e servit de kernel (`kernel_fallback.reason`,
#: `kernel_turn.fallback_reason`). Vocabular ÎNCHIS: `outcome`-urile interpretării (fără `ok`),
#: `exception` (o citire sau un strat care aruncă), `vocabulary_unavailable` (vocabularul nu s-a
#: putut citi: fallback ÎNAINTEA apelului), `snapshot_error` (instantaneul contextului a picat),
#: `executor_refused` (niciun executor n-a servit turul), `no_sentence` (PR C) și `dark` (lanțul a
#: mers, dar niciun executor nu există încă: `execute_plans` întoarce `None` până la PR C).
#: NX-353: `dark_timeout` = plafonul NOSTRU pe interpretarea din modul dark (nu o eroare a
#: furnizorului, ca la NX-311: altă reparație).
FALLBACK_REASONS: tuple[str, ...] = (
    *(o for o in OUTCOMES if o != "ok"),
    "exception",
    "vocabulary_unavailable",
    "snapshot_error",
    "executor_refused",
    "no_sentence",
    "dark",
    "dark_timeout",
)
#: Etichetele operațiilor de DB ale kernelului: excepția declarată de la suprafața I16 (un tur căzut
#: pe v1 e identic cu flagul stins în afara lor). Toate trei sunt ale kernelului, cu nume proprii
#: (recenzia, constatarea 2): nicio citire a căii v1 nu se exclude din comparație.
MENU_OP = "kernel_category_menu"
FACTS_OP = "kernel_reference_facts"
VOCABULARY_OP = "kernel_load_vocabulary"
SUBJECT_PAIRS_OP = "kernel_subject_pairs"
KERNEL_READS: frozenset[str] = frozenset({MENU_OP, FACTS_OP, VOCABULARY_OP, SUBJECT_PAIRS_OP})
#: Traceul unui tur neservit (`dark`): niciun executor n-a rulat.
NO_EXECUTOR = "none"
_DARK = "dark"
_DARK_TIMEOUT = "dark_timeout"
_EXCEPTION = "exception"
_VOCABULARY_UNAVAILABLE = "vocabulary_unavailable"
_SNAPSHOT_ERROR = "snapshot_error"
_EXECUTOR_REFUSED = "executor_refused"
_NO_SENTENCE = "no_sentence"

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
    "safety_referral_composed",  # NX-382 faza 2c: propoziția verificată a compozitorului
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
    now = (gate.topic.category_key, gate.topic.product_type, gate.topic.type_umbrella)
    if not gate.topic.has_subject:
        return True
    if now == (before.topic.category_key, before.topic.product_type, before.topic.type_umbrella):
        return False
    # NX-348: o RAFINARE (completarea unei jumătăți goale) nu e subiect nou; reducerul păstrează
    # atunci revizia subiectului (recenzia NX-348, constatarea 5).
    return (
        gate.topic.changed_at_revision != before.topic.changed_at_revision
        or before.topic == Topic()
    )


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
    policy_for: PolicyFor | None = None


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


def _primary_act(interp: TurnInterpretation, outcome: GateOutcome) -> Any:
    remaining = [a for i, a in enumerate(interp.acts) if i not in outcome.skipped_acts]
    return remaining[-1] if remaining else None


def _policy_for(
    interp: TurnInterpretation,
    resolved: tuple[ResolvedRef, ...],
    outcome: GateOutcome,
    inp: InterpretInput,
) -> PolicyFor | None:
    """Politica de răspuns pe produsele unei COMPARAȚII (NX-336 C2, I12): aceeași ca
    `_answer_policy`, dar judecată DOAR pe rândurile arătate de executor (după siguranță), cu
    partenerul unei comparații cu o țintă (`partners`). Un candidat scos de siguranță nu e în
    tabel, deci nu are voie să rețină verdictul (recenzia C2)."""
    act = _primary_act(interp, outcome)
    if act is None:
        return None

    def policy_for(partners: Sequence[str], rows: Sequence[Mapping[str, Any]]) -> Any:
        shown = {str(r["id"]) for r in rows}
        facts = {str(r["id"]): facts_from_row(dict(r)) for r in rows}
        on_table = tuple(
            r.model_copy(update={"product_ids": [p for p in r.product_ids if p in shown]})
            for r in resolved
        )
        return answer_policy(
            act,
            on_table,
            facts,
            vocab=inp.vocab,
            pack=inp.pack,
            references=interp.references,
            ambiguity=outcome.decision,
            locale=inp.locale,
            partners=tuple(p for p in partners if p in shown),
        )

    return policy_for


#: Executorii care servesc o comparație sau detaliul unei ținte.
_TARGETED: frozenset[str] = frozenset({"compare", "detail"})


#: Executorii care SCRIU (I10: doar pe ținte `exact`).
_MUTATING: frozenset[str] = frozenset({"cart"})


#: Actele care nu cer nimic: rămase singure lângă o mutație scoasă de poartă, turul tot cerea doar
#: scrierea.
_NO_REQUEST: frozenset[str] = frozenset({"chitchat"})


def _mutating_turn(chain: _Chain) -> bool:
    """Turul a cerut o scriere: un act care SCRIE printre actele rămase după poartă, SAU o mutație
    scoasă de poartă (regula 0, I24) lângă care n-a rămas nicio altă cerere. Pe un `reply_only`
    răspunsul e atunci al kernelului, niciodată o buclă v1 cu `cart_add` (NX-383: «il iau pe ala cu
    acoperire mai mare» ⇒ ținta numea o proprietate, poarta a scos coșul cu verdictul `act`, iar
    bucla v1 a pus în coș un produs ales de model). O mutație scoasă lângă o cerere de citire lasă
    citirea să fie servită (dezvăluirea `invalid_target` spune restul)."""
    interp = chain.interpreted.interpretation
    if interp is None:
        return False
    skipped = set(chain.outcome.skipped_acts)
    kept = [a for i, a in enumerate(interp.acts) if i not in skipped]
    if any(a.kind in MUTATING_ACTS for a in kept):
        return True
    dropped = any(a.kind in MUTATING_ACTS for i, a in enumerate(interp.acts) if i in skipped)
    return dropped and all(a.kind in _NO_REQUEST for a in kept)


def _social_turn(chain: _Chain) -> bool:
    """NX-382 faza 2c: turul e DOAR `chitchat` (un salut, o mulțumire, un rămas-bun), fără niciun
    act scos de poartă. Pe `reply_only` îl răspunde compozitorul, nu bucla v1 (de acolo a venit
    abonarea la stoc pe «ok pa», NX-383)."""
    interp = chain.interpreted.interpretation
    if interp is None or chain.outcome.skipped_acts or not interp.acts:
        return False
    # recenzia 2c: «mersi, apropo am tenul gras» schimbă starea, iar un «da» spus unei oferte are
    # o referință; nu sunt doar un salut
    if interp.changes or interp.references:
        return False
    return all(a.kind in _NO_REQUEST for a in interp.acts)


def _dropped_request(chain: _Chain) -> bool:
    """Poarta a scos și o cerere care NU scrie (regula 0). Pe un refuz de mutație, dezvăluirea
    `invalid_target` o spune clientului; fără ea, refuzul coșului e tot răspunsul (NX-383)."""
    interp = chain.interpreted.interpretation
    if interp is None:
        return False
    skipped = set(chain.outcome.skipped_acts)
    return any(
        a.kind not in MUTATING_ACTS and a.kind not in _NO_REQUEST
        for i, a in enumerate(interp.acts)
        if i in skipped
    )


def _mutations_exact(chain: _Chain) -> bool:
    """I10 la rulare (D2): fiecare id al unei mutații vine dintr-o referință rezolvată `exact`.
    Plannerul pune în planul `cart` doar ținte `exact`, deci garda nu schimbă nimic azi; e plasa
    pentru ziua în care un rând de planner sau o poartă se schimbă (o mutație pe un candidat ambiguu
    ar scrie în coșul clientului un produs pe care nu l-a ales)."""
    exact = {p for r in chain.resolved if r.outcome == "exact" for p in r.product_ids}
    return all(
        set(plan.product_ids) <= exact for plan in chain.planned.plans if plan.executor in _MUTATING
    )


def _target_lost(chain: _Chain) -> bool:
    """Actul principal numește ≥ 2 ținte, dar planul (`compare`/`detail`) a rămas cu sub 2 produse:
    o țintă s-a pierdut FĂRĂ dezvăluire (`_read` din planner dezvăluie doar un nume negăsit sau o
    țintă `stale`). Servit, turul ar compara ancora cu un similar ales de cod sau ar arăta detaliul
    doar al uneia (recenzia C2, P1), deci rămâne `dark`: calea v1 răspunde."""
    interp = chain.interpreted.interpretation
    act = _primary_act(interp, chain.outcome) if interp is not None else None
    planned = chain.planned
    plan = planned.plans[planned.primary]
    return (
        act is not None
        and len(act.targets) >= 2
        and plan.executor in _TARGETED
        and len(plan.product_ids) < 2
    )


async def _with_pair_compatibility(
    deps: PipelineDeps,
    business_id: str,
    delta: TurnDelta,
    state: ConversationStateV2,
    vocab: Any,
) -> TurnDelta:
    """NX-348: propunerea de subiect primește perechea (raft, tip) VERIFICATĂ în catalog. Citirea
    (UN checkout, `business_id = $1`) se face doar când perechea are ambele jumătăți; picată ⇒ nicio
    pereche verificată (completarea devine schimbare de subiect: nevoile se parchează, nu se pierd),
    numărat `subject_pairs_unavailable` în contoarele deltei (trace + `kernel_delta`)."""
    if not needs_pairs(delta, state):
        return delta
    try:
        async with deps.db(SUBJECT_PAIRS_OP) as conn:
            rows = await subject_type_pairs(conn, business_id)
    except Exception:  # noqa: BLE001 — orice eroare a citirii: fără dovadă, numărat, nu tăcut
        counters = {**delta.counters, "subject_pairs_unavailable": 1}
        return replace(delta, counters=counters)
    return mark_pairs(delta, state, rows, vocab)


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
    delta = await _with_pair_compatibility(deps, ctx.business.id, delta, state, vocab)
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
        checked=accepted_changes(validated.checked, delta),
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
        policy_for=_policy_for(interp, resolved, outcome, inp),
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


def _record_fallback(ctx: TurnContext, reason: str, snapshot: str, *, dark: bool = False) -> None:
    if reason not in FALLBACK_REASONS:
        reason = _EXCEPTION
    ctx.trace["kernel_fallback"] = {"reason": reason, "vocabulary_snapshot": snapshot}
    # NX-353: `mode` doar în modul dark (servirea rămâne byte-identică): raportul separă populațiile
    mode = {"mode": "dark"} if dark else {}
    ctx.emit("kernel_turn", served=False, executor=None, plans=0, fallback_reason=reason, **mode)


# --- executorii și commit-ul (PR B) ---------------------------------------------------------------


async def execute_plans(
    ctx: TurnContext,
    deps: PipelineDeps,
    planned: PlannedTurn,
    outcome: GateOutcome,
    policy_for: PolicyFor | None = None,
    mutating: bool = False,
    dropped_request: bool = False,
    social: bool = False,
) -> bool | None:
    """Seam-ul executorilor: rulează planurile turului (cu decizia porții, a cărei întrebare o pune
    executorul `ask` sau compunerea, la confirmare). `None` = niciun executor pentru plan (turul
    rămâne `dark`), `False` = executorul a refuzat, `True` = a servit. Compunerea rulează în
    executor, deci citește vederea de citire deja scrisă (`_apply_turn_view`).

    PR C: executorii de CITIRE (`kernel_executors`, un singur plan), cu politica de răspuns
    judecată pe produsele comparate (`policy_for`, I12).
    Mutațiile, `bundle`, `delegate`, `faq`/`order` și planurile multiple întorc `None` până la PR D.
    `NoSentence` (fraza fail-closed lipsă din pachet) urcă la `_serve`."""
    from src.agent.kernel_executors import execute_read_plans  # noqa: PLC0415 — ciclul agent

    return await execute_read_plans(
        ctx, deps, planned, outcome, policy_for, mutating, dropped_request, social=social
    )


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


def _disclosure_memory(ctx: TurnContext, planned: PlannedTurn) -> tuple[StateUpdateProposal, ...]:
    """NX-374 (recenzia A2): memoria dezvăluirii `need_unverifiable`, DOAR dacă fraza pachetului e
    chiar în răspunsul trimis (ca memoria întrebării, `_asked_in_reply`). Un plan care n-a servit,
    o căutare fără rezultate sau o frază lipsă din pachet lasă nevoia de spus pe următoarea căutare
    care o poartă, în loc s-o marcheze spusă fără ca clientul s-o fi văzut."""
    if not planned.disclosed_needs:
        return ()
    from src.agent.kernel_executors import kernel_sentence  # noqa: PLC0415 — ciclul agent

    pack = getattr(ctx.business, "domain_pack", None)
    sentence = kernel_sentence(pack, ctx.language, "need_unverifiable")
    if not _asked_in_reply(ctx.reply, sentence):
        return ()
    return disclosure_memory(planned, ctx.turn_id)


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
        memory=(
            *_question_memory(ctx, chain.outcome),
            *_disclosure_memory(ctx, chain.planned),
        ),
        executor_proposals=_executor_added(ctx, saved),
    )


_SERVED = "served"


def _drop_executor_events(ctx: TurnContext, mark: int) -> None:
    """Pe un tur neservit DUPĂ ce un executor a rulat, evenimentele lui (căutarea, cererea
    neîmplinită, `tool_call`) se scot: calea v1 le emite din nou pe ale ei, iar dublura ar număra
    de două ori cererea. Rămân doar evenimentele declarate ale kernelului. Citirile de DB făcute de
    executor nu se pot anula (declarat, NX-336 PR C)."""
    from src.agent.kernel_executors import KERNEL_EXECUTOR_EVENTS  # noqa: PLC0415

    kept = [e for e in ctx.events[mark:] if e.type in KERNEL_EXECUTOR_EVENTS]
    del ctx.events[mark:]
    ctx.events.extend(kept)


async def _serve(
    ctx: TurnContext, deps: PipelineDeps, chain: _Chain, saved: ContextSnapshot
) -> str:
    """Vederea de citire, apoi executorii (care compun și o citesc). Întoarce `_SERVED`, `_DARK`
    (niciun executor) sau `_EXECUTOR_REFUSED`. Turul e servit doar dacă executorul a pus un
    răspuns NOU (P6: un `True` fără răspuns ar fi tăcere; un `False` CU răspuns e servit, §4); un
    răspuns rămas neschimbat de dinaintea ramurii nu contează."""
    from src.agent.kernel_executors import NoSentence  # noqa: PLC0415 — ciclul agent

    if _target_lost(chain):
        return _DARK
    if not _mutations_exact(chain):
        ctx.emit("kernel_i10_blocked")
        return _DARK
    _apply_turn_view(ctx, chain.state, chain.reduced.state, chain.delta.thread)
    try:
        verdict = await execute_plans(
            ctx,
            deps,
            chain.planned,
            chain.outcome,
            chain.policy_for,
            _mutating_turn(chain),
            _dropped_request(chain),
            _social_turn(chain),
        )
    except NoSentence as e:
        ctx.emit("kernel_sentence_missing", code=e.code)
        return _NO_SENTENCE
    if verdict is None:
        return _DARK
    if ctx.reply is None or ctx.reply == saved.fields.get("reply"):
        return _EXECUTOR_REFUSED
    return _SERVED


# --- ramura ---------------------------------------------------------------------------------------


def _dark_truth(state: ConversationStateV2) -> dict[str, Any]:
    """NX-353: ce a validat kernelul din cuvintele clientului, după reducer: tipul spus (sau
    codurile umbrelei), raftul și nevoile de fațetă active (`contains`, valori de vocabular). Sunt
    CHEI de catalog, nu text de client; raportul le folosește ca adevăr PROXY (declarat)."""
    topic = state.topic
    types = [topic.product_type] if topic.product_type else list(topic.type_umbrella)
    return {
        "types": types,
        "type_learned": bool(topic.type_learned),
        "shelf": topic.category_key,
        # `unmapped` poartă cuvintele clientului (recenzia NX-353): nu e o cheie de catalog
        "needs": [
            [n.key, n.normalized_value]
            for n in state.active_needs()
            if n.operator == "contains"
            and isinstance(n.normalized_value, str)
            and n.key != UNMAPPED_KEY
        ],
    }


def _dark_record(chain: _Chain) -> dict[str, Any]:
    """NX-353: înregistrarea dark a turului, înaintea căutării (executorul real, planurile,
    adevărul proxy, dacă starea v2 se scrie). O căutare picată își păstrează restul câmpurilor."""
    planned = chain.planned
    return {
        "executor": planned.plans[planned.primary].executor,
        "plans": len(planned.plans),
        "searched": False,
        "kernel_ids": [],
        "lexical_step": None,
        "ms": None,
        "error": None,
        "truth": _dark_truth(chain.reduced.state),
        # ce memorie a citit kernelul: fără scriere, e starea ținută de v1 (declarat)
        "state_v2_write": bool(
            getattr(get_settings(), "conversation_state_v2_write_enabled", False)
        ),
    }


async def _dark_search(
    ctx: TurnContext, deps: PipelineDeps, chain: _Chain, out: dict[str, Any]
) -> None:
    """NX-353 (pasul 7, DARK): ce ar fi servit kernelul, fără să servească. Pe un plan `search`
    principal rulează DOAR căutarea planificată (read-only, aceeași ca pe calea servită, fără
    compunere, deci fără al doilea apel de model) și reține primele `card_slots` id-uri în `out`.
    Zero text de client: id-uri de produs, treapta lexicală, ms."""
    from time import perf_counter  # noqa: PLC0415

    from src.tools.catalog_tools import run_planned_search  # noqa: PLC0415 — ciclul unelte

    plan = chain.planned.plans[chain.planned.primary]
    if plan.executor != "search" or plan.search_args is None:
        return
    mark = len(ctx.events)
    started = perf_counter()
    paging = chain.planned.primary in chain.planned.excludes_shown  # NX-370
    extra: dict[str, Any] = {"exclude_shown": True} if paging else {}
    if paging and get_settings().search_resume_excludes_subject_seen_enabled:  # NX-378: ca servita
        from src.agent.kernel_executors import seen_in_state  # noqa: PLC0415

        if seen := seen_in_state(chain.reduced.state):
            extra["seen_extra"] = seen
    result = await run_planned_search(ctx, deps, plan.search_args, **extra)
    out["ms"] = round((perf_counter() - started) * 1000, 1)
    slots = int(getattr(get_settings(), "card_slots", 6))
    ids = [str(p.get("product_id") or p.get("id")) for p in (result.products or [])]
    out["kernel_ids"] = [i for i in ids if i and i != "None"][:slots]
    search = next((e for e in ctx.events[mark:] if e.type == "product_search"), None)
    out["lexical_step"] = (search.properties or {}).get("lexical_step") if search else None
    out["searched"] = True


async def _interpret(
    deps: PipelineDeps, inp: InterpretInput, business_id: str, *, dark: bool
) -> InterpretedTurn | None:
    """Apelul de interpretare. În modul DARK are plafonul lui (`INTERPRETED_TURN_DARK_TIMEOUT_S`):
    clientul așteaptă răspunsul v1 după el, iar un furnizor blocat ar adăuga altfel până la
    `llm_call_total_cap_s` pe FIECARE tur, pentru un rezultat pe care nu-l folosește nimeni
    (recenzia NX-353). `None` = plafonul a expirat. Pe servire, neschimbat."""
    call = interpret_turn(deps.llm, inp, business_id=business_id)
    if not dark:
        return await call
    cap = float(getattr(get_settings(), "interpreted_turn_dark_timeout_s", 5.0))
    try:
        return await asyncio.wait_for(call, timeout=cap)
    except TimeoutError:
        return None


async def run_interpreted_turn(ctx: TurnContext, deps: PipelineDeps, *, dark: bool = False) -> bool:
    """Turul pe calea interpretată. Întoarce `True` doar când un executor a servit turul; atunci
    `ctx.kernel_turn` e scris (ultimul), iar commit-ul trece doar prin reducer. PR B: niciun
    executor nu e legat (`execute_plans` ⇒ `None`), deci în producție turul rămâne DARK și ramura
    întoarce `False` (calea v1 răspunde și persistă ca azi). Nu aruncă (P6): orice eșec e un
    `kernel_fallback{reason}`, iar contextul predat căii v1 e cel de dinaintea ramurii (o
    restaurare picată se numără, `kernel_restore_failed`, iar calea v1 continuă)."""
    dark_mode = dark
    try:
        saved = ContextSnapshot.take(ctx)
    except Exception as e:  # noqa: BLE001 — P6: fără instantaneu nu se atinge nimic
        log.warning("interpreted_turn: instantaneu (%s)", type(e).__name__)
        _record_fallback(ctx, _SNAPSHOT_ERROR, "", dark=dark_mode)
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
    interpreted = None
    if inp is not None:
        interpreted = await _interpret(deps, inp, ctx.business.id, dark=dark_mode)
        if interpreted is None:
            log.warning("interpreted_turn: plafonul interpretării dark a expirat")
            reason = _DARK_TIMEOUT
    if interpreted is not None:
        ctx.emit("turn_interpretation", **interpreted.event)
        reason, snapshot = interpreted.outcome, interpreted.vocabulary_snapshot
        if interpreted.outcome == "ok":
            try:
                chain = await _chain(ctx, deps, inp, interpreted)
            except Exception as e:  # noqa: BLE001 — P6: după apel, tot un fallback
                log.warning("interpreted_turn: lanțul (%s)", type(e).__name__)
                reason = _EXCEPTION
    served, record, kernel_turn, dark_record = False, None, None, None
    dark = False
    mark = len(ctx.events)
    if chain is not None and dark_mode:
        # NX-353: fără executori și fără scurtături; doar căutarea planificată, read-only
        dark = True
        try:
            dark_record = _dark_record(chain)
            await _dark_search(ctx, deps, chain, dark_record)
        except Exception as e:  # noqa: BLE001 — P6: căutarea picată se numără, v1 răspunde
            log.warning("interpreted_turn: căutarea dark (%s)", type(e).__name__)
            if dark_record is None:
                dark_record = {"executor": None, "searched": False, "kernel_ids": []}
            dark_record["searched"] = False
            dark_record["error"] = type(e).__name__
    elif chain is not None:
        try:
            verdict = await _serve(ctx, deps, chain, saved)
            if verdict in (_EXECUTOR_REFUSED, _NO_SENTENCE):
                reason = verdict
            else:
                served, dark = verdict == _SERVED, verdict == _DARK
        except Exception as e:  # noqa: BLE001 — P6: un executor picat ⇒ fallback
            log.warning("interpreted_turn: executorii (%s)", type(e).__name__)
            served, reason = False, _EXCEPTION
    if served:
        # Turul e SERVIT: un executor poate fi scris deja (coșul, NX-336 D2), deci de aici nimic nu
        # mai cade pe v1, care ar putea repeta mutația. Un trace sau o intrare de commit picate se
        # numără; fără `kernel_turn`, commit-ul e cel de azi (`reduce_all`), declarat.
        try:
            record = _chain_record(ctx, chain, served=True)
        except Exception as e:  # noqa: BLE001
            log.warning("interpreted_turn: traceul (%s)", type(e).__name__)
            ctx.emit("kernel_record_failed", error=type(e).__name__)
        try:
            kernel_turn = kernel_turn_of(chain, ctx, saved)
        except Exception as e:  # noqa: BLE001
            log.warning("interpreted_turn: intrarea commit-ului (%s)", type(e).__name__)
            ctx.emit("kernel_commit_input_failed", error=type(e).__name__)
        if record is not None:
            trace, events = record
            ctx.trace["kernel"] = trace
            for name, properties in events:
                ctx.emit(name, **properties)
        # Ultimul: intrarea commit-ului există DOAR pe un tur servit (§2).
        ctx.kernel_turn = kernel_turn
        return True
    if dark:
        try:
            record = _chain_record(ctx, chain, served=False)
        except Exception as e:  # noqa: BLE001 — P6: traceul picat ⇒ fallback fără trace
            log.warning("interpreted_turn: traceul (%s)", type(e).__name__)
            reason = _EXCEPTION
    _drop_executor_events(ctx, mark)
    try:
        saved.restore(ctx)
    except Exception as e:  # noqa: BLE001 — P6: numărat, calea v1 continuă (declarat)
        log.warning("interpreted_turn: restaurarea (%s)", type(e).__name__)
        ctx.emit("kernel_restore_failed", error=type(e).__name__)
    if record is None:
        chain_trace = None
        if chain is not None:
            # NX-387: un fallback DUPĂ lanț (executor refuzat, frază lipsă, excepție) își păstrează
            # traceul redactat, ca motivul să se poată citi (pe wide-2026-10-07 două ture
            # `executor_refused` aveau doar motivul). Sub cheia fallback-ului, nu `kernel` (aceea
            # înseamnă un lanț încheiat), iar evenimentele rămân cele de azi.
            try:
                built = cap_trace(redact_trace(build_trace(chain, ctx.turn_id), _redact))
                chain_trace = built.model_dump(mode="json")
            except Exception as e:  # noqa: BLE001 — P6: traceul e diagnostic, turul continuă
                log.warning("interpreted_turn: traceul fallback-ului (%s)", type(e).__name__)
        _record_fallback(ctx, reason, snapshot, dark=dark_mode)
        if chain_trace is not None:
            ctx.trace["kernel_fallback"]["chain"] = chain_trace
        return False
    trace, events = record
    ctx.trace["kernel"] = trace
    for name, properties in events:
        if dark_mode and name == "kernel_turn":
            properties = {**properties, "mode": "dark"}
        ctx.emit(name, **properties)
    if dark_record is not None:
        # NX-353: ce ar fi servit kernelul (id-uri de produs, fără text de client); raportul îl
        # compară cu setul servit de v1 pe același tur (`conversation_traces.recommended`).
        ctx.trace["kernel_dark"] = dark_record
        ctx.emit(
            "kernel_dark",
            executor=dark_record.get("executor"),
            searched=bool(dark_record.get("searched")),
            kernel_n=len(dark_record.get("kernel_ids") or ()),
            lexical_step=dark_record.get("lexical_step"),
            error=dark_record.get("error"),
        )
    return False


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
