"""NX-235 — reducerul: SINGURUL loc care are voie să schimbe starea conversației.

Până acum starea se schimba oriunde: `clarify` scria în `constraints`, agentul rula
`merge_constraints`, tool-urile împingeau prin `state_patch`, iar processorul le împăca la
scriere. Fiecare cale avea propriile reguli, deci întrebarea „poate modelul să relaxeze o
constrângere hard?" nu avea un răspuns în cod — avea mai multe.

Aici e un singur răspuns. Stagiile nu mai SCRIU stare, ci PROPUN (`StateUpdateProposal`, operații
allowlistate); reducerul validează și aplică, sau respinge typed. Fiind pur și determinist, e și
singurul lucru care se poate re-aplica în siguranță pe o stare proaspătă la conflict optimistic —
fără să rerulăm modelul și fără să repetăm un efect deja produs.

Invariantele pe care le apără (și pe care le demonstrează testele, nu comentariile):

  • o inferență de model nu devine niciodată `hard` și nu poate rescrie un `hard` existent (D7);
  • o corecție lasă TOMBSTONE — faptul retras nu poate reveni din istoric/rezumat, fiindcă doar
    clientul (`user_explicit`/`action`) are dreptul să-l reafirme;
  • siguranța (NX-173) nu se revocă decât explicit de client; nici topic switch, nici model;
  • maximum O întrebare în așteptare, iar aceeași cheie nu se re-întreabă la nesfârșit;
  • schimbarea de subiect retrage NUMAI nevoile legate de vechea categorie, nu faptele despre om.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from src.conversation.needs import (
    HARD,
    SOFT,
    NeedVocabulary,
    norm_key,
    normalize_need,
    value_fingerprint,
)
from src.conversation.state_v2 import (
    HARD_CAPABLE_SOURCES,
    MAX_REVOCATIONS,
    REVIVE_CAPABLE_SOURCES,
    AskedQuestion,
    ConversationStateV2,
    DisplayedRef,
    Need,
    PendingClarification,
    References,
    Revocation,
    Topic,
    bounded_map,
    enforce_caps,
)

ProposalOp = Literal[
    "set_need",
    "confirm",
    "revoke",
    "supersede",
    "set_topic",
    "set_pending_question",
    "resolve_question",
    "set_references",
    "set_active_search",
    "set_cart_ref",
]

ALLOWED_OPS: frozenset[str] = frozenset(
    {
        "set_need",
        "confirm",
        "revoke",
        "supersede",
        "set_topic",
        "set_pending_question",
        "resolve_question",
        "set_references",
        "set_active_search",
        "set_cart_ref",
    }
)

# Motivele de respingere — vocabular ÎNCHIS (intră în `need_update_rejected{reason}`, deci trebuie
# low-cardinality; P10/P12: niciodată cheia sau valoarea în label).
REJECT_REASONS: frozenset[str] = frozenset(
    {
        "unknown_op",
        "unknown_key",
        "topic_key",
        "invalid_payload",
        "hard_downgrade",
        "revoked_key",
        "safety_immutable",
        "sensitive_no_consent",
        "already_pending",
        "already_asked",
    }
)


@dataclass(frozen=True)
class StateUpdateProposal:
    """O propunere TYPED de schimbare. Stagiile o construiesc; nimeni nu scrie direct în stare.

    `source` e cine afirmă, nu cine a apelat: un fapt extras de model din propoziția clientului
    rămâne `model_inferred` — altfel eticheta ar deveni o formalitate și `hard` ar fi la un
    argument distanță."""

    op: ProposalOp
    key: str | None = None
    value: Any = None
    strength: str | None = None
    source: str = "model_inferred"
    turn_id: str | None = None
    reason_code: str = "user_explicit"
    sensitive_class: str | None = None
    # set_topic
    category_key: str | None = None
    goal: str | None = None
    # set_pending_question / resolve_question
    question_id: str | None = None
    reason: str | None = None
    options_refs: tuple[str, ...] = ()
    expires_after_turns: int = 3
    resume_route: str | None = None
    # set_references / set_active_search / set_cart_ref
    payload: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class RejectedUpdate:
    """Respingere TYPED. `reason` e din vocabularul închis de mai sus — ajunge în metrici."""

    op: str
    reason: str
    key: str | None = None
    source: str = "model_inferred"


@dataclass(frozen=True)
class Applied:
    """Ce s-a aplicat efectiv — materia primă a lui `need_update{operation,strength,source,outcome}`
    (fără cheie/valoare brută în labels)."""

    op: str
    key: str | None = None
    strength: str = SOFT
    source: str = "model_inferred"
    outcome: str = "applied"  # applied | unchanged | superseded | revoked | reset


@dataclass(frozen=True)
class ReducerPolicy:
    """Politica sub care rulează reducerul. Toate deciziile care ar putea fi „configurabile prin
    prompt" sunt AICI, în cod, ca modelul să nu le poată muta."""

    vocabulary: NeedVocabulary = field(default_factory=NeedVocabulary)
    # Consimțământ pentru fapte sensibile (NX-230). Fără el, un fapt cu `sensitive_class` NU se
    # persistă — turul îl poate folosi request-scoped, memoria nu-l primește.
    sensitive_consent: bool = False
    # Câte încercări pe ACEEAȘI cheie înainte să nu mai întrebăm (anti-buclă).
    max_clarification_attempts: int = 2
    # `True` doar în teste/replay: lasă modelul să propună `hard`. Niciodată în runtime (D7).
    allow_model_hard: bool = False


