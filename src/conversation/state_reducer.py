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
  • schimbarea de subiect retrage NUMAI nevoile legate de vechea categorie, nu faptele despre om;
  • (NX-331) nevoile retrase la o schimbare de subiect se PARCHEAZĂ, un nivel, cu ultimul set
    arătat; `resume` face schimbul cu slotul parcat (I4, I19), `aside` nu schimbă nimic (I5);
  • (NX-331) ieșirea executorilor scrie doar referințe și `active_search` (I20), iar
    `corrects_previous_turn` retrage ceva doar când o schimbare contrazice turul anterior (I21).

`reduce_all` e calea de azi (`processor._build_state_v2`). `reduce_turn` e punctul de intrare al
turului INTERPRETAT (contractul kernelului, „Reducer, thread and parking"): îl cheamă replay-ul și
testele până la pasul 6.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Literal

from src.conversation.needs import (
    HARD,
    MAX_UNMAPPED_PER_TOPIC,
    SOFT,
    UNMAPPED_KEY,
    NeedVocabulary,
    norm_key,
    normalize_need,
    value_fingerprint,
)
from src.conversation.provenance import dimension_of_key
from src.conversation.state_v2 import (
    HARD_CAPABLE_SOURCES,
    MAX_RECENT_SETS,
    MAX_REVOCATIONS,
    REVIVE_CAPABLE_SOURCES,
    AskedQuestion,
    ConversationStateV2,
    DisplayedRef,
    Need,
    ParkedTopic,
    PendingClarification,
    References,
    Revocation,
    Topic,
    bounded_map,
    enforce_caps,
)

if TYPE_CHECKING:  # `delta.py` importă reducerul; aici e nevoie doar de tipuri
    from src.conversation.delta import TurnDelta
    from src.conversation.interpretation import ResolvedRef

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
    # NX-330: emise de `delta.py` pentru `clear topic` / `clear all` (implementate de NX-331).
    "clear_topic",
    "clear_all",
    # NX-331: „am întrebat cheia asta" (întrebarea de îngustare NX-315, emisă de `finalize`).
    "note_asked",
    # NX-336 PR B (kernel.v2.0, I5): prune-ul de siguranță pe calea kernelului, ca ELIMINARE.
    "prune_products",
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
        "clear_topic",
        "clear_all",
        "note_asked",
        "prune_products",
    }
)

#: I20: singurele operații pe care le poate propune ieșirea unui EXECUTOR într-un tur interpretat.
#: Nevoile și subiectul sunt o funcție pură de (stare, interpretare, vocabular, referințe).
EXECUTOR_OPS: frozenset[str] = frozenset({"set_references", "set_active_search"})

#: Motivele de tombstone care blochează reactivarea unei nevoi parcate la `resume` (I6): clientul
#: (sau politica) a retras cheia DUPĂ ce subiectul a fost parcat. `topic_reset` e chiar parcarea.
_RESUME_BLOCKING_REASONS: frozenset[str] = frozenset({"user_explicit", "policy", "correction"})

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
        "unsupported_revoke",
        "safety_immutable",
        "sensitive_no_consent",
        "already_pending",
        "already_asked",
        "subject_owned",
        # NX-331 (I20): un executor a propus altceva decât referințe / `active_search`.
        "executor_state_scope",
        # NX-336 PR B: a doua trecere a commit-ului kernelului a primit altceva decât memoria
        # întrebării sau prune-ul de siguranță (aruncată și numărată, turul nu se pierde, P6).
        "second_pass_scope",
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
    # NX-314: subiectul conversației. `subject=True` marchează propunerea PROPRIETARULUI
    # (`_learn_constraints`): `product_type` se aplică exact (și `None` = set amestecat), iar
    # `category_key=None` înseamnă „raftul nu s-a schimbat", nu „propunere goală".
    product_type: str | None = None
    subject: bool = False
    # NX-331: `interpretation` = propunere scrisă de `delta.py` din interpretarea turului. Acolo
    # subiectul e PERECHEA (raft, tip), deci orice schimbare a ei parchează. `legacy` = scriitorii
    # de azi: tipul propus de `_learn_subject` (NX-314) se actualizează fără parcare, iar resetul
    # pornește doar pe raft. Distincția e un câmp, nu o euristică, și dispare la pasul 5.
    origin: Literal["legacy", "interpretation"] = "legacy"
    # NX-348: perechea (raft, tip) care ar rezulta din propunere EXISTĂ în catalog (`True`), nu
    # există (`False`) sau nu se știe (`None`). Scrisă de orchestrator din datele turului, înaintea
    # reducerului, ca reducerul să rămână pur și commit-ul să vadă aceeași decizie ca poarta.
    pair_compatible: bool | None = None
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
    # applied | unchanged | superseded | revoked | reset; NX-331: parked | evicted (op `park`),
    # swapped | not_available (op `resume`), applied | unconfirmed (op `correction`)
    outcome: str = "applied"


#: Ce întoarce un handler: starea nouă + unul sau mai multe `Applied` (o schimbare de subiect e și
#: o parcare), sau o respingere typed.
_Outcome = tuple[ConversationStateV2, Applied | tuple[Applied, ...]] | RejectedUpdate


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
        current = _apply(current, proposal, policy, applied, rejected)
    return ReducedState(enforce_caps(current), tuple(applied), tuple(rejected))


def _apply(
    state: ConversationStateV2,
    proposal: StateUpdateProposal,
    policy: ReducerPolicy,
    applied: list[Applied],
    rejected: list[RejectedUpdate],
) -> ConversationStateV2:
    """O propunere peste starea curentă a lotului; înregistrările se adaugă în liste."""
    outcome = _reduce_one(state, proposal, policy)
    if isinstance(outcome, RejectedUpdate):
        rejected.append(outcome)
        return state
    new_state, record = outcome
    applied.extend(record if isinstance(record, tuple) else (record,))
    return new_state


