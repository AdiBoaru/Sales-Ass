"""NX-330 (kernel v1.0, pasul 3a) — interpretarea validată → propuneri pentru reducer.

Contractul (`docs/KERNEL-CONTRACT-v1.md`, „Reducer, thread and parking"): reducerul e SINGURUL
scriitor de stare (I3), iar el primește doar propuneri typed. Modulul traduce `CheckedChange`-urile
din `provenance.py` în `StateUpdateProposal`, în ordinea din contract: schimbările de subiect întâi,
apoi restul, în ordinea scrisă de model.

Trei reguli pe care le ține modulul, nu reducerul:

- **I23:** o schimbare `inferred` nu produce propunere. Devine `RankingSignal`, care ordonează
  rezultatele turului curent și nu se persistă niciodată.
- **Valoarea relativă** («mai ieftin decât ăsta») o calculează CODUL din prețul RECITIT în tur al
  produsului rezolvat `exact`. Fără o țintă exactă sau fără preț cunoscut, schimbarea e respinsă
  (`unknown_reference`), nu ghicită.
- **Paranteza cu schimbări** (`thread=aside` + `changes` nevide) se tratează ca `continue` și se
  numără (`aside_with_changes`): contractul, „Thread".

Fiecare propunere se construiește cu op-ul LITERAL, ca extractorul NX-327 (poarta I3) să poată
clasifica static fiecare scriitor. Modul PUR: aceleași intrări ⇒ același `TurnDelta`, byte cu byte
(I20, partea pasului 3a)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from src.conversation.interpretation import CheckedChange, ResolvedRef, TurnInterpretation
from src.conversation.needs import NeedKind, NeedVocabulary
from src.conversation.provenance import (
    MAX_ACTS,
    MAX_CHANGES,
    MAX_REFERENCES,
    PRICE,
    UNMAPPED,
    Handle,
)
from src.conversation.references import ReferenceFacts
from src.conversation.state_reducer import StateUpdateProposal

CATEGORY = "category"
#: Cheia universală a unei excluderi, când fațeta nu are propriul operator `not_contains`.
RESTRICTION_KEY = "restriction"
UNMAPPED_KEY = "unmapped"

_SOURCE_BY_PROVENANCE: Mapping[str, str] = {
    "explicit": "user_explicit",
    "implicit": "user_implicit",
}


@dataclass(frozen=True)
class RankingSignal:
    """Un `inferred`: ordonează rezultatele turului curent, nu se persistă niciodată (I23)."""

    dimension: str
    value: str | float | None
    relation: str


@dataclass(frozen=True)
class TurnDelta:
    thread: Literal["continue", "aside", "resume"]
    proposals: tuple[StateUpdateProposal, ...] = ()
    ranking: tuple[RankingSignal, ...] = ()
    rejected: tuple[CheckedChange, ...] = ()
    counters: Mapping[str, int] = field(default_factory=dict)


def _is_subject(c: CheckedChange) -> bool:
    return (c.change.op in ("set", "add") and c.dimension == CATEGORY) or (
        c.change.op == "clear" and c.change.target == "topic"
    )


def _relative_price(
    c: CheckedChange, resolved: Sequence[ResolvedRef], facts: ReferenceFacts
) -> float | None:
    """Prețul RECITIT al produsului la care se raportează schimbarea, sau None."""
    ref = next((r for r in resolved if r.ref_id == c.change.relative_to), None)
    if ref is None or ref.outcome != "exact" or len(ref.product_ids) != 1:
        return None
    product = facts.products.get(ref.product_ids[0])
    price = getattr(product, "price", None)
    return float(price) if price is not None else None


def _common(c: CheckedChange, source: str, turn_id: str) -> dict[str, Any]:
    """Câmpurile comune ale unei propuneri (sursă, tur, tărie)."""
    strength = c.strength if c.strength in ("hard", "soft") else None
    return {"source": source, "turn_id": turn_id, "strength": strength}


def _need_proposals(
    c: CheckedChange,
    source: str,
    turn_id: str,
    needs: NeedVocabulary,
    value: str | float | None,
) -> list[StateUpdateProposal]:
    common = _common(c, source, turn_id)
    relation = c.change.relation or "eq"
    if c.dimension == PRICE:
        keys = {"lte": ["budget_max"], "gte": ["budget_min"]}.get(
            relation, ["budget_max", "budget_min"]
        )
        return [StateUpdateProposal("set_need", key=k, value=value, **common) for k in keys]
    if c.dimension == UNMAPPED:
        # `unmapped` nu e niciodată dur (I25), oricât de explicit ar fi citatul.
        soft = {**common, "strength": "soft"}
        return [StateUpdateProposal("set_need", key=UNMAPPED_KEY, value=value, **soft)]
    if relation == "avoid":
        spec = needs.spec_for(c.dimension)
        exclusion = spec is not None and spec.kind is NeedKind.EXCLUSION
        key = c.dimension if exclusion else RESTRICTION_KEY
        return [StateUpdateProposal("set_need", key=key, value=value, **common)]
    return [StateUpdateProposal("set_need", key=c.dimension, value=value, **common)]


def _structural_proposal(
    c: CheckedChange, source: str, turn_id: str, handle: Handle | None
) -> StateUpdateProposal:
    """`clear` / `remove` / `replace`: operațiile pe handle-uri sau pe tot subiectul."""
    common = _common(c, source, turn_id)
    op = c.change.op
    if op == "clear" and c.change.target == "topic":
        return StateUpdateProposal("clear_topic", **common)
    if op == "clear":
        return StateUpdateProposal("clear_all", **common)
    assert handle is not None  # validarea a respins deja un handle necunoscut (`unknown_handle`)
    if op == "remove":
        return StateUpdateProposal("revoke", key=handle.key, value=handle.value, **common)
    return StateUpdateProposal("supersede", key=handle.key, value=c.canonical_value, **common)


def to_delta(
    interp: TurnInterpretation,
    checked: Sequence[CheckedChange],
    resolved: Sequence[ResolvedRef] = (),
    facts: ReferenceFacts | None = None,
    *,
    handles: Sequence[Handle] = (),
    needs: NeedVocabulary | None = None,
    turn_id: str = "",
) -> TurnDelta:
    """`CheckedChange`-uri → `TurnDelta`. PUR. Nu atinge starea: propunerile le aplică reducerul."""
    needs = needs or NeedVocabulary()
    facts = facts or ReferenceFacts()
    by_handle = {h.handle: h for h in handles}
    counters: dict[str, int] = {}

    thread = interp.thread
    if thread == "aside" and interp.changes:
        thread = "continue"
        counters["aside_with_changes"] = 1
    overflow = (
        max(0, len(interp.changes) - MAX_CHANGES)
        + max(0, len(interp.references) - MAX_REFERENCES)
        + max(0, len(interp.acts) - MAX_ACTS)
    )
    if overflow:
        counters["interpretation_truncated"] = overflow
    if thread == "aside":
        # Paranteza pură: nimic de propus. Identitatea pe stare (I5) o ține reducerul (NX-331).
        return TurnDelta(thread="aside", counters=counters)

    ordered = [c for c in checked if _is_subject(c)] + [c for c in checked if not _is_subject(c)]
    proposals: list[StateUpdateProposal] = []
    ranking: list[RankingSignal] = []
    rejected: list[CheckedChange] = []

    for c in ordered:
        if c.rejected is not None:
            rejected.append(c)
            continue
        if c.provenance == "inferred" or c.strength == "ranking":
            if c.change.op in ("set", "add", "replace") and c.canonical_value is not None:
                ranking.append(
                    RankingSignal(c.dimension, c.canonical_value, c.change.relation or "eq")
                )
            continue
        source = _SOURCE_BY_PROVENANCE[c.provenance]
        if c.change.op in ("clear", "remove", "replace"):
            handle = by_handle.get(c.change.target or "")
            proposals.append(_structural_proposal(c, source, turn_id, handle))
            continue
        if c.dimension == CATEGORY:
            proposals.append(
                StateUpdateProposal(
                    "set_topic",
                    category_key=str(c.canonical_value),
                    **_common(c, source, turn_id),
                )
            )
            continue
        value = c.canonical_value
        if c.change.relative_to is not None:
            value = _relative_price(c, resolved, facts)
            if value is None:
                rejected.append(
                    c.model_copy(update={"rejected": "unknown_reference", "strength": "ranking"})
                )
                continue
        proposals += _need_proposals(c, source, turn_id, needs, value)

    return TurnDelta(
        thread=thread,
        proposals=tuple(proposals),
        ranking=tuple(ranking),
        rejected=tuple(rejected),
        counters=counters,
    )


__all__ = ["RankingSignal", "TurnDelta", "to_delta"]