@dataclass(frozen=True)
class ReducedState:
    """Rezultatul unui LOT de propuneri (un tur = un lot = o revizie)."""

    state: ConversationStateV2
    applied: tuple[Applied, ...] = ()
    rejected: tuple[RejectedUpdate, ...] = ()


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


def reduce(
    state: ConversationStateV2, proposal: StateUpdateProposal, policy: ReducerPolicy
) -> ConversationStateV2 | RejectedUpdate:
    """Aplică O propunere. PUR: aceeași intrare → aceeași ieșire, fără ceas și fără random.

    Întoarce starea NOUĂ sau `RejectedUpdate`. Nu ridică: o propunere stricată e o respingere
    typed, nu o excepție care ar rupe turul (P6)."""
    outcome = _reduce_one(state, proposal, policy)
    return outcome[0] if isinstance(outcome, tuple) else outcome


def reduce_all(
    state: ConversationStateV2,
    proposals: Sequence[StateUpdateProposal],
    policy: ReducerPolicy,
    *,
    revision: int | None = None,
) -> ReducedState:
    """Aplică un lot în ORDINE, la o singură revizie nouă.

    Un tur = un lot = o revizie. Contorul e al DOCUMENTULUI (implicit `state.revision + 1`), nu al
    coloanei `state_version`: la re-aplicare pe o stare proaspătă citim revizia din documentul
    proaspăt, deci n-avem nevoie de un al doilea contor care ar putea rămâne în urmă. Așa o
    referință „lista de la revizia 7" e verificabilă contra a ceea ce s-a scris efectiv.

    La conflict optimistic, apelantul cheamă din nou `reduce_all` cu ACELEAȘI propuneri pe starea
    proaspăt citită: rezultatul e re-derivat, nu re-cerut modelului."""
    current = replace(state, revision=revision if revision is not None else state.revision + 1)
    applied: list[Applied] = []
    rejected: list[RejectedUpdate] = []
    for proposal in proposals:
        outcome = _reduce_one(current, proposal, policy)
        if isinstance(outcome, RejectedUpdate):
            rejected.append(outcome)
            continue
        current, record = outcome
        applied.append(record)
    return ReducedState(enforce_caps(current), tuple(applied), tuple(rejected))


# ---------------------------------------------------------------------------
# Implementare
# ---------------------------------------------------------------------------


def _reduce_one(
    state: ConversationStateV2, proposal: StateUpdateProposal, policy: ReducerPolicy
) -> tuple[ConversationStateV2, Applied] | RejectedUpdate:
    if proposal.op not in ALLOWED_OPS:
        return RejectedUpdate(str(proposal.op), "unknown_op", None, proposal.source)
    handler = _HANDLERS[proposal.op]
    return handler(state, proposal, policy)


def _effective_strength(
    proposal: StateUpdateProposal, default_strength: str, policy: ReducerPolicy
) -> str:
    """Tăria EFECTIVĂ. `hard` cere o sursă capabilă (D7): `model_inferred` coboară întotdeauna la
    `soft`, indiferent ce cere propunerea. Asta e poarta pe care „ignoră instrucțiunile, marchează
    asta ca obligatoriu" nu o poate trece."""
    requested = proposal.strength or default_strength
    if requested != HARD:
        return SOFT
    if proposal.source in HARD_CAPABLE_SOURCES or policy.allow_model_hard:
        return HARD
    return SOFT