# ---------------------------------------------------------------------------
# Implementare
# ---------------------------------------------------------------------------


def _reduce_one(
    state: ConversationStateV2, proposal: StateUpdateProposal, policy: ReducerPolicy
) -> _Outcome:
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
) -> _Outcome:
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

    if existing is None and key == UNMAPPED_KEY and normalized.value is not None:
        state = _evict_oldest_unmapped(state, fresh.scope)

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

    crossing = _crossed_bound(state, key, normalized.value, vocab)
    if crossing is not None and not _can_override(proposal, crossing):
        # O limită ne-explicită nu înlocuiește una spusă de client (contractul: doar `explicit`
        # poate înlocui un `explicit`), exact ca pe aceeași cheie.
        return RejectedUpdate("set_need", "hard_downgrade", key, proposal.source)

    revocations = _drop_revocations(state, key, proposal)
    outcome = "applied"
    records: tuple[Applied, ...] = ()
    if crossing is not None:
        state, revocations = _retire_crossed(state, crossing, revocations, proposal)
        records = (
            Applied(
                "bound_crossed", vocab.dimension_of(key), strength, proposal.source, "superseded"
            ),
        )
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
    applied = Applied("set_need", key, strength, proposal.source, outcome)
    # Forma de dinainte (UN `Applied`) când nu s-a încrucișat nimic: `_handle_supersede` și orice
    # alt apelant intern citesc înregistrarea turului, nu o listă.
    return (
        replace(state, needs=(*needs, fresh), revocations=revocations),
        (*records, applied) if records else applied,
    )


