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

import dataclasses

import pytest

from src.agent.turn_planner import plan_turn
from src.conversation.interpretation import CheckedChange, ResolvedRef
from src.conversation.state_v2 import ConversationStateV2
from tests.kernel import fixture_catalog as fc
from tests.test_kernel_planner import _gate, _interp, _need, _plan, _search, _state

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


# --- recenzia PR-ului (#537, verificator independent) -------------------------------------------


def _hit(outcome: str, ids: list[str], rid: str = "r1") -> ResolvedRef:
    reason = {"exact": "named", "ambiguous": "catalog_tie"}.get(outcome, "name_not_found")
    return ResolvedRef(
        ref_id=rid, kind="name", outcome=outcome, product_ids=ids, source="catalog", reason=reason
    )


def _find_named(*, targeted: bool, query: str | None = None, changes=()) -> object:
    act: dict = {"kind": "find", **({"query": query} if query else {})}
    if targeted:
        act["targets"] = ["r1"]
    return _interp(acts=[act], references=[_name_ref()], changes=list(changes))


@pytest.mark.parametrize("targeted", [True, False], ids=["in_targets", "untargeted"])
def test_b1_a_name_not_found_searches_on_it_wherever_the_model_put_it(targeted):
    """B1: cu referința în `find.targets` (resolverul `not_found`), căutarea rula pe „toner", cu
    `product_name` setat, dar textul NU era numele."""
    interp = _find_named(targeted=targeted, query="aveti ANUA Heartleaf 77 toner?")
    args = _search(
        _plan("sole-ro", interp, _state(product_type="toner de fata"), resolved=(_resolved(),))
    )
    assert (args.query, args.product_name) == (ANUA, ANUA)


@pytest.mark.parametrize("targeted", [True, False], ids=["in_targets", "untargeted"])
def test_b1_an_exact_name_reaches_the_client_as_the_product(targeted):
    """B1: `exact` ⇒ produsul găsit de resolver (id-ul recitit din catalog, I1) e răspunsul: planul
    `detail` pe el, ca rândul `detail` pe o țintă `exact`. Pe codul vechi id-ul se pierdea."""
    planned = _plan(
        "sole-ro",
        _find_named(targeted=targeted),
        _state(product_type="toner de fata"),
        resolved=(_hit("exact", ["p-anua"]),),
    )
    plan = planned.plans[planned.primary]
    assert (plan.executor, plan.product_ids) == ("detail", ["p-anua"])


@pytest.mark.parametrize("targeted", [True, False], ids=["in_targets", "untargeted"])
def test_b1_an_ambiguous_name_answers_about_all_candidates(targeted):
    """B1: `ambiguous` pe cel mult trei candidați (mărimile aceluiași produs) ⇒ răspunsul despre
    toți, ca `act_both` pe o citire (`detail` cu ≥ 2 candidați). Peste trei, lista o dă căutarea pe
    nume (o citire ar întreba; o cerere `find` primește lista)."""
    ids = ["p-250", "p-500", "p-pad"]
    planned = _plan(
        "sole-ro",
        _find_named(targeted=targeted),
        _state(product_type="toner de fata"),
        resolved=(_hit("ambiguous", ids),),
    )
    plan = planned.plans[planned.primary]
    assert (plan.executor, plan.product_ids) == ("detail", ids)
    many = _plan(
        "sole-ro",
        _find_named(targeted=targeted),
        _state(product_type="toner de fata"),
        resolved=(_hit("ambiguous", [*ids, "p-mini"]),),
    )
    assert _search(many).product_name == ANUA


def test_b1_the_full_chain_on_electronics_serves_the_named_phone():
    """B1 pe lanțul complet (validator, resolver pe catalogul fixture-ului, reducer, poartă,
    planner): «aveti Apple Phone 2 256 GB?» cu referința în `find.targets` ⇒ resolverul `exact`
    pe el-02; pe codul vechi planul era `search('Telefoane', product_name=None)`."""
    said = "aveti Apple Phone 2 256 GB?"
    interp = _interp(
        acts=[{"kind": "find", "targets": ["r1"], "query": said}],
        references=[_name_ref(name="Apple Phone 2 256 GB")],
    )
    step = fc.kernel_step("electronics", _state("telefoane"), interp, said)
    hit = next(r for r in step.resolved if r.ref_id == "r1")
    assert (hit.outcome, hit.product_ids) == ("exact", ["el-02"])
    plan = step.planned.plans[step.planned.primary]
    assert (plan.executor, plan.product_ids) == ("detail", ["el-02"])


