"""Extragerea DRUMULUI dintr-o traversare de relații. Pur: zero I/O, zero ceas, zero SQL.

`traverse_relation_chain` întoarce, pentru fiecare nod atins, cel mai bun succesor al lui. Asta
include și noduri de pe ramuri care nu pornesc din ancoră: explorarea a trecut și pe acolo. Modulul
ăsta extrage din ele **lanțul propriu-zis** — succesorul ancorei, succesorul aceluia, și așa mai
departe.

De ce e o funcție separată și nu încă un `where` în SQL: e o operație pe un graf deja citit, deci
nu are ce căuta într-un query, iar ca funcție pură se poate testa fără Postgres pe exact cazurile
care contează (ramură ruptă, nod repetat, lanț mai scurt decât plafonul). Aceeași linie ca la
`src/channels/web/render_v2.py`: ce se poate calcula pur, se calculează pur, ca două rulări să dea
același rezultat.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

__all__ = ["walk_chain"]


def walk_chain(
    hops: Sequence[Mapping[str, Any]], anchor_id: str, max_steps: int
) -> list[dict[str, Any]]:
    """Lanțul care pornește din `anchor_id`, cel mult `max_steps` pași, în ordine.

    `hops` = rândurile de la `traverse_relation_chain` (`{id, parent, depth, position}`), în care
    fiecare `parent` apare cel mult o dată. Se merge din succesor în succesor până când nu mai
    există unul, s-a atins plafonul, sau pasul următor ar repeta un produs deja din lanț.

    Garda pe produse deja văzute e a doua plasă, nu prima: clauza `CYCLE` din SQL a eliminat deja
    ciclurile. Rămâne aici fiindcă un lanț care se întoarce la un pas anterior ar fi un sfat absurd
    („pune cremă, apoi tonic, apoi cremă"), iar costul verificării e un set.

    Gol = ancora n-are succesor. Pur și determinist: aceeași intrare, același rezultat."""
    if max_steps <= 0:
        return []
    successor: dict[str, Mapping[str, Any]] = {}
    for hop in hops:
        parent = hop.get("parent")
        # Primul câștigă: rândurile vin deja ordonate (adâncime, poziția muchiei, id), iar un
        # `parent` duplicat ar însemna că interogarea s-a schimbat sub noi. Nu ne prefacem că
        # alegem — păstrăm ordinea pe care contractul o promite.
        if isinstance(parent, str) and parent not in successor:
            successor[parent] = hop
    chain: list[dict[str, Any]] = []
    current, seen = anchor_id, {anchor_id}
    while len(chain) < max_steps:
        nxt = successor.get(current)
        if nxt is None:
            break
        nxt_id = str(nxt.get("id") or "")
        if not nxt_id or nxt_id in seen:
            break
        chain.append(dict(nxt))
        seen.add(nxt_id)
        current = nxt_id
    return chain