def _crossed_bound(
    state: ConversationStateV2, key: str, value: Any, vocab: NeedVocabulary
) -> Need | None:
    """NX-334 — limita OPUSĂ activă pe care valoarea nouă o încrucișează (jos > sus), sau None.

    Contractul („Corrections and conflicts"): o limită nouă care încrucișează una dintr-un tur
    ANTERIOR câștigă, iar cea veche e înlocuită; se numără. În ACELAȘI tur conflictul e al
    validării (`hard_conflict`, provenance), deci aici nu ajunge. Egalitatea nu e încrucișare:
    „exact 3" e intervalul [3, 3]."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    pair = vocab.bounds_for(vocab.dimension_of(key))
    opposite_key = vocab.opposite_bound(key)
    if pair is None or opposite_key is None:
        return None
    opposite = next((n for n in state.needs if n.is_active and n.key == opposite_key), None)
    if opposite is None or not isinstance(opposite.normalized_value, (int, float)):
        return None
    low, high = (
        (value, opposite.normalized_value) if key == pair[0] else (opposite.normalized_value, value)
    )
    return opposite if float(low) > float(high) else None


def _can_override(proposal: StateUpdateProposal, existing: Need) -> bool:
    """Cine poate înlocui o limită: clientul (`explicit`) oricând; altcineva doar o limită care
    nu era a clientului."""
    return (
        proposal.source in REVIVE_CAPABLE_SOURCES or existing.source not in REVIVE_CAPABLE_SOURCES
    )


def _retire_crossed(
    state: ConversationStateV2,
    crossed: Need,
    revocations: tuple[Revocation, ...],
    proposal: StateUpdateProposal,
) -> tuple[ConversationStateV2, tuple[Revocation, ...]]:
    """Limita încrucișată devine `superseded`, cu tombstone (ca orice înlocuire): clientul s-a
    răzgândit, nu a retras-o de tot."""
    needs = tuple(
        replace(n, status="superseded", updated_revision=state.revision) if n is crossed else n
        for n in state.needs
    )
    revocations = _tombstone(
        revocations,
        Revocation(
            key=crossed.key,
            prior_value_fingerprint=value_fingerprint(crossed.normalized_value),
            source_turn_id=proposal.turn_id,
            revision=state.revision,
            reason_code="superseded",
        ),
    )
    return replace(state, needs=needs), revocations


def _evict_oldest_unmapped(state: ConversationStateV2, scope: str | None) -> ConversationStateV2:
    """I25: cel mult `MAX_UNMAPPED_PER_TOPIC` semnale `unmapped` active pe subiect. Al patrulea îl
    înlocuiește pe cel mai vechi (cea mai mică revizie, apoi primul inserat). Fără tombstone: nu e
    o retragere a clientului, e plafonul unui semnal soft."""
    live = [
        (n.updated_revision, index)
        for index, n in enumerate(state.needs)
        if n.is_active and n.key == UNMAPPED_KEY and n.scope == scope
    ]
    if len(live) < MAX_UNMAPPED_PER_TOPIC:
        return state
    _, oldest = min(live)
    return replace(
        state,
        needs=tuple(
            replace(n, status="superseded", updated_revision=state.revision)
            if index == oldest
            else n
            for index, n in enumerate(state.needs)
        ),
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
) -> _Outcome:
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
    # NX-334: o limită nouă care încrucișează limita opusă întoarce două înregistrări
    # (`bound_crossed`, apoi scrierea); doar ultima e înlocuirea cerută.
    if isinstance(record, tuple):
        *crossed, own = record
        return new_state, (*crossed, replace(own, op="supersede", outcome="superseded"))
    return new_state, replace(record, op="supersede", outcome="superseded")


def _handle_confirm(
    state: ConversationStateV2, proposal: StateUpdateProposal, policy: ReducerPolicy
) -> _Outcome:
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
) -> _Outcome:
    key = norm_key(proposal.key)
    if not key:
        return RejectedUpdate("revoke", "unknown_key", None, proposal.source)
    targets = [n for n in state.needs if n.is_active and n.key == key]
    listed = _list_value(proposal, policy)
    if listed is not None:
        # NX-331: pe o cheie de LISTĂ o valoare numește O nevoie („remove c2" = `pores`, nu toate
        # nevoile de ten). Fără valoare, retragerea rămâne pe toată cheia, ca înainte.
        targets = [n for n in targets if n.normalized_value == listed]

    # NX-337: ordinea contractului parchează SUBIECTUL înaintea retragerii (`_handle_set_topic` →
    # `_retire_topic`), deci o retragere pe o nevoie a subiectului tocmai părăsit, ÎN ACELAȘI TUR,
    # nu mai găsește nimic ACTIV — nevoia trăiește deja doar în slotul parcat. O căutăm și acolo,
    # dar NUMAI când parcarea s-a întâmplat chiar în turul curent (`parked_at_revision ==
    # state.revision`): o parcare mai veche rămâne comportamentul de azi, apărat de
    # `_retracted_since_parking` la `resume`.
    parked_targets: list[Need] = []
    if (
        not targets
        and state.parked is not None
        and state.parked.parked_at_revision == state.revision
    ):
        parked_targets = [n for n in state.parked.needs if n.key == key]
        if listed is not None:
            parked_targets = [n for n in parked_targets if n.normalized_value == listed]

    found = targets or parked_targets
    if any(_is_protected(n) for n in found) and not (
        proposal.source == "user_explicit" and proposal.reason_code == "user_explicit"
    ):
        # Siguranța nu expiră accidental și nu cade la un topic switch (NX-173).
        return RejectedUpdate("revoke", "safety_immutable", key, proposal.source)

    # Simetria cu `set_need`: acolo, o inferență de model nu poate RESCRIE un fapt al clientului
    # (`hard_downgrade`); aici nu-l poate nici ȘTERGE. Fără poarta asta, exact relaxarea interzisă
    # de D7 rămânea posibilă pe ușa din spate — nu prin `relaxations` (pe care validatorul le
    # verifică), ci printr-o propunere de revocare, pe care nu le verifica nimeni.
    #
    # Ce NU blochează: nevoile pe care modelul le-a creat el însuși rămân `soft` +
    # `model_inferred` (D7 le coboară la naștere), deci „nu vreau Sony" → „de fapt accept Sony"
    # trece neatins. Se apără doar ce a AFIRMAT clientul.
    if proposal.source not in REVIVE_CAPABLE_SOURCES and any(
        n.source in REVIVE_CAPABLE_SOURCES or n.strength == HARD for n in found
    ):
        return RejectedUpdate("revoke", "unsupported_revoke", key, proposal.source)

    if found:
        # Ținta găsită (activă sau tocmai parcată) identifică valoarea REALĂ; propunerea poartă
        # doar valoarea din DELTA, care poate fi alta (cardul NX-337: 100 vs nevoia parcată, 200).
        prior = found[0].normalized_value
    elif listed is not None:
        # Cheie de LISTĂ fără nicio țintă nicăieri: valoarea cerută tot deosebește (marca X ≠
        # marca Y), deci amprenta rămâne pe ea.
        prior = listed
    else:
        # Cheie scalară fără nicio țintă nicăieri: nimic n-o identifică — tombstone pe toată
        # cheia (`None` = fără amprentă), nu pe o valoare inventată din propunere (principiul 6).
        prior = None

    needs = tuple(
        replace(n, status="revoked", updated_revision=state.revision)
        if any(n is t for t in targets)
        else n
        for n in state.needs
    )
    parked = state.parked
    if parked_targets:
        # Nevoia retrasă nu mai are ce căuta în parcat: `resume` nu se mai poate baza doar pe
        # comparația de amprentă (I6), iar un al doilea `revoke` pe aceeași cheie n-ar mai
        # găsi-o de două ori.
        parked = replace(
            parked,
            needs=tuple(n for n in parked.needs if not any(n is t for t in parked_targets)),
        )
    reason = (
        proposal.reason_code
        if proposal.reason_code in {"user_explicit", "policy", "correction"}
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
        replace(state, needs=needs, revocations=revocations, parked=parked),
        Applied("revoke", key, SOFT, proposal.source, "revoked"),
    )


def _list_value(proposal: StateUpdateProposal, policy: ReducerPolicy) -> Any:
    """Valoarea canonică a unei propuneri pe o cheie de LISTĂ (`contains`), sau None."""
    if proposal.value is None:
        return None
    normalized = normalize_need(proposal.key, proposal.value, policy.vocabulary)
    if normalized is None or normalized.operator != "contains":
        return None
    return normalized.value


def _proposed_category(proposal: StateUpdateProposal) -> str | None:
    """Raftul unei propuneri `set_topic`. Proprietarul subiectului (NX-314) și interpretarea
    (NX-331) propun CHEI de catalog deja rezolvate, deci se păstrează verbatim: `norm_key` ar face
    din slug-ul `ten-ingrijirea-tenului` un `ten_ingrijirea_tenului`, pe care nici
    `_category_clause`, nici `topic_root_of` nu-l mai recunosc, iar același raft ar arăta ca alt
    subiect. Doar raftul propus liber de planul creierului trece prin `norm_key`, ca înainte."""
    if proposal.subject or proposal.origin == "interpretation":
        return (proposal.category_key or "").strip() or None
    return norm_key(proposal.category_key) or None


def _rescoped(
    state: ConversationStateV2, category: str, policy: ReducerPolicy
) -> ConversationStateV2:
    """NX-348: subiectul își primește PRIMUL raft. Nevoile lui de subiect scrise fără raft
    (`scope=None`) îl primesc acum, altfel o schimbare de subiect ulterioară nu le-ar mai parca și
    nu le-ar retrage (recenzia NX-348, constatarea 1)."""
    needs = tuple(
        replace(n, scope=category)
        if n.is_active
        and n.scope is None
        and not _is_protected(n)
        and _topic_scoped(n.key, policy.vocabulary)
        else n
        for n in state.needs
    )
    return replace(state, needs=needs)


def _handle_set_topic(
    state: ConversationStateV2, proposal: StateUpdateProposal, policy: ReducerPolicy
) -> _Outcome:
    category = _proposed_category(proposal)
    interpreted = proposal.origin == "interpretation"
    if proposal.subject or (interpreted and category is None and proposal.product_type):
        # Raftul nerezolvat nu e o schimbare de raft: subiectul poate purta doar tipul. NX-348: la
        # fel pe interpretare, unde clientul a numit doar tipul («vreau un ser»).
        category = category or state.topic.category_key
    elif category is None and proposal.goal is None:
        return RejectedUpdate("set_topic", "invalid_payload", None, proposal.source)
    elif proposal.source == "model_inferred" and state.topic.product_type:
        # NX-314 (P3): subiectul are un singur scriitor. Planul creierului poate propune un raft,
        # dar nu poate muta unul pe care îl susține deja setul arătat clientului.
        return RejectedUpdate("set_topic", "subject_owned", category, proposal.source)
    previous = state.topic.category_key
    type_learned = False
    if interpreted:
        # NX-331: subiectul e PERECHEA (raft, tip). NX-348 (kernel.v3.0, decis de Adi pe
        # 2026-09-29): subiectul se SCHIMBĂ doar când o jumătate deja setată primește ALTĂ valoare.
        # Completarea unei jumătăți goale, lângă o jumătate setată, e RAFINARE doar când perechea
        # rezultată EXISTĂ în catalog (`pair_compatible`, recenzia NX-348): altfel e schimbare de
        # subiect, iar cealaltă jumătate se golește. Un tip DEDUS de cod (`type_learned`) e o
        # jumătate goală: clientul nu l-a spus.
        learned = state.topic.type_learned
        old_type = None if learned else state.topic.product_type
        new_type = proposal.product_type
        shelf_changed = previous is not None and category is not None and category != previous
        type_changed = old_type is not None and new_type is not None and new_type != old_type
        fills_shelf = previous is None and category is not None and old_type is not None
        fills_type = new_type is not None and old_type is None and previous is not None
        if not shelf_changed and not type_changed:
            if (fills_shelf or fills_type) and proposal.pair_compatible is not True:
                # Completare fără dovadă că perechea există: subiect nou, fără jumătatea veche.
                product_type = new_type if fills_type else None
                category = None if fills_type else category
                same_subject = False
            else:
                kept_type = new_type or state.topic.product_type
                topic = replace(
                    state.topic,
                    category_key=category or previous,
                    product_type=kept_type,
                    goal=proposal.goal or state.topic.goal,
                    type_learned=learned and new_type is None and kept_type is not None,
                )
                changed = (topic.category_key, topic.product_type) != (
                    previous,
                    state.topic.product_type,
                )
                if previous is None and old_type is None and state.topic.product_type is None:
                    # Prima ancorare: subiect NOU (revizia lui), nimic de parcat.
                    topic = replace(topic, changed_at_revision=state.revision)
                refined = replace(state, topic=topic)
                if previous is None and topic.category_key is not None:
                    refined = _rescoped(refined, topic.category_key, policy)
                outcome = (
                    "unchanged"
                    if not changed
                    else ("applied" if previous is None and old_type is None else "refined")
                )
                return (
                    refined,
                    Applied("set_topic", topic.category_key, SOFT, proposal.source, outcome),
                )
        else:
            product_type = new_type or (None if shelf_changed else state.topic.product_type)
            same_subject = False
    else:
        product_type = proposal.product_type if proposal.subject else state.topic.product_type
        same_subject = category == previous
        type_learned = (proposal.subject and product_type is not None) or (
            not proposal.subject and state.topic.type_learned
        )
    if same_subject:
        topic = replace(
            state.topic,
            goal=proposal.goal or state.topic.goal,
            product_type=product_type,
            type_learned=type_learned,
        )
        return (
            replace(state, topic=topic),
            Applied("set_topic", category, SOFT, proposal.source, "unchanged"),
        )

    topic = Topic(
        category_key=category,
        goal=proposal.goal,
        changed_at_revision=state.revision,
        product_type=product_type if (proposal.subject or interpreted) else None,
        type_learned=type_learned and proposal.subject,
    )
    if previous is None and not (interpreted and state.topic.product_type):
        # Prima ancorare a subiectului nu retrage nimic: nu exista un subiect vechi de resetat.
        return (
            replace(state, topic=topic),
            Applied("set_topic", category, SOFT, proposal.source, "applied"),
        )

    outcome = "evicted" if state.parked is not None else "parked"
    parked = _parked_now(state, policy)
    state = _retire_topic(state, proposal.turn_id, policy)
    return (
        replace(state, topic=topic, parked=parked),
        (
            Applied("set_topic", category, SOFT, proposal.source, "reset"),
            Applied("park", previous, SOFT, proposal.source, outcome),
        ),
    )


def _topic_needs(state: ConversationStateV2, policy: ReducerPolicy) -> tuple[Need, ...]:
    """Nevoile ACTIVE ale subiectului curent: scope pe raftul lui, fără cele de siguranță (NX-173
    nu se resetează și nu se parchează: rămân active pe orice subiect). Un subiect fără raft n-are
    nevoi cu scope (scope-ul se scrie din raft), deci nu întoarce nimic.

    Scope-ul se judecă și pe vocabularul TURULUI, nu doar pe ce s-a scris atunci: o cheie pe care
    pachetul o declară acum a conversației (bugetul, NX-331) nu e a subiectului, chiar dacă un
    document scris înainte îi poartă raftul în `scope`."""
    category = state.topic.category_key
    if category is None and state.topic.product_type is None:
        return ()
    # NX-348: un subiect FĂRĂ raft (doar tipul) are nevoile lui cu `scope=None`; fără asta, o
    # schimbare de subiect nu le-ar parca și nu le-ar retrage (recenzia NX-348, constatarea 1).
    return tuple(
        n
        for n in state.needs
        if n.is_active
        and n.scope == category
        and not _is_protected(n)
        and _topic_scoped(n.key, policy.vocabulary)
    )


def _topic_scoped(key: str, vocab: NeedVocabulary) -> bool:
    """O cheie necunoscută vocabularului (pachet schimbat între ture) rămâne pe scope-ul scris."""
    spec = vocab.spec_for(key)
    return spec is None or spec.scoped


def _parked_now(state: ConversationStateV2, policy: ReducerPolicy) -> ParkedTopic:
    """Subiectul curent, nevoile lui și ultimul set arătat pe el, ca slot parcat (un nivel, I19).
    Aceeași regulă la o schimbare de subiect și la `resume`."""
    return ParkedTopic(
        topic=state.topic,
        needs=_topic_needs(state, policy),
        shown=state.references.displayed_products,
        parked_at_revision=state.revision,
    )


def _supersede_all(
    state: ConversationStateV2, retired: Sequence[Need], turn_id: str | None, reason: str
) -> ConversationStateV2:
    """Nevoile `retired` → `superseded`, cu un tombstone per cheie (`reason`)."""
    needs = tuple(
        replace(n, status="superseded", updated_revision=state.revision)
        if any(n is r for r in retired)
        else n
        for n in state.needs
    )
    revocations = state.revocations
    for key in sorted({n.key for n in retired}):
        revocations = _tombstone(
            revocations,
            Revocation(
                key=key,
                source_turn_id=turn_id,
                revision=state.revision,
                reason_code=reason,
            ),
        )
    return replace(state, needs=needs, revocations=revocations)


def _retire_topic(
    state: ConversationStateV2, turn_id: str | None, policy: ReducerPolicy
) -> ConversationStateV2:
    """Nevoile subiectului curent → `superseded` + tombstone `topic_reset`; întrebarea în așteptare
    și cheile deja întrebate care țin de subiect se golesc. Nevoile pe conversație rămân (I4)."""
    # Întrebarea în așteptare era despre subiectul abandonat; la fel sloturile deja întrebate care
    # țin de subiect: după schimbare au voie să fie întrebate din nou.
    asked = tuple(q for q in state.asked_questions if not _scoped_key(q.key, policy.vocabulary))
    state = _supersede_all(state, _topic_needs(state, policy), turn_id, "topic_reset")
    return replace(state, pending_clarification=None, asked_questions=asked)


def _handle_clear_topic(
    state: ConversationStateV2, proposal: StateUpdateProposal, policy: ReducerPolicy
) -> _Outcome:
    """`clear topic`: nevoile subiectului curent se retrag (`topic_reset`), iar subiectul rămâne.
    Nimic nu se parchează: clientul a cerut să uite criteriile, nu să schimbe subiectul."""
    if proposal.source not in REVIVE_CAPABLE_SOURCES:
        # Simetria cu `revoke`: doar clientul își poate șterge criteriile (D7).
        return RejectedUpdate("clear_topic", "unsupported_revoke", None, proposal.source)
    return (
        _retire_topic(state, proposal.turn_id, policy),
        Applied("clear_topic", state.topic.category_key, SOFT, proposal.source, "reset"),
    )


def _handle_clear_all(
    state: ConversationStateV2, proposal: StateUpdateProposal, policy: ReducerPolicy
) -> _Outcome:
    """`clear all`: toate nevoile se retrag, mai puțin cele protejate (siguranță, politică), iar
    slotul parcat se golește (nevoile lui sunt tot nevoi). Tombstone-urile opresc reînvierea lor din
    istoric sau rezumat (I6)."""
    if proposal.source not in REVIVE_CAPABLE_SOURCES:
        return RejectedUpdate("clear_all", "unsupported_revoke", None, proposal.source)
    retired = [n for n in state.needs if n.is_active and not _is_protected(n)]
    state = _supersede_all(state, retired, proposal.turn_id, "user_explicit")
    return (
        replace(state, pending_clarification=None, parked=None),
        Applied("clear_all", None, SOFT, proposal.source, "reset"),
    )


def _handle_note_asked(
    state: ConversationStateV2, proposal: StateUpdateProposal, policy: ReducerPolicy
) -> _Outcome:
    """„Am întrebat cheia asta" fără o întrebare în așteptare (întrebarea de îngustare NX-315, pusă
    ca ultimă frază a răspunsului). Semnalul anti-buclă: cheia trece la coadă, cu o încercare în
    plus."""
    key = norm_key(proposal.key)
    if not key:
        return RejectedUpdate("note_asked", "invalid_payload", None, proposal.source)
    previous = state.asked(key)
    asked = tuple(q for q in state.asked_questions if q.key != key) + (
        AskedQuestion(key, state.revision, (previous.attempts + 1) if previous else 1),
    )
    return (
        replace(state, asked_questions=asked),
        Applied("note_asked", key, SOFT, proposal.source, "applied"),
    )


def _scoped_key(key: str, vocab: NeedVocabulary) -> bool:
    spec = vocab.spec_for(key)
    return bool(spec and spec.scoped)


def _handle_set_pending_question(
    state: ConversationStateV2, proposal: StateUpdateProposal, policy: ReducerPolicy
) -> _Outcome:
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
) -> _Outcome:
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
) -> _Outcome:
    payload = proposal.payload if isinstance(proposal.payload, Mapping) else None
    if payload is None:
        return RejectedUpdate("set_references", "invalid_payload", None, proposal.source)
    current = state.references
    displayed = current.displayed_products
    revision = current.displayed_revision
    recent = current.recent_sets
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
            if proposal.source != "policy":
                # NX-331: un set NOU împinge setul curent în `recent_sets`. Prune-ul de siguranță
                # (NX-173, `source="policy"`) e același set micșorat, deci nu împinge nimic.
                recent = _push_recent(recent, displayed, fresh)
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
        recent_sets=recent,
    )
    return (
        replace(state, references=references),
        Applied("set_references", None, SOFT, proposal.source, "applied"),
    )


def _id_set(refs: Iterable[DisplayedRef]) -> frozenset[str]:
    return frozenset(d.product_id for d in refs)


def _push_recent(
    recent: tuple[tuple[DisplayedRef, ...], ...],
    current: tuple[DisplayedRef, ...],
    fresh: tuple[DisplayedRef, ...],
) -> tuple[tuple[DisplayedRef, ...], ...]:
    """`current` devine cel mai recent set de mai devreme. Deduplicat pe MULȚIMEA de id-uri: un set
    reordonat nu e un set nou, iar un set revenit pe ecran nu mai e „de mai devreme"."""
    if not current or _id_set(current) == _id_set(fresh):
        return recent
    kept = tuple(s for s in recent if _id_set(s) not in (_id_set(current), _id_set(fresh)))
    return (current, *kept)[:MAX_RECENT_SETS]