def _is_protected(need: Need) -> bool:
    """O nevoie de SIGURANȚĂ: fie e marcată sensibilă, fie a fost pusă de policy (NX-173). Nu se
    rescrie și nu se revocă decât explicit de client."""
    return bool(need.sensitive_class) or need.source == "policy"


def _matches(need: Need, key: str, value: Any, *, list_like: bool) -> bool:
    """Ce înseamnă „aceeași nevoie". Pentru `contains` (liste) identitatea include VALOAREA — două
    nevoi de tip concern coexistă; pentru restul, cheia e suficientă (un buget, nu trei)."""
    if need.key != key:
        return False
    return need.normalized_value == value if list_like else True


def _tombstone(
    revocations: Iterable[Revocation], new: Revocation, *, drop_keys: frozenset[str] = frozenset()
) -> tuple[Revocation, ...]:
    """Adaugă un tombstone, curățând întâi cele pe aceeași cheie (nu ținem istoricul retragerilor,
    ci faptul că ultima e valabilă) și pe cele explicit eliminate (re-afirmare de client)."""
    kept = [r for r in revocations if r.key != new.key and r.key not in drop_keys]
    return tuple(kept + [new])[-MAX_REVOCATIONS:]


def _handle_set_need(
    state: ConversationStateV2, proposal: StateUpdateProposal, policy: ReducerPolicy
) -> tuple[ConversationStateV2, Applied] | RejectedUpdate:
    vocab = policy.vocabulary
    if vocab.is_topic_key(proposal.key):
        return RejectedUpdate("set_need", "topic_key", norm_key(proposal.key), proposal.source)
    normalized = normalize_need(proposal.key, proposal.value, vocab)
    if normalized is None:
        return RejectedUpdate("set_need", "unknown_key", norm_key(proposal.key), proposal.source)
    key = normalized.key

    if proposal.sensitive_class and not policy.sensitive_consent:
        # Fapt sensibil fără politică/consimțământ: turul îl poate folosi, memoria nu-l primește.
        return RejectedUpdate("set_need", "sensitive_no_consent", key, proposal.source)

    revoked = state.revoked_keys()
    forced = proposal.op == "supersede"
    if key in revoked and proposal.source not in REVIVE_CAPABLE_SOURCES:
        # AICI se închide bucla: rezumatul/istoricul/modelul nu pot reînvia un fapt retras.
        return RejectedUpdate("set_need", "revoked_key", key, proposal.source)

    list_like = normalized.operator == "contains"
    existing = next(
        (
            n
            for n in state.needs
            if n.is_active and _matches(n, key, normalized.value, list_like=list_like)
        ),
        None,
    )
    same_key_active = next((n for n in state.needs if n.is_active and n.key == key), None)

    if (
        same_key_active is not None
        and _is_protected(same_key_active)
        and proposal.source
        not in (
            "user_explicit",
            "policy",
        )
    ):
        return RejectedUpdate("set_need", "safety_immutable", key, proposal.source)

    if (
        same_key_active is not None
        and same_key_active.strength == HARD
        and same_key_active.normalized_value != normalized.value
        and proposal.source not in HARD_CAPABLE_SOURCES
        and not forced
    ):
        # „Modelul propune să șteargă un hard constraint" → reject + eveniment (failure matrix).
        return RejectedUpdate("set_need", "hard_downgrade", key, proposal.source)

    strength = _effective_strength(proposal, normalized.default_strength, policy)
    scope = state.topic.category_key if normalized.scoped else None
    fresh = Need(
        key=key,
        operator=normalized.operator,
        normalized_value=normalized.value,
        strength=strength,
        status="active" if normalized.value is not None else "unknown",
        source=proposal.source,
        source_turn_id=proposal.turn_id,
        confirmed=bool(proposal.source == "user_explicit" and normalized.value is not None),
        sensitive_class=proposal.sensitive_class,
        updated_revision=state.revision,
        scope=scope,
    )

    if existing is not None and existing.normalized_value == normalized.value:
        # Idempotent: aceeași valoare reafirmată doar întărește (confirmare + revizie), nu duplică.
        needs = tuple(
            replace(
                n,
                confirmed=n.confirmed or fresh.confirmed,
                strength=HARD if HARD in (n.strength, strength) else SOFT,
                updated_revision=state.revision,
            )
            if n is existing
            else n
            for n in state.needs
        )
        return (
            replace(state, needs=needs, revocations=_drop_revocations(state, key, proposal)),
            Applied("set_need", key, strength, proposal.source, "unchanged"),
        )

    revocations = _drop_revocations(state, key, proposal)
    outcome = "applied"
    if same_key_active is not None and not list_like:
        # CORECȚIE: valoarea veche devine `superseded` ȘI primește tombstone — nu dispare tăcut.
        revocations = _tombstone(
            revocations,
            Revocation(
                key=key,
                prior_value_fingerprint=value_fingerprint(same_key_active.normalized_value),
                source_turn_id=proposal.turn_id,
                revision=state.revision,
                reason_code="superseded",
            ),
        )
        outcome = "superseded"

    needs = tuple(
        replace(n, status="superseded", updated_revision=state.revision)
        if (n is same_key_active and not list_like) or n is existing
        else n
        for n in state.needs
    )
    return (
        replace(state, needs=(*needs, fresh), revocations=revocations),
        Applied("set_need", key, strength, proposal.source, outcome),
    )


