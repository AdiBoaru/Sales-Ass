"""Fixture pentru `tests/test_state_writers_inventory.py`.

Un exemplu din FIECARE forma 1-5 din card (NX-327), plus un alias si un acces dinamic
(`unresolved`). NU e scanat ca parte din inventarul real -- `scripts/state_writers.py`
scaneaza `src/**/*.py`, nu `tests/`. Stand-in-urile de mai jos (`StateUpdateProposal`,
`ToolResult`, `replace`) sunt locale, deliberat: extractorul lucreaza pe NUME (cum spune
cardul: "urmat o singura data prin numele functiei, fara analiza de flux"), nu pe
rezolvare de import -- daca testul ar importa clasele reale din `src.*`, n-ar mai dovedi
ca extractorul le recunoaste pe nume, ci doar ca stie sa urmareasca un import.
"""

from __future__ import annotations

from dataclasses import dataclass, replace


@dataclass
class StateUpdateProposal:
    op: str
    key: str | None = None
    value: object = None


@dataclass
class ToolResult:
    ok: bool = True
    state_patch: dict | None = None


def form1_assign(ctx):
    """Forma 1: atribuire directa pe `ctx.state.<f>`."""
    ctx.state.pending_question = {"field": "x"}


def form1_alias_mutating_call(ctx):
    """Forma 1 (edge case din card): alias local (`s = ctx.state`) + apel mutant."""
    s = ctx.state
    s.constraints.update({"a": 1})


def form1_subscript_assign(ctx):
    """Forma 1: subscript pe atributul starii (`ctx.state.<f>[...] = ...`)."""
    ctx.state.constraints["a"] = 1


def form2_patch_subscript(ctx):
    """Forma 2: `ctx.state_patch["<f>"] = ...`."""
    ctx.state_patch["active_search"] = {"pool": []}


def form2_patch_update(ctx):
    """Forma 2: `state_patch.update({...})`."""
    ctx.state_patch.update({"displayed_products": []})


def form2_tool_result():
    """Forma 2 (edge case din card): `ToolResult(state_patch={...})`."""
    return ToolResult(ok=True, state_patch={"active_search": None})


def form3_dict_literal(base_state):
    """Forma 3: literal de dict cu cheia campului, ca in `processor._build_new_state`."""
    new_state = {**base_state, "constraints": {}}
    return new_state


def form4_dataclasses_replace(state):
    """Forma 4 (edge case din card): `dataclasses.replace(state, f=...)`."""
    return replace(state, active_search=None)


def form5_proposal_constructor(ctx):
    """Forma 5: constructor de propunere `StateUpdateProposal(op="<op>", ...)`."""
    ctx.state_proposals.append(StateUpdateProposal("set_need", key="x", value="y"))


def _need_helper(ctx):
    """Fabrica de propuneri -- construieste direct `StateUpdateProposal(...)`."""
    return [StateUpdateProposal("set_topic", key="category_key", value="x")]


def form5_proposal_call(ctx):
    """Forma 5 (indirect): apel catre o functie care CONSTRUIESTE propunerea, urmarit o
    singura data prin numele functiei, fara analiza de flux."""
    ctx.state_proposals.extend(_need_helper(ctx))


def unresolved_setattr_dynamic(ctx, name, value):
    """Failure case din card: acces dinamic -> `unresolved`, fara intrare declarata ->
    testul de inventar pica."""
    setattr(ctx.state, name, value)