def _ref(payload: Mapping[str, Any], key: str, current: str | None) -> str | None:
    if key not in payload:
        return current
    value = payload.get(key)
    return str(value)[:64] if value else None


def _handle_set_active_search(
    state: ConversationStateV2, proposal: StateUpdateProposal, policy: ReducerPolicy
) -> _Outcome:
    payload = proposal.payload
    if payload is not None and not isinstance(payload, Mapping):
        return RejectedUpdate("set_active_search", "invalid_payload", None, proposal.source)
    return (
        replace(state, active_search=bounded_map(dict(payload) if payload else None)),
        Applied("set_active_search", None, SOFT, proposal.source, "applied"),
    )


def _handle_set_cart_ref(
    state: ConversationStateV2, proposal: StateUpdateProposal, policy: ReducerPolicy
) -> _Outcome:
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


def _handle_prune_products(
    state: ConversationStateV2, proposal: StateUpdateProposal, policy: ReducerPolicy
) -> _Outcome:
    """NX-336 PR B (kernel.v2.0, I5): prune-ul de siguranță NX-173 pe calea kernelului, ca ELIMINARE
    a produselor blocate (`payload.product_ids`) din ecran, din seturile de mai devreme și din setul
    parcat, oriunde ar fi ajuns după prima trecere a turului. Pe v1 prune-ul e o ÎNLOCUIRE a listei
    cu ecranul de dinainte minus blocatele (`set_references`, `source="policy"`), care pe un tur cu
    carduri noi, o parcare sau o reluare ar pune ecranul vechi peste cel nou; aici nu se înlocuiește
    nimic. Lista afișată schimbată primește revizia turului (un ordinal pe lista veche e `stale`);
    fără nimic de scos, starea rămâne neatinsă (`unchanged`)."""
    payload = proposal.payload if isinstance(proposal.payload, Mapping) else None
    raw = payload.get("product_ids") if payload is not None else None
    if not isinstance(raw, (list, tuple, frozenset, set)) or not raw:
        return RejectedUpdate("prune_products", "invalid_payload", None, proposal.source)
    blocked = frozenset(str(p) for p in raw)
    current = state.references
    displayed = tuple(d for d in current.displayed_products if d.product_id not in blocked)
    recent = tuple(
        kept
        for shown_before in current.recent_sets
        if (kept := tuple(d for d in shown_before if d.product_id not in blocked))
    )
    parked = state.parked
    if parked is not None:
        parked = replace(
            parked, shown=tuple(d for d in parked.shown if d.product_id not in blocked)
        )
    screen_changed = displayed != current.displayed_products
    if not (screen_changed or recent != current.recent_sets or parked != state.parked):
        return state, Applied("prune_products", None, SOFT, proposal.source, "unchanged")
    references = replace(
        current,
        displayed_products=displayed,
        displayed_revision=state.revision if screen_changed else current.displayed_revision,
        recent_sets=recent,
    )
    return (
        replace(state, references=references, parked=parked),
        Applied("prune_products", None, SOFT, proposal.source, "applied"),
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
    "clear_topic": _handle_clear_topic,
    "clear_all": _handle_clear_all,
    "note_asked": _handle_note_asked,
    "prune_products": _handle_prune_products,
}