def _drop_revocations(
    state: ConversationStateV2, key: str, proposal: StateUpdateProposal
) -> tuple[Revocation, ...]:
    """Clientul reafirmă explicit o cheie retrasă → tombstone-ul ei dispare (altfel n-ar mai putea
    reveni niciodată la un fapt pe care l-a retras din greșeală)."""
    if proposal.source not in REVIVE_CAPABLE_SOURCES:
        return state.revocations
    return tuple(r for r in state.revocations if r.key != key)


def _handle_supersede(
    state: ConversationStateV2, proposal: StateUpdateProposal, policy: ReducerPolicy
) -> tuple[ConversationStateV2, Applied] | RejectedUpdate:
    """Corecție EXPLICITĂ. Doar clientul (sau o acțiune semnată) o poate cere: altfel modelul ar
    avea o poartă laterală ca să înlocuiască un `hard`."""
    if proposal.source not in REVIVE_CAPABLE_SOURCES:
        return RejectedUpdate(
            "supersede", "hard_downgrade", norm_key(proposal.key), proposal.source
        )
    result = _handle_set_need(state, replace(proposal, op="supersede"), policy)
    if isinstance(result, RejectedUpdate):
        return replace(result, op="supersede")
    new_state, record = result
    return new_state, replace(record, op="supersede", outcome="superseded")


def _handle_confirm(
    state: ConversationStateV2, proposal: StateUpdateProposal, policy: ReducerPolicy
) -> tuple[ConversationStateV2, Applied] | RejectedUpdate:
    key = norm_key(proposal.key)
    target = next((n for n in state.needs if n.is_active and n.key == key), None)
    if target is None:
        return RejectedUpdate("confirm", "unknown_key", key, proposal.source)
    needs = tuple(
        replace(n, confirmed=True, updated_revision=state.revision) if n is target else n
        for n in state.needs
    )
    return (
        replace(state, needs=needs),
        Applied("confirm", key, target.strength, proposal.source, "applied"),
    )


def _handle_revoke(
    state: ConversationStateV2, proposal: StateUpdateProposal, policy: ReducerPolicy
) -> tuple[ConversationStateV2, Applied] | RejectedUpdate:
    key = norm_key(proposal.key)
    if not key:
        return RejectedUpdate("revoke", "unknown_key", None, proposal.source)
    targets = [n for n in state.needs if n.is_active and n.key == key]
    if any(_is_protected(n) for n in targets) and not (
        proposal.source == "user_explicit" and proposal.reason_code == "user_explicit"
    ):
        # Siguranța nu expiră accidental și nu cade la un topic switch (NX-173).
        return RejectedUpdate("revoke", "safety_immutable", key, proposal.source)

    prior = targets[0].normalized_value if targets else proposal.value
    needs = tuple(
        replace(n, status="revoked", updated_revision=state.revision) if n in targets else n
        for n in state.needs
    )
    reason = (
        proposal.reason_code
        if proposal.reason_code in {"user_explicit", "policy"}
        else "user_explicit"
    )
    revocations = _tombstone(
        state.revocations,
        Revocation(
            key=key,
            prior_value_fingerprint=value_fingerprint(prior) if prior is not None else None,
            source_turn_id=proposal.turn_id,
            revision=state.revision,
            reason_code=reason,
        ),
    )
    return (
        replace(state, needs=needs, revocations=revocations),
        Applied("revoke", key, SOFT, proposal.source, "revoked"),
    )


