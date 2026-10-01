"""NX-375 (`kernel.v6.2`): un produs numit într-o cerere `find` e ce caută clientul.

Rularea pe producție din 2026-10-01 (`KERNEL-LIVE-2026-10-01.md`, clasa A3, k6 T1): «aveți ANUA
Heartleaf 77 toner?». Modelul a declarat referința `name`, dar `find.targets` era gol, deci
plannerul a ignorat-o și a căutat „toner": produsul (fără tip în catalog) n-a intrat în pool, iar
clientul a aflat că „nu apare", deși e în stoc la 30 de lei. Contractul spune deja că resolverul
găsește doar un nume scris întreg și că o căutare aproximativă după nume e a plannerului; pe
`detail`/`compare` o face, pe `find` nu. Acum o referință `name` nefolosită de nimic altceva
(nu e țintă, nu e ancoră `relative_to`) face căutarea pe numele ei. Zero model, zero DB.
"""

from __future__ import annotations

import pytest

from src.conversation.interpretation import ResolvedRef
from tests.test_kernel_planner import _interp, _plan, _search, _state

ANUA = "ANUA Heartleaf 77 toner"


def _name_ref(rid: str = "r1", name: str = ANUA) -> dict:
    return {"id": rid, "text": name, "kind": "name", "name": name}


def _resolved(outcome: str = "not_found", kind: str = "name", rid: str = "r1") -> ResolvedRef:
    return ResolvedRef(
        ref_id=rid,
        kind=kind,
        outcome=outcome,
        product_ids=[],
        source="catalog",
        reason="name_not_found",
    )


def test_the_real_turn_searches_on_the_named_product():
    """k6 T1: referința declarată, `find.targets` gol, plus tipul „toner" ca schimbare."""
    interp = _interp(
        acts=[{"kind": "find", "query": "aveti ANUA Heartleaf 77 toner?"}],
        references=[_name_ref()],
        changes=[
            {
                "op": "set",
                "dimension": "product_type",
                "relation": "eq",
                "value": "toner de fata",
                "quote": "toner",
            }
        ],
    )
    args = _search(_plan("sole-ro", interp, resolved=(_resolved(),)))
    assert (args.query, args.product_name) == (ANUA, ANUA)


@pytest.mark.parametrize("outcome", ["not_found", "exact", "ambiguous"])
def test_any_live_resolution_of_the_name_searches_on_it(outcome):
    """Găsit întreg (`exact`), între mărimi (`ambiguous`) sau negăsit: produsul numit e cererea."""
    interp = _interp(acts=[{"kind": "find"}], references=[_name_ref()])
    args = _search(_plan("sole-ro", interp, resolved=(_resolved(outcome),)))
    assert args.query == ANUA


def test_on_another_domain_the_rule_is_the_same():
    name = "Samsung Galaxy S24"
    interp = _interp(acts=[{"kind": "find"}], references=[_name_ref(name=name)])
    args = _search(_plan("electronics", interp, _state("telefoane"), resolved=(_resolved(),)))
    assert (args.query, args.product_name) == (name, name)


def test_a_reference_anchoring_a_change_is_not_the_request():
    """«ceva ca ANUA, mai ieftin»: ANUA e ancora prețului, clientul caută ALTCEVA."""
    interp = _interp(
        acts=[{"kind": "find", "query": "ceva mai ieftin"}],
        references=[_name_ref()],
        changes=[
            {
                "op": "set",
                "dimension": "price",
                "relation": "lte",
                "relative_to": "r1",
                "quote": "mai ieftin",
            }
        ],
    )
    args = _search(
        _plan(
            "sole-ro", interp, _state(product_type="toner de fata"), resolved=(_resolved("exact"),)
        )
    )
    assert args.query != ANUA and args.product_name is None


def test_a_reference_targeted_by_another_act_stays_with_it():
    interp = _interp(
        acts=[{"kind": "detail", "targets": ["r1"]}, {"kind": "find", "query": "si un ser"}],
        references=[_name_ref()],
    )
    planned = _plan("sole-ro", interp, resolved=(_resolved("exact"),))
    for plan in planned.plans:
        if plan.search_args is not None:
            assert plan.search_args.query != ANUA


@pytest.mark.parametrize(("outcome", "kind"), [("stale", "name"), ("not_found", "attribute")])
def test_a_stale_or_reclassified_reference_is_not_a_product_name(outcome, kind):
    """`stale`: nu mai e în catalog. Reclasificată (un „nume" care numește o proprietate, I24)."""
    interp = _interp(acts=[{"kind": "find", "query": "un toner"}], references=[_name_ref()])
    planned = _plan(
        "sole-ro",
        interp,
        _state(product_type="toner de fata"),
        resolved=(_resolved(outcome, kind),),
    )
    args = _search(planned)
    assert args.query != ANUA and args.product_name is None


def test_a_find_without_a_name_is_unchanged():
    interp = _interp(acts=[{"kind": "find", "query": "un toner"}])
    args = _search(_plan("sole-ro", interp, _state(product_type="toner de fata")))
    assert args.product_name is None


# --- recenzia adversarială ---------------------------------------------------------------------


def _cheaper(relative_to: str | None = None) -> dict:
    change = {"op": "set", "dimension": "price", "relation": "lte", "quote": "mai ieftin"}
    return {**change, "relative_to": relative_to} if relative_to else change


@pytest.mark.parametrize(
    "said",
    [
        "ai ceva mai ieftin decat Aurelia?",  # d32: limita relativă, fără `relative_to`
        "Ser Corvina Luminozitate e prea scump, ai ceva asemanator mai ieftin?",  # e34
    ],
)
def test_a_relative_price_without_an_anchor_keeps_the_name_as_the_anchor(said):
    """Interpretările REALE stocate (raportul NX-335): modelul uită uneori `relative_to`; numele e
    atunci ancora prețului, iar clientul vrea ALTCEVA. Pe prima variantă căuta chiar produsul."""
    interp = _interp(
        acts=[{"kind": "find", "query": said}],
        references=[_name_ref(name="Aurelia")],
        changes=[_cheaper()],
    )
    planned = _plan("sole-ro", interp, _state(product_type="ser de fata"), resolved=(_resolved(),))
    args = _search(planned)
    assert args.product_name is None and args.query != "Aurelia"


def test_two_unused_names_are_not_guessed():
    interp = _interp(
        acts=[{"kind": "find", "query": "ANUA sau COSRX?"}],
        references=[_name_ref("r1", "ANUA Heartleaf"), _name_ref("r2", "COSRX Snail")],
    )
    planned = _plan(
        "sole-ro",
        interp,
        _state(product_type="toner de fata"),
        resolved=(_resolved(rid="r1"), _resolved(rid="r2")),
    )
    assert _search(planned).product_name is None


def test_a_name_search_ignores_an_old_shelf_and_budget():
    """«sub 50 lei» acum câteva ture, pe raftul telefoanelor: clientul cere acum un produs pe nume;
    filtrele vechi l-ar ascunde, iar unealta ar spune că „nu există ca atare"."""
    from tests.test_kernel_planner import _need

    budget = _need("budget_max", 50.0, strength="hard")
    interp = _interp(acts=[{"kind": "find"}], references=[_name_ref(name="Samsung Galaxy S24")])
    planned = _plan(
        "electronics", interp, _state("telefoane", needs=(budget,)), resolved=(_resolved(),)
    )
    args = _search(planned)
    assert args.product_name == "Samsung Galaxy S24"
    assert args.category is None and args.price_max is None