def test_b3_the_name_search_inherits_no_old_subject_filter():
    """B3: pe modă o restricție veche (poliester) ajungea `exclude`, pe electronice o culoare veche
    ajungea filtru. Căutarea pe nume poartă doar ce spune ACEST tur."""
    fashion = _plan(
        "fashion",
        _find_named(targeted=False),
        _state("rochii", needs=(_need("restriction", "poliester"),)),
        resolved=(_resolved(),),
    )
    args = _search(fashion)
    assert not args.exclude and args.category is None
    electronics = _plan(
        "electronics",
        _find_named(targeted=False),
        _state("telefoane", needs=(_need("color", "negru"),)),
        resolved=(_resolved(),),
    )
    args = _search(electronics)
    assert not args.concerns and not args.features and not args.prefer and args.brand is None
    assert "name_unscoped" in electronics.gaps


def test_b4_a_budget_said_this_turn_still_applies_and_the_old_one_is_a_gap():
    """B4: «aveti Apple Phone 2 sub 500 lei?»: bugetul spus ACUM rămâne filtru; cel vechi se scoate,
    dar nu în tăcere (golul `name_unscoped`)."""
    now = dataclasses.replace(_need("budget_max", 500.0, strength="hard"), updated_revision=1)
    said = {"op": "set", "dimension": "price", "relation": "lte", "number": 500, "quote": "sub 500"}
    planned = _plan(
        "electronics",
        _find_named(targeted=False, changes=[said]),
        _state("telefoane", needs=(now,)),
        resolved=(_resolved(),),
    )
    assert _search(planned).price_max == 500.0
    assert "name_unscoped" in planned.gaps  # raftul vechi „telefoane" a fost scos
    old = _plan(
        "electronics",
        _find_named(targeted=False),
        _state(needs=(_need("budget_max", 500.0, strength="hard"),)),
        resolved=(_resolved(),),
    )
    assert _search(old).price_max is None and "name_unscoped" in old.gaps


def test_b6_a_name_alone_is_a_subject_for_the_gate():
    """B6: un `find` fără cuvinte de cerere și fără subiect, doar cu un nume, primea întrebarea de
    raft a porții. Numele e subiectul: căutarea rulează pe el."""
    said = "Samsung Galaxy S24"
    interp = _interp(acts=[{"kind": "find"}], references=[_name_ref(name=said)])
    step = fc.kernel_step("electronics", ConversationStateV2(), interp, said)
    assert step.outcome.decision.reason != "no_subject"
    assert _search(step.planned).product_name == said


@pytest.mark.parametrize("rejected", [None, "unknown_reference"])
def test_b8_the_relative_price_guard_reads_accepted_changes(rejected):
    """B8: o limită relativă de preț RESPINSĂ (validator sau delta) nu mai face din nume ancora."""
    raw = _cheaper()
    interp = _interp(
        acts=[{"kind": "find", "query": "ceva ca Aurelia"}],
        references=[_name_ref(name="Aurelia")],
        changes=[raw],
    )
    checked = CheckedChange(
        change=interp.changes[0],
        dimension="price",
        canonical_value=None,
        provenance="explicit",
        strength="soft",
        rejected=rejected,
    )
    accepted = [checked] if rejected is None else []
    planned = plan_turn(
        interp,
        _state(product_type="ser de fata"),
        (),
        (_resolved(),),
        _gate(),
        changed=False,
        pack=fc.pack("sole-ro"),
        vocab=fc.vocabulary("sole-ro"),
        locale="ro",
        checked=accepted,
    )
    name = _search(planned).product_name
    assert name == (None if rejected is None else "Aurelia")