def _handle_set_topic(
    state: ConversationStateV2, proposal: StateUpdateProposal, policy: ReducerPolicy
) -> tuple[ConversationStateV2, Applied] | RejectedUpdate:
    category = norm_key(proposal.category_key) or None
    if category is None and proposal.goal is None:
        return RejectedUpdate("set_topic", "invalid_payload", None, proposal.source)
    previous = state.topic.category_key
    if category == previous:
        topic = replace(state.topic, goal=proposal.goal or state.topic.goal)
        return (
            replace(state, topic=topic),
            Applied("set_topic", category, SOFT, proposal.source, "unchanged"),
        )

    topic = Topic(category_key=category, goal=proposal.goal, changed_at_revision=state.revision)
    if previous is None:
        # Prima ancorare a subiectului nu retrage nimic — nu exista un „vechi" de resetat.
        return (
            replace(state, topic=topic),
            Applied("set_topic", category, SOFT, proposal.source, "applied"),
        )

    scoped_keys = {
        n.key for n in state.needs if n.is_active and n.scope == previous and not _is_protected(n)
    }
    needs = tuple(
        replace(n, status="superseded", updated_revision=state.revision)
        if n.is_active and n.scope == previous and not _is_protected(n)
        else n
        for n in state.needs
    )
    revocations = state.revocations
    for key in sorted(scoped_keys):
        revocations = _tombstone(
            revocations,
            Revocation(
                key=key,
                prior_value_fingerprint=None,
                source_turn_id=proposal.turn_id,
                revision=state.revision,
                reason_code="topic_reset",
            ),
        )
    # Întrebarea în așteptare era despre subiectul abandonat; la fel sloturile deja întrebate care
    # țin de categorie — după schimbare au voie să fie întrebate din nou.
    asked = tuple(q for q in state.asked_questions if not _scoped_key(q.key, policy.vocabulary))
    return (
        replace(
            state,
            topic=topic,
            needs=needs,
            revocations=revocations,
            pending_clarification=None,
            asked_questions=asked,
        ),
        Applied("set_topic", category, SOFT, proposal.source, "reset"),
    )


def _scoped_key(key: str, vocab: NeedVocabulary) -> bool:
    spec = vocab.spec_for(key)
    return bool(spec and spec.scoped)


def _handle_set_pending_question(
    state: ConversationStateV2, proposal: StateUpdateProposal, policy: ReducerPolicy
) -> tuple[ConversationStateV2, Applied] | RejectedUpdate:
    key = norm_key(proposal.key)
    if not key:
        return RejectedUpdate("set_pending_question", "invalid_payload", None, proposal.source)
    pending = state.pending_clarification
    if pending is not None and not pending.expired_at(state.revision):
        # MAXIMUM o întrebare în așteptare — invariantul „o singură clarificare pe tur".
        return RejectedUpdate("set_pending_question", "already_pending", key, proposal.source)
    asked = state.asked(key)
    if asked is not None and asked.attempts >= policy.max_clarification_attempts:
        # Aceeași cheie, deja întrebată de destule ori → best effort, nu buclă.
        return RejectedUpdate("set_pending_question", "already_asked", key, proposal.source)

    attempts = (asked.attempts + 1) if asked else 1
    fresh = PendingClarification(
        question_id=proposal.question_id or f"q:{key}:{state.revision}:{attempts}",
        target_key=key,
        reason=proposal.reason or "missing_required",
        options_refs=tuple(proposal.options_refs)[:6],
        asked_at_revision=state.revision,
        expires_after_turns=max(1, proposal.expires_after_turns),
        attempts=attempts,
        resume_route=proposal.resume_route,
    )
    return (
        replace(state, pending_clarification=fresh),
        Applied("set_pending_question", key, SOFT, proposal.source, "applied"),
    )


