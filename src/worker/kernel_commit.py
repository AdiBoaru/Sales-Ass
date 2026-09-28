"""Kernel `kernel.v2.0`, pasul 6 (NX-336 PR B): commit-ul unui tur SERVIT de kernel.

Rol `orchestrator` în `tests/kernel_modules.json`. Pe calea de azi commit-ul e
`reduce_all(stare, ctx.state_proposals + coada Sender-ului)`: fiecare stagiu propune, reducerul
aplică. Pe un tur servit de kernel propunerile stagiilor de DINAINTEA ramurii NU intră (nici cele
ale lui `clarify_resume`, nici ale scriitorilor vechi, pe care ramura îi ocolește prin construcție):
starea e o funcție de (stare, interpretare, vocabular, referințe), deci intrarea e `KernelTurn`,
scris de orchestrator doar după ce executorii au servit turul.

Două treceri, în ordinea contractului:

1. `reduce_turn(stare, delta, executor, resolved, primary, corrects)`: `executor` = propunerile
   adăugate de executori în tur + coada Sender-ului fără prune-ul de siguranță. Ce nu e în
   `EXECUTOR_OPS` se respinge CU înregistrare (`executor_state_scope`, I20), și pe `aside`;
2. `reduce_all(…, [memoria întrebării, eliminarea produselor blocate], revision=fixată)`.

Prune-ul de siguranță NX-173 e pe calea kernelului o ELIMINARE (`prune_products`): produsele blocate
= ecranul de la încărcarea turului minus lista curățată pe care o scrie `_prune_displayed`; ies din
ecranul de după prima trecere, din seturile de mai devreme și din setul parcat. Pe v1 prune-ul e o
înlocuire a listei, care pe un tur cu carduri noi, o parcare sau o reluare ar pune ecranul vechi
peste cel nou; calea v1 rămâne neatinsă. Fiindcă blocatele se calculează din ecranul de la
ÎNCĂRCARE, conflictul de versiune scoate din ecranul PROASPĂT doar produsele blocate în tur.

Pe `aside` prima trecere e identitatea (I5, `kernel.v2.0`), iar a doua aplică memoria și
eliminarea. Un tur = o revizie: pe `aside`, revizia crește o dată doar dacă s-a scos ceva.

A doua trecere primește o mulțime FIXĂ (`SECOND_PASS_OPS`); orice altceva se aruncă și se numără
(`second_pass_scope`), fără să piardă turul (P6). Totul e PUR."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from src.conversation.needs import norm_key
from src.conversation.state_reducer import (
    EXECUTOR_OPS,
    ReducedState,
    ReducerPolicy,
    RejectedUpdate,
    StateUpdateProposal,
    reduce_all,
    reduce_turn,
)
from src.conversation.state_v2 import ConversationStateV2

if TYPE_CHECKING:
    from src.conversation.delta import TurnDelta
    from src.conversation.interpretation import ResolvedRef

#: Sursa prune-ului de siguranță în coada Sender-ului (`processor._sender_tail`).
POLICY_SOURCE = "policy"
#: Operațiile memoriei întrebării (`ambiguity_gate.memory_proposal`).
MEMORY_OPS: frozenset[str] = frozenset({"set_pending_question", "note_asked"})
#: Eliminarea produselor blocate (`state_reducer._handle_prune_products`).
PRUNE_OP = "prune_products"
#: Mulțimea FIXĂ a celei de-a doua treceri.
SECOND_PASS_OPS: frozenset[str] = MEMORY_OPS | {PRUNE_OP}
#: Forma prune-ului v1 din coada Sender-ului, pe care calea kernelului o consumă ca eliminare.
_V1_PRUNE_OP = "set_references"
SECOND_PASS_SCOPE = "second_pass_scope"
EXECUTOR_SCOPE = "executor_state_scope"


@dataclass(frozen=True)
class KernelTurn:
    """Intrarea commit-ului unui tur servit de kernel. Proprietar unic: orchestratorul
    (`interpreted_turn`), scris DOAR după ce executorii au servit turul; niciodată pe fallback.

    `delta` poartă deja `resolve_question` când turul a răspuns unei întrebări vii (§1).
    `memory` e memoria întrebării DOAR când întrebarea a fost pusă în răspuns.
    `executor_proposals` = ce au adăugat executorii în `ctx.state_proposals` în tur (I20)."""

    delta: TurnDelta
    resolved: tuple[ResolvedRef, ...]
    primary: str | None
    corrects_previous_turn: bool
    memory: tuple[StateUpdateProposal, ...] = ()
    executor_proposals: tuple[StateUpdateProposal, ...] = ()


def split_tail(
    tail: Sequence[StateUpdateProposal],
) -> tuple[tuple[StateUpdateProposal, ...], tuple[StateUpdateProposal, ...]]:
    """Coada Sender-ului → (ce au scris executorii, ce e al politicii de siguranță)."""
    executor = tuple(p for p in tail if p.source != POLICY_SOURCE)
    policy = tuple(p for p in tail if p.source == POLICY_SOURCE)
    return executor, policy


def _v1_prune_kept(p: StateUpdateProposal) -> frozenset[str] | None:
    """Produsele PĂSTRATE de un prune v1 (`set_references`, `policy`, doar `displayed_products`),
    sau None dacă propunerea nu are forma asta."""
    payload = p.payload if isinstance(p.payload, Mapping) else None
    if (
        p.op != _V1_PRUNE_OP
        or p.source != POLICY_SOURCE
        or payload is None
        or set(payload) != {"displayed_products"}
    ):
        return None
    kept = set()
    for item in payload.get("displayed_products") or ():
        if isinstance(item, Mapping) and (item.get("product_id") or item.get("id")):
            kept.add(str(item.get("product_id") or item.get("id")))
    return frozenset(kept)


def blocked_ids(
    screen_before: Sequence[str], policy_tail: Sequence[StateUpdateProposal]
) -> frozenset[str]:
    """Produsele blocate de prune-ul turului: ecranul de la ÎNCĂRCARE minus lista curățată."""
    blocked: set[str] = set()
    for p in policy_tail:
        kept = _v1_prune_kept(p)
        if kept is not None:
            blocked |= set(screen_before) - kept
    return frozenset(blocked)


def second_pass_input(
    memory: Sequence[StateUpdateProposal],
    policy_tail: Sequence[StateUpdateProposal],
    blocked: frozenset[str],
) -> tuple[tuple[StateUpdateProposal, ...], tuple[RejectedUpdate, ...]]:
    """Intrarea celei de-a doua treceri și ce se aruncă. Intră: memoria întrebării și eliminarea
    produselor blocate. Prune-ul v1 din coadă e CONSUMAT (devine eliminarea). Orice altceva ⇒
    `RejectedUpdate(second_pass_scope)`, numărat în `need_update_rejected`, iar turul continuă."""
    kept: list[StateUpdateProposal] = []
    dropped: list[RejectedUpdate] = []
    for p in memory:
        if p.op in MEMORY_OPS:
            kept.append(p)
        else:
            dropped.append(_dropped(p, SECOND_PASS_SCOPE))
    for p in policy_tail:
        if _v1_prune_kept(p) is None:
            dropped.append(_dropped(p, SECOND_PASS_SCOPE))
    if blocked:
        kept.append(
            StateUpdateProposal(
                "prune_products", source=POLICY_SOURCE, payload={"product_ids": sorted(blocked)}
            )
        )
    return tuple(kept), tuple(dropped)


def _dropped(p: StateUpdateProposal, reason: str) -> RejectedUpdate:
    return RejectedUpdate(str(p.op), reason, norm_key(p.key) or None, p.source)


def _removed(reduced: ReducedState) -> bool:
    return any(a.op == PRUNE_OP and a.outcome == "applied" for a in reduced.applied)


def commit_kernel_turn(
    state_before: ConversationStateV2,
    turn: KernelTurn,
    tail: Sequence[StateUpdateProposal],
    policy: ReducerPolicy,
    *,
    screen_before: Sequence[str] = (),
) -> ReducedState:
    """Starea de persistat a unui tur servit de kernel (cele două treceri din docstring-ul
    modulului). `screen_before` = id-urile ecranului de la încărcarea turului (`ctx.state_v2`), din
    care se calculează produsele blocate. `Applied`/`RejectedUpdate` din ambele treceri, plus ce
    s-a respins înaintea lor, pentru evenimente."""
    executor_tail, policy_tail = split_tail(tail)
    executor = (*turn.executor_proposals, *executor_tail)
    allowed = tuple(p for p in executor if p.op in EXECUTOR_OPS)
    outside = tuple(_dropped(p, EXECUTOR_SCOPE) for p in executor if p.op not in EXECUTOR_OPS)
    first = reduce_turn(
        state_before,
        turn.delta,
        allowed,
        turn.resolved,
        turn.primary,
        turn.corrects_previous_turn,
        policy,
    )
    second_in, dropped = second_pass_input(
        turn.memory, policy_tail, blocked_ids(screen_before, policy_tail)
    )
    second = reduce_all(first.state, second_in, policy, revision=first.state.revision)
    if first.state.revision == state_before.revision and _removed(second):
        # `aside` + un produs scos: revizia crește o dată, ca `displayed_revision` să fie nouă (un
        # ordinal pe lista veche devine `stale`, failure matrix).
        second = reduce_all(first.state, second_in, policy)
    return ReducedState(
        second.state,
        (*first.applied, *second.applied),
        (*outside, *first.rejected, *second.rejected, *dropped),
    )


__all__ = [
    "EXECUTOR_SCOPE",
    "MEMORY_OPS",
    "POLICY_SOURCE",
    "PRUNE_OP",
    "SECOND_PASS_OPS",
    "SECOND_PASS_SCOPE",
    "KernelTurn",
    "blocked_ids",
    "commit_kernel_turn",
    "second_pass_input",
    "split_tail",
]