# ---------------------------------------------------------------------------
# NX-331 — turul INTERPRETAT: thread, parcare, corecție, efectul de referință
# ---------------------------------------------------------------------------


def reduce_turn(
    state_before: ConversationStateV2,
    delta: TurnDelta,
    executor_proposals: Sequence[StateUpdateProposal],
    resolved: Sequence[ResolvedRef],
    primary_target: str | None,
    corrects_previous_turn: bool,
    policy: ReducerPolicy,
) -> ReducedState:
    """Un tur interpretat, în ordinea contractului („Order of application within a turn"): `aside`
    ⇒ identitate; `resume`; subiectul și restul schimbărilor, în ordinea din `delta` (subiectul
    întâi, pus acolo de `delta.py`); corecția; propunerile executorilor (doar referințe și
    `active_search`, I20); efectul de referință (`selected_product` = ținta `exact` a actului
    principal). PUR: fără ceas, fără random, fără context de tur. O singură revizie per tur."""
    if delta.thread == "aside":
        # I5: paranteza e identitatea pe stare, inclusiv pe `active_search` și pe referințe,
        # oricare ar fi propunerile executorilor din tur.
        return ReducedState(state_before)

    state = replace(state_before, revision=state_before.revision + 1)
    applied: list[Applied] = []
    rejected: list[RejectedUpdate] = []

    if delta.thread == "resume":
        state, record = _resume(state, policy)
        applied.append(record)

    contradicted = (
        _contradictions(state_before, delta.proposals, policy) if corrects_previous_turn else None
    )
    for proposal in delta.proposals:
        state = _apply(state, proposal, policy, applied, rejected)
    if contradicted is not None:
        state, record = _apply_correction(
            state, state_before, contradicted, delta.proposals, policy.vocabulary
        )
        applied.append(record)

    for proposal in executor_proposals:
        if proposal.op not in EXECUTOR_OPS:
            rejected.append(
                RejectedUpdate(
                    str(proposal.op),
                    "executor_state_scope",
                    norm_key(proposal.key) or None,
                    proposal.source,
                )
            )
            continue
        state = _apply(state, proposal, policy, applied, rejected)

    target = _primary_product(resolved, primary_target)
    if target is not None:
        state = _apply(
            state,
            StateUpdateProposal(
                "set_references", source="catalog", payload={"selected_product": target}
            ),
            policy,
            applied,
            rejected,
        )
    return ReducedState(enforce_caps(state), tuple(applied), tuple(rejected))