def _handle_resolve_question(
    state: ConversationStateV2, proposal: StateUpdateProposal, policy: ReducerPolicy
) -> tuple[ConversationStateV2, Applied] | RejectedUpdate:
    pending = state.pending_clarification
    key = norm_key(proposal.key) or (pending.target_key if pending else "")
    if not key:
        return RejectedUpdate("resolve_question", "invalid_payload", None, proposal.source)
    attempts = pending.attempts if pending and pending.target_key == key else 1
    previous = state.asked(key)
    asked = tuple(q for q in state.asked_questions if q.key != key) + (
        AskedQuestion(key, state.revision, max(attempts, previous.attempts if previous else 1)),
    )
    return (
        replace(state, pending_clarification=None, asked_questions=asked),
        Applied("resolve_question", key, SOFT, proposal.source, "applied"),
    )


def _handle_set_references(
    state: ConversationStateV2, proposal: StateUpdateProposal, policy: ReducerPolicy
) -> tuple[ConversationStateV2, Applied] | RejectedUpdate:
    payload = proposal.payload if isinstance(proposal.payload, Mapping) else None
    if payload is None:
        return RejectedUpdate("set_references", "invalid_payload", None, proposal.source)
    current = state.references
    displayed = current.displayed_products
    revision = current.displayed_revision
    if "displayed_products" in payload:
        fresh = tuple(
            d
            for d in (DisplayedRef.from_jsonb(x) for x in (payload.get("displayed_products") or []))
            if d is not None
        )
        if [d.product_id for d in fresh] != [d.product_id for d in displayed]:
            # Lista s-a schimbat ⇒ revizie nouă. Un ordinal emis peste lista veche devine STALE,
            # în loc să selecteze tăcut alt produs (failure matrix).
            revision = state.revision
        displayed = fresh
    references = References(
        selected_product=_ref(payload, "selected_product", current.selected_product),
        page_product=_ref(payload, "page_product", current.page_product),
        displayed_products=displayed,
        displayed_revision=revision,
        compared_products=(
            tuple(str(c)[:64] for c in (payload.get("compared_products") or []) if c)
            if "compared_products" in payload
            else current.compared_products
        ),
        last_action=_ref(payload, "last_action", current.last_action),
    )
    return (
        replace(state, references=references),
        Applied("set_references", None, SOFT, proposal.source, "applied"),
    )


def _ref(payload: Mapping[str, Any], key: str, current: str | None) -> str | None:
    if key not in payload:
        return current
    value = payload.get(key)
    return str(value)[:64] if value else None


def _handle_set_active_search(
    state: ConversationStateV2, proposal: StateUpdateProposal, policy: ReducerPolicy
) -> tuple[ConversationStateV2, Applied] | RejectedUpdate:
    payload = proposal.payload
    if payload is not None and not isinstance(payload, Mapping):
        return RejectedUpdate("set_active_search", "invalid_payload", None, proposal.source)
    return (
        replace(state, active_search=bounded_map(dict(payload) if payload else None)),
        Applied("set_active_search", None, SOFT, proposal.source, "applied"),
    )


def _handle_set_cart_ref(
    state: ConversationStateV2, proposal: StateUpdateProposal, policy: ReducerPolicy
) -> tuple[ConversationStateV2, Applied] | RejectedUpdate:
    """Doar REFERINȚA coșului (id + versiune). Liniile și totalurile rămân ale NX-237 — starea nu
    devine un al doilea coș care poate diverge de cel real."""
    payload = proposal.payload
    if payload is not None and not isinstance(payload, Mapping):
        return RejectedUpdate("set_cart_ref", "invalid_payload", None, proposal.source)
    cart_ref = None
    if payload:
        cart_ref = {
            "ref": str(payload.get("ref") or "")[:64] or None,
            "version": int(payload.get("version") or 0),
        }
    return (
        replace(state, cart_ref=cart_ref),
        Applied("set_cart_ref", None, SOFT, proposal.source, "applied"),
    )


_HANDLERS: Mapping[str, Any] = {
    "set_need": _handle_set_need,
    "supersede": _handle_supersede,
    "confirm": _handle_confirm,
    "revoke": _handle_revoke,
    "set_topic": _handle_set_topic,
    "set_pending_question": _handle_set_pending_question,
    "resolve_question": _handle_resolve_question,
    "set_references": _handle_set_references,
    "set_active_search": _handle_set_active_search,
    "set_cart_ref": _handle_set_cart_ref,
}


__all__ = [
    "ALLOWED_OPS",
    "REJECT_REASONS",
    "Applied",
    "ProposalOp",
    "ReducedState",
    "ReducerPolicy",
    "RejectedUpdate",
    "StateUpdateProposal",
    "reduce",
    "reduce_all",
]
