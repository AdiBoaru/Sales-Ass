"""NX-203 — split tuning vs holdout, cu felii SINGLE-USE per gate (D13 / ADR secțiunea 3).

Regula anti-contaminare: pe **tuning** se aleg documentele/embeddings/reranker/ponderi; **holdout**
e partiționat în felii dedicate — H1 (gate switch NX-207), H2 (gate NX-209), H3 (evaluarea NX-210).
O felie se deschide O SINGURĂ DATĂ la gate-ul ei; după deschidere e „arsă" (nu mai validează nimic).

Atribuirea e DETERMINISTĂ (hash pe id, fără random — reproductibilă, stabilă la re-rulare) și
STRATIFICATĂ pe categorie, ca fiecare felie să acopere aceleași categorii de query.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from enum import Enum

from src.evals.retrieval.schema import QrelsQuery, QrelsSet


class Split(str, Enum):
    tuning = "tuning"
    holdout_h1 = "holdout_h1"  # gate switch documente (NX-207)
    holdout_h2 = "holdout_h2"  # gate search tool (NX-209)
    holdout_h3 = "holdout_h3"  # evaluarea prototipului (NX-210)


# Proporții țintă. Tuning majoritar; câte o felie de holdout per gate.
_WEIGHTS: list[tuple[Split, float]] = [
    (Split.tuning, 0.55),
    (Split.holdout_h1, 0.15),
    (Split.holdout_h2, 0.15),
    (Split.holdout_h3, 0.15),
]


def _bucket(qid: str) -> float:
    """[0,1) determinist din id — stabil între rulări (fără Math.random/hash-seed-ul procesului)."""
    h = hashlib.sha256(qid.encode("utf-8")).hexdigest()
    return int(h[:8], 16) / 0xFFFFFFFF


def assign_split(qid: str) -> Split:
    """Felia unei interogări, determinist din id. Pragurile cumulative din `_WEIGHTS`."""
    b = _bucket(qid)
    acc = 0.0
    for split, w in _WEIGHTS:
        acc += w
        if b < acc:
            return split
    return Split.tuning


def partition(qset: QrelsSet) -> dict[Split, list[QrelsQuery]]:
    """Împarte qrels-ul pe felii, STRATIFICAT pe categorie: atribuirea deterministă se aplică în
    cadrul fiecărei categorii, ca feliile să fie echilibrate pe tipuri de query, nu doar global."""
    by_cat: dict[str | None, list[QrelsQuery]] = defaultdict(list)
    for q in qset.queries:
        by_cat[q.category].append(q)

    out: dict[Split, list[QrelsQuery]] = {s: [] for s in Split}
    for _cat, items in by_cat.items():
        for q in sorted(items, key=lambda x: x.id):  # ordine stabilă
            out[assign_split(q.id)].append(q)
    return out


def holdout_slice_for_gate(gate: str) -> Split:
    """Maparea gate → felie single-use. Deschiderea unei felii la alt gate = contaminare."""
    mapping = {
        "NX-207": Split.holdout_h1,
        "NX-209": Split.holdout_h2,
        "NX-210": Split.holdout_h3,
    }
    if gate not in mapping:
        raise ValueError(f"gate necunoscut pentru holdout: {gate!r}")
    return mapping[gate]