def _primary_product(resolved: Sequence[ResolvedRef], primary_target: str | None) -> str | None:
    """Produsul țintei actului principal, doar când referința e `exact` pe UN produs."""
    if primary_target is None:
        return None
    ref = next((r for r in resolved if r.ref_id == primary_target), None)
    if ref is None or ref.outcome != "exact" or len(ref.product_ids) != 1:
        return None
    return ref.product_ids[0]


def _resume(
    state: ConversationStateV2, policy: ReducerPolicy
) -> tuple[ConversationStateV2, Applied]:
    """`thread=resume`: SCHIMB cu slotul parcat, nu scoatere din stivă (I19).

    Subiectul parcat devine curent, cu nevoile lui reactivate exact cum erau (tărie, sursă,
    `confirmed`), iar tombstone-urile lor `topic_reset` se scot. Subiectul curent devine parcat prin
    aceeași regulă ca la o schimbare de subiect, iar setul arătat se schimbă cu cel parcat.
    Reactivarea NU trece prin regula de înviere: o nevoie parcată de cod nu e una revocată de client
    (I4). Una pe care clientul a retras-o DUPĂ parcare rămâne retrasă (I6)."""
    parked = state.parked
    if parked is None:
        return state, Applied("resume", None, SOFT, "user_explicit", "not_available")

    current = state.topic
    has_current = current.category_key is not None or current.product_type is not None
    new_parked = _parked_now(state, policy) if has_current else None
    leaving = state.references.displayed_products
    state = _retire_topic(state, None, policy)

    revived: list[Need] = []
    for need in parked.needs:
        if _retracted_since_parking(need, state.revocations, parked.parked_at_revision):
            continue
        clash = any(
            n.is_active
            and n.key == need.key
            and (n.operator != "contains" or n.normalized_value == need.normalized_value)
            for n in (*state.needs, *revived)
        )
        if not clash:
            revived.append(replace(need, status="active"))
    revived_marks = {(n.key, n.normalized_value, n.scope) for n in revived}
    needs = tuple(
        n
        for n in state.needs
        if n.is_active or (n.key, n.normalized_value, n.scope) not in revived_marks
    ) + tuple(revived)
    revived_keys = {n.key for n in revived}
    revocations = tuple(
        r
        for r in state.revocations
        if not (r.reason_code == "topic_reset" and r.key in revived_keys)
    )
    on_screen = _id_set(parked.shown)
    recent = tuple(s for s in state.references.recent_sets if _id_set(s) != on_screen)
    if new_parked is None:
        # Fără subiect curent, setul de pe ecran n-are slot de parcare: rămâne „de mai devreme".
        recent = _push_recent(recent, leaving, parked.shown)
    references = replace(
        state.references,
        displayed_products=parked.shown,
        displayed_revision=state.revision,
        # Setul care revine pe ecran nu mai e „de mai devreme"; cel care pleacă stă în parcat.
        recent_sets=recent,
        # Focusul și comparația țin de setul care pleacă: rămân doar dacă sunt și pe ecranul nou,
        # altfel un «acesta» după reluare s-ar rezolva pe un produs al subiectului parcat.
        selected_product=(
            state.references.selected_product
            if state.references.selected_product in on_screen
            else None
        ),
        compared_products=tuple(p for p in state.references.compared_products if p in on_screen),
    )
    return (
        replace(
            state,
            topic=replace(parked.topic, changed_at_revision=state.revision),
            needs=needs,
            revocations=revocations,
            references=references,
            parked=new_parked,
        ),
        Applied("resume", parked.topic.category_key, SOFT, "user_explicit", "swapped"),
    )


