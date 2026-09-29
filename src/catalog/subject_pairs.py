"""NX-348 — perechile (raft, tip de produs) care EXISTĂ în catalog, pentru rafinarea subiectului.

Pe calea interpretată (kernel.v3.0), completarea unei jumătăți goale a subiectului e RAFINARE doar
când perechea rezultată există în catalog: «telefoane» + «un laptop» nu e o rafinare, e alt subiect
(recenzia NX-348, constatarea 2). Reducerul e PUR, deci decizia se ia AICI, pe datele turului, și
călătorește pe propunere (`StateUpdateProposal.pair_compatible`), ca poarta și commit-ul să vadă
aceeași decizie. Modulul NU e în registrul kernelului: e partea de I/O, ca `reference_facts`.

Un raft conține un tip când un produs servabil de acel tip stă pe raft sau în subarborele lui
(`path`), aceeași definiție de subarbore ca vocabularul. Doar categoria PRIMARĂ: pe SOLE
`product_category_map` e gol (măsurat 2026-09-16); un catalog cu apartenență multiplă ar pierde
perechi, adică ar parca mai des, niciodată invers.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from src.db.queries.catalog import subject_type_pairs


def pair_exists(shelf: str, kind: str, rows: Sequence[tuple[str, str]], vocab: Any) -> bool:
    """Perechea (raft, tip) are cel puțin un produs servabil. Raftul se caută în vocabular după
    cheie (calea lui); un raft necunoscut vocabularului nu poate dovedi nimic ⇒ `False`. PUR."""
    entries = getattr(vocab, "categories", ()) or ()
    path = next((e.path for e in entries if e.key == shelf), None)
    if not path:
        return False
    return any(
        kind == row_kind and (row_path == path or row_path.startswith(path + "/"))
        for row_path, row_kind in rows
    )


__all__ = ["pair_exists", "subject_type_pairs"]
