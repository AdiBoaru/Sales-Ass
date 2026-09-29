"""NX-348 — perechile (raft, tip de produs) care EXISTĂ în catalog, pentru rafinarea subiectului.

Pe calea interpretată (kernel.v3.0), completarea unei jumătăți goale a subiectului e RAFINARE doar
când perechea rezultată există în catalog: «telefoane» + «un laptop» nu e o rafinare, e alt subiect
(recenzia NX-348). Reducerul e PUR, deci verificarea se face ÎNAINTEA lui, pe datele turului, iar
propunerea poartă PERECHEA verificată (`StateUpdateProposal.pair_verified`), nu un bool: reducerul
rafinează doar dacă perechea pe care o aplică e exact cea verificată, deci un `resume` sau o scriere
concurentă între poartă și commit nu pot transforma o verificare veche într-o rafinare greșită.

Logica PURĂ (`proposal_pair`, `mark_pairs`, `pair_exists`) e aici, cu un singur proprietar:
orchestratorul (citirea reală) și `kernel_step` din teste (catalogul de fixture) o folosesc la fel.
Citirea (`subject_type_pairs`, SQL în `db/queries/catalog.py`) e partea de I/O; modulul NU e în
registrul kernelului, ca `reference_facts`.

Un raft conține un tip când un produs servabil de acel tip stă pe raft sau în subarborele lui
(`path`). Doar categoria PRIMARĂ: pe SOLE `product_category_map` e gol (măsurat 2026-09-16); un
catalog cu apartenență multiplă ar pierde perechi, adică ar parca mai des, niciodată invers.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from typing import Any

from src.db.queries.catalog import subject_type_pairs


def _base_topic(state: Any, thread: str, category: str | None) -> Any:
    """Subiectul pe care se aplică propunerea: cel parcat după un `resume` sau când propunerea
    numește raftul parcat (reducerul face atunci un SCHIMB), altfel cel curent."""
    parked = getattr(state, "parked", None)
    if parked is not None and (
        thread == "resume" or (category is not None and category == parked.topic.category_key)
    ):
        return parked.topic
    return state.topic


def proposal_pair(proposal: Any, state: Any, thread: str) -> tuple[str, str] | None:
    """Perechea (raft, tip) care ar rezulta din propunerea de subiect, sau `None` când îi lipsește
    o jumătate (atunci nu e nimic de verificat). Un tip DEDUS (`type_learned`) e jumătate goală."""
    if getattr(proposal, "op", None) != "set_topic" or proposal.origin != "interpretation":
        return None
    base = _base_topic(state, thread, proposal.category_key)
    shelf = proposal.category_key or base.category_key
    kind = proposal.product_type or (None if base.type_learned else base.product_type)
    if shelf is None or kind is None:
        return None
    return (shelf, kind)


def pair_exists(shelf: str, kind: str, rows: Sequence[tuple[str, str]], vocab: Any) -> bool:
    """Perechea (raft, tip) are cel puțin un produs servabil. Raftul se caută în vocabular după
    cheie (calea lui; fără cale, cheia); un raft necunoscut vocabularului ⇒ `False`. PUR."""
    entries = getattr(vocab, "categories", ()) or ()
    entry = next((e for e in entries if e.key == shelf), None)
    if entry is None:
        return False
    path = entry.path or entry.key
    return any(
        kind == row_kind and (row_path == path or row_path.startswith(path + "/"))
        for row_path, row_kind in rows
    )


def _umbrella_shelf(proposal: Any, state: Any, thread: str) -> str | None:
    """NX-352: raftul pe care ar rămâne o UMBRELĂ fără tip spus clar (raftul turului, altfel cel
    curent), sau `None` când propunerea nu e o astfel de umbrelă."""
    if (
        getattr(proposal, "op", None) != "set_topic"
        or proposal.origin != "interpretation"
        or not getattr(proposal, "type_umbrella", ())
        or proposal.product_type
    ):
        return None
    return proposal.category_key or _base_topic(state, thread, proposal.category_key).category_key


def needs_pairs(delta: Any, state: Any) -> bool:
    """Turul are o propunere de subiect cu ambele jumătăți: doar atunci merită citirea."""
    return any(
        proposal_pair(p, state, delta.thread) or _umbrella_shelf(p, state, delta.thread)
        for p in delta.proposals
    )


def mark_pairs(delta: Any, state: Any, rows: Sequence[tuple[str, str]], vocab: Any) -> Any:
    """Propunerile de subiect primesc perechea VERIFICATĂ, când există în catalog. PUR. O umbrelă
    (NX-352) primește perechea primului ei cod care există pe raft: reducerul păstrează raftul la
    alt fel de produs doar pe ea."""
    out = []
    for p in delta.proposals:
        shelf = _umbrella_shelf(p, state, delta.thread)
        if shelf is not None:
            codes = [k for k in p.type_umbrella if pair_exists(shelf, k, rows, vocab)]
            code = codes[0] if codes else None
            if code is not None:
                p = replace(p, pair_verified=(shelf, code))
        else:
            pair = proposal_pair(p, state, delta.thread)
            if pair is not None and pair_exists(pair[0], pair[1], rows, vocab):
                p = replace(p, pair_verified=pair)
        out.append(p)
    return replace(delta, proposals=tuple(out))


__all__ = ["mark_pairs", "needs_pairs", "pair_exists", "proposal_pair", "subject_type_pairs"]