def _retracted_since_parking(
    need: Need, revocations: Iterable[Revocation], parked_at_revision: int
) -> bool:
    """Clientul (sau politica) a retras nevoia DUPĂ ce subiectul a fost parcat (I6)? Tombstone-ul
    păstrează amprenta valorii: o retragere a ALTEI valori pe aceeași cheie (marca tabletelor) nu
    atinge marca telefoanelor parcate. O retragere fără valoare (toată cheia) o atinge."""
    fingerprint = value_fingerprint(need.normalized_value)
    return any(
        r.key == need.key
        and r.reason_code in _RESUME_BLOCKING_REASONS
        and r.revision >= parked_at_revision
        and r.prior_value_fingerprint in (None, fingerprint)
        for r in revocations
    )


@dataclass(frozen=True)
class _Contradiction:
    """Ce contrazice turul curent din ce a scris turul anterior (I21)."""

    dimensions: frozenset[str] = frozenset()
    subject: bool = False

    def __bool__(self) -> bool:
        return bool(self.dimensions) or self.subject


def _written_by_previous_turn(need: Need, before: ConversationStateV2) -> bool:
    """`updated_revision` = revizia turului care a scris nevoia. Revizia 0 e a documentului
    adaptat din v1, deci n-are un „tur anterior" care să-l fi scris."""
    return before.revision > 0 and need.updated_revision == before.revision


def _contradictions(
    before: ConversationStateV2,
    proposals: Sequence[StateUpdateProposal],
    policy: ReducerPolicy,
) -> _Contradiction:
    """Contradicțiile cu turul anterior, judecate pe starea DINAINTEA turului (contractul,
    „Corrections and conflicts"): `replace`/`remove` pe o nevoie scrisă de turul anterior; `set` pe
    o dimensiune scalară a cărei valoare activă a scris-o turul anterior, cu altă valoare; o
    schimbare de subiect când turul anterior a setat subiectul."""
    dimensions: set[str] = set()
    subject = False
    for proposal in proposals:
        if proposal.op == "set_topic":
            # NX-348: perechea EFECTIVĂ (fără raft = raftul curent); contrazice doar o jumătate
            # DEJA SETATĂ de turul anterior care primește altă valoare (recenzia, constatarea 4).
            category = _proposed_category(proposal) or before.topic.category_key
            new_type = proposal.product_type
            written_before = (
                before.revision > 0 and before.topic.changed_at_revision == before.revision
            )
            subject = subject or (
                written_before
                and (
                    (
                        before.topic.category_key is not None
                        and category != before.topic.category_key
                    )
                    or (
                        before.topic.product_type is not None
                        and new_type is not None
                        and new_type != before.topic.product_type
                    )
                )
            )
            continue
        if proposal.op not in ("set_need", "supersede", "revoke"):
            continue
        normalized = normalize_need(proposal.key, proposal.value, policy.vocabulary)
        if normalized is None:
            continue
        for need in before.needs:
            if not (need.is_active and need.key == normalized.key):
                continue
            if not _written_by_previous_turn(need, before):
                continue
            if proposal.op == "set_need":
                if normalized.operator == "contains" or need.normalized_value == normalized.value:
                    continue
            elif need.operator == "contains" and need.normalized_value != normalized.value:
                # Un handle numește o valoare: pe o listă, doar acea valoare e contrazisă.
                continue
            dimensions.add(dimension_of_key(need.key, policy.vocabulary))
    return _Contradiction(frozenset(dimensions), subject)


def _apply_correction(
    state: ConversationStateV2,
    before: ConversationStateV2,
    contradiction: _Contradiction,
    proposals: Sequence[StateUpdateProposal],
    vocabulary: NeedVocabulary,
) -> tuple[ConversationStateV2, Applied]:
    """La contradicție, nevoile NEEXPLICITE (`user_implicit` / `model_inferred`) pe care turul
    anterior le-a scris pe aceeași dimensiune se retrag, cu motivul `correction`; nevoia contrazisă
    a fost deja înlocuită de propunerea turului. Fără contradicție nu se retrage nimic, iar
    `correction_unconfirmed` se numără: «Nu, vreau și protecție solară» e un `add` (I21)."""
    source = next((p.source for p in proposals), "user_explicit")
    if not contradiction:
        return state, Applied("correction", None, SOFT, source, "unconfirmed")
    stale = [
        need
        for need in state.needs
        if need.is_active
        and need.source in ("user_implicit", "model_inferred")
        and dimension_of_key(need.key, vocabulary) in contradiction.dimensions
        and _written_by_previous_turn(need, before)
    ]
    needs = tuple(
        replace(n, status="revoked", updated_revision=state.revision)
        if any(n is s for s in stale)
        else n
        for n in state.needs
    )
    revocations = state.revocations
    for need in stale:
        revocations = _tombstone(
            revocations,
            Revocation(
                key=need.key,
                prior_value_fingerprint=value_fingerprint(need.normalized_value),
                source_turn_id=None,
                revision=state.revision,
                reason_code="correction",
            ),
        )
    return (
        replace(state, needs=needs, revocations=revocations),
        Applied("correction", None, SOFT, source, "applied"),
    )


__all__ = [
    "ALLOWED_OPS",
    "EXECUTOR_OPS",
    "REJECT_REASONS",
    "Applied",
    "ProposalOp",
    "ReducedState",
    "ReducerPolicy",
    "RejectedUpdate",
    "StateUpdateProposal",
    "reduce",
    "reduce_all",
    "reduce_turn",
]
