"""NX-333 — plannerul turului (`src/agent/turn_planner.py`), pasul 4b al kernelului `kernel.v1.1`.

Ce se verifică, în ordinea cardului:

- tabelul act → executor (§1), rând cu rând, pe cele cinci pachete (patru de fixture + SOLE);
- multi-act: cel mult două planuri, mutația înaintea citirii, `depends_on`, al treilea act nefăcut;
- `SearchArgs` derivat din stare (§2) și ce nu încape în el, în `gaps` (§3);
- proprietățile I1 (id-urile unui plan vin doar din referințele rezolvate ale turului) și I7 (niciun
  `price_max` și niciun filtru dintr-o nevoie ne-dură), fiecare picată de o mutație;
- calea planificată a uneltei: fără moștenire din sesiune și fără `_typed_constraints` pe mesajul
  brut (pe SQL), cu garda NX-319 care nu scoate nimic din ce a pus plannerul;
- testul de pondere din §4: `rank_terms` ca cheie secundară stă sub o potrivire de fațetă.

Zero model, zero DB: pachetele trec prin loaderul de producție, vocabularul e al fixture-ului."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from src.agent import turn_planner as tp
from src.agent.turn_planner import GAPS, PlannedTurn, plan_turn
from src.catalog.vocabulary import (
    CATEGORY_DIMENSION,
    CatalogVocabulary,
    ResolutionStatus,
    VocabEntry,
    resolve_any,
)
from src.conversation.ambiguity_gate import GateOutcome, target_question_key
from src.conversation.delta import RankingSignal
from src.conversation.interpretation import (
    AmbiguityDecision,
    ResolvedRef,
    TurnInterpretation,
)
from src.conversation.needs import MAX_UNMAPPED_PER_TOPIC, NeedVocabulary
from src.conversation.state_v2 import (
    HARD_CAPABLE_SOURCES,
    ConversationStateV2,
    DisplayedRef,
    Need,
    References,
    Topic,
)
from src.db.queries.fusion import RANK_WEIGHTS, fuse_candidates
from src.domain.loader import load_domain_pack
from src.models import BusinessConfig, Contact, InboundMessage, TurnContext
from src.tools import catalog_tools as ct
from src.worker.runner import PipelineDeps
from tests.kernel import fixture_catalog as fc
from tests.kernel import replay

PACKS = (*replay.FIXTURE_PACKS, "sole-ro")
#: Un raft real al pachetului SOLE (fără produse de fixture, vocabularul lui e gol).
SOLE_SHELF = "ten-ingrijirea-tenului"


# --- utilitare -----------------------------------------------------------------------------------


def _interp(**compact: Any) -> TurnInterpretation:
    return TurnInterpretation.model_validate(replay.expand_interpretation(compact))


def _gate(
    verdict: str = "act",
    reason: str = "clear",
    question: str | None = None,
    asked_key: str | None = None,
    skipped: tuple[int, ...] = (),
) -> GateOutcome:
    return GateOutcome(
        AmbiguityDecision(verdict=verdict, reason=reason, question=question),  # type: ignore[arg-type]
        asked_key=asked_key,
        asked_kind="pending" if asked_key else None,
        skipped_acts=skipped,
    )


def _ref(
    ref_id: str,
    outcome: str,
    ids: list[str],
    *,
    kind: str = "ordinal",
    reason: str | None = None,
) -> ResolvedRef:
    return ResolvedRef(
        ref_id=ref_id,
        kind=kind,
        outcome=outcome,  # type: ignore[arg-type]
        product_ids=ids,
        source="shown_now",
        reason=reason,
    )


def _need(key: str, value: Any, strength: str = "soft", source: str = "user_explicit") -> Need:
    return Need(
        key=key,
        operator="eq",
        normalized_value=value,
        strength=strength,
        status="active",
        source=source,
    )


def _state(
    category: str | None = None,
    *,
    needs: tuple[Need, ...] = (),
    active_search: dict | None = None,
    product_type: str | None = None,
) -> ConversationStateV2:
    return ConversationStateV2(
        revision=1,
        topic=Topic(category_key=category, product_type=product_type),
        needs=needs,
        active_search=active_search,
    )


def _shelf(name: str) -> str:
    if name == "sole-ro":
        return SOLE_SHELF
    return replay.load_pack(name)["categories"][0]["key"]


def _label(name: str) -> str:
    """Eticheta raftului, cum o dă vocabularul (SOLE n-are vocabular de fixture: cheia)."""
    if name == "sole-ro":
        return SOLE_SHELF
    return replay.load_pack(name)["categories"][0]["name"]


def _ids(name: str) -> list[str]:
    ids = list(fc.products(name))
    return ids[:4] if ids else ["p1", "p2", "p3", "p4"]


def _plan(
    name: str,
    interp: TurnInterpretation,
    state: ConversationStateV2 | None = None,
    *,
    resolved: tuple[ResolvedRef, ...] = (),
    gate: GateOutcome | None = None,
    ranking: tuple[RankingSignal, ...] = (),
    changed: bool = False,
    locale: str = "ro",
    loaded: Any = None,
) -> PlannedTurn:
    return plan_turn(
        interp,
        state or ConversationStateV2(),
        ranking,
        resolved,
        gate or _gate(),
        changed=changed,
        pack=loaded if loaded is not None else fc.pack(name),
        vocab=fc.vocabulary(name),
        locale=locale,
    )


def _only(planned: PlannedTurn):
    assert len(planned.plans) == 1, planned
    return planned.plans[0]


def _journey_turn(journey_id: str, index: int) -> PlannedTurn:
    journey = next(j for j in replay.load_journeys() if j.journey_id == journey_id)
    return fc.planned_turn(journey, index)


# --- tabelul act → executor, pe cinci pachete -----------------------------------------------------


@pytest.mark.parametrize("name", PACKS)
def test_find_without_subject_words_or_needs_is_the_gates_question(name):
    """Rândul 1: poarta a dat `must_ask` cu întrebare ⇒ un singur plan `ask`."""
    gate = _gate("must_ask", "no_subject", "Ce cauți?", asked_key="subject")
    plan = _only(_plan(name, _interp(acts=[{"kind": "find"}]), gate=gate))
    assert plan.executor == "ask" and plan.product_ids == [] and plan.search_args is None


@pytest.mark.parametrize("name", PACKS)
def test_find_without_anything_and_no_question_is_reply_only_and_counted(name):
    """P6: poarta n-a întrebat (câștig mic / deja întrebat) și nu e ce căuta ⇒ `reply_only`,
    numărat în `gaps`, nu o căutare pe nimic."""
    planned = _plan(name, _interp(acts=[{"kind": "find"}]), gate=_gate("act", "already_asked"))
    assert _only(planned).executor == "reply_only"
    assert planned.gaps == ("no_subject",)


@pytest.mark.parametrize("name", PACKS)
def test_find_with_a_known_subject_and_no_words_searches_the_subject_label(name):
    """«Ce recomanzi?» după ce clientul și-a construit subiectul: căutarea vine din stare, cu
    eticheta raftului drept cerere (workaroundul declarat în contract)."""
    state = _state(_shelf(name))
    plan = _only(_plan(name, _interp(acts=[{"kind": "find", "query": "ce recomanzi"}]), state))
    assert plan.executor == "search"
    assert plan.search_args.query == _label(name)
    assert plan.search_args.category == _shelf(name)


@pytest.mark.parametrize("name", PACKS)
def test_find_with_words_searches_the_words(name):
    state = _state(_shelf(name))
    plan = _only(_plan(name, _interp(acts=[{"kind": "find", "query": "ceva elegant"}]), state))
    assert plan.executor == "search" and plan.search_args.query == "ceva elegant"


@pytest.mark.parametrize("name", PACKS)
def test_find_with_a_confirmation_still_searches(name):
    """Verdictul `act` + `confirm_implicit`: căutarea rulează, întrebarea o pune compunerea."""
    gate = _gate("act", "confirm_implicit", "Să înțeleg că e vorba de X?", asked_key="k")
    state = _state(_shelf(name))
    plan = _only(_plan(name, _interp(acts=[{"kind": "find", "query": "ceva"}]), state, gate=gate))
    assert plan.executor == "search"


@pytest.mark.parametrize("name", PACKS)
def test_show_more_pages_an_active_session_when_nothing_changed(name):
    state = _state(_shelf(name), active_search={"fp": "x", "pool": ["a"], "cursor": 0})
    plan = _only(_plan(name, _interp(acts=[{"kind": "show_more"}]), state))
    assert plan.executor == "page" and plan.search_args is None


@pytest.mark.parametrize("name", PACKS)
def test_show_more_with_changes_is_a_refinement(name):
    state = _state(_shelf(name), active_search={"fp": "x", "pool": ["a"], "cursor": 0})
    plan = _only(_plan(name, _interp(acts=[{"kind": "show_more"}]), state, changed=True))
    assert plan.executor == "search" and plan.search_args.category == _shelf(name)


@pytest.mark.parametrize("name", PACKS)
def test_show_more_without_a_session_searches_from_state(name):
    """Rândul nou (kernel.v1.1): fără sesiune și fără schimbări, «mai arată-mi» e o căutare din
    stare; fără subiect, `reply_only`."""
    with_subject = _only(_plan(name, _interp(acts=[{"kind": "show_more"}]), _state(_shelf(name))))
    assert with_subject.executor == "search" and with_subject.search_args.query == _label(name)
    planned = _plan(name, _interp(acts=[{"kind": "show_more"}]), _state())
    assert _only(planned).executor == "reply_only" and planned.gaps == ("no_subject",)


@pytest.mark.parametrize("name", PACKS)
def test_compare_two_exact_targets_and_one_exact_target(name):
    a, b = _ids(name)[:2]
    two = _interp(
        acts=[{"kind": "compare", "targets": ["r1", "r2"]}],
        references=[
            {"id": "r1", "text": "primul", "kind": "ordinal", "ordinal": 1},
            {"id": "r2", "text": "al doilea", "kind": "ordinal", "ordinal": 2},
        ],
    )
    plan = _only(_plan(name, two, resolved=(_ref("r1", "exact", [a]), _ref("r2", "exact", [b]))))
    assert (plan.executor, plan.product_ids) == ("compare", [a, b])
    one = _interp(
        acts=[{"kind": "compare", "targets": ["r1"]}],
        references=[{"id": "r1", "text": "primul", "kind": "ordinal", "ordinal": 1}],
    )
    plan = _only(_plan(name, one, resolved=(_ref("r1", "exact", [a]),)))
    assert (plan.executor, plan.product_ids) == ("compare", [a])


@pytest.mark.parametrize("name", PACKS)
@pytest.mark.parametrize("kind", ["detail", "link"])
def test_detail_and_link_on_an_exact_target(name, kind):
    a = _ids(name)[1]
    interp = _interp(
        acts=[{"kind": kind, "targets": ["r1"]}],
        references=[{"id": "r1", "text": "al doilea", "kind": "ordinal", "ordinal": 2}],
    )
    plan = _only(_plan(name, interp, resolved=(_ref("r1", "exact", [a]),)))
    assert (plan.executor, plan.product_ids) == (kind, [a])


@pytest.mark.parametrize("name", PACKS)
@pytest.mark.parametrize("kind", ["link", "detail", "compare"])
def test_a_name_not_found_becomes_a_search_on_the_name(name, kind):
    """`link` (contractul) și `detail`/`compare` (rândul nou, kernel.v1.1) pe un nume negăsit:
    căutarea e plasa, cu `product_name`, iar dezvăluirea `not_exact_match` e pe planul lui."""
    interp = _interp(
        acts=[{"kind": kind, "targets": ["r1"]}],
        references=[{"id": "r1", "text": "Nokia 800", "kind": "name", "name": "Nokia 800"}],
    )
    resolved = (_ref("r1", "not_found", [], kind="name", reason="name_not_found"),)
    planned = _plan(name, interp, _state(_shelf(name)), resolved=resolved)
    plan = _only(planned)
    assert plan.executor == "search"
    assert (plan.search_args.query, plan.search_args.product_name) == ("Nokia 800", "Nokia 800")
    assert plan.product_ids == []
    assert planned.disclosures == ((0, "not_exact_match"),)


@pytest.mark.parametrize("name", PACKS)
def test_a_stale_target_searches_by_name_or_by_subject(name):
    """O țintă dispărută din catalog (`stale`, `not_in_catalog`): pe nume dacă referința îl are,
    altfel pe subiect; fără nimic, `reply_only`. Mereu cu `not_exact_match`."""
    stale = (_ref("r1", "stale", [], kind="ordinal", reason="not_in_catalog"),)
    ordinal = _interp(
        acts=[{"kind": "detail", "targets": ["r1"]}],
        references=[{"id": "r1", "text": "primul", "kind": "ordinal", "ordinal": 1}],
    )
    planned = _plan(name, ordinal, _state(_shelf(name)), resolved=stale)
    plan = _only(planned)
    assert plan.executor == "search" and plan.search_args.query == _label(name)
    assert plan.search_args.product_name is None
    assert planned.disclosures == ((0, "not_exact_match"),)
    bare = _plan(name, ordinal, _state(), resolved=stale)
    assert _only(bare).executor == "reply_only"
    assert bare.disclosures == ((0, "not_exact_match"),)


@pytest.mark.parametrize("name", PACKS)
def test_cart_on_an_exact_target(name):
    a = _ids(name)[0]
    interp = _interp(
        acts=[{"kind": "cart", "targets": ["r1"]}],
        references=[{"id": "r1", "text": "primul", "kind": "ordinal", "ordinal": 1}],
    )
    plan = _only(_plan(name, interp, resolved=(_ref("r1", "exact", [a]),)))
    assert (plan.executor, plan.product_ids) == ("cart", [a])


@pytest.mark.parametrize("name", PACKS)
def test_bundle_goes_to_the_packs_executor_or_to_a_search(name):
    """`bundle` e dat de pachet: SOLE `{"*": "routine_plan"}`, mobila doar pe canapele, restul
    fără intrare (ecommerce.json) ⇒ căutare. Fără subiect nu există bundle."""
    shelf = _shelf(name)
    interp = _interp(acts=[{"kind": "bundle"}])
    plan = _only(_plan(name, interp, _state(shelf)))
    declared = {"sole-ro": "routine_plan", "furniture": "related_products"}
    loaded = fc.pack(name)
    assert tp.bundle_executor(_state(shelf), pack=loaded, vocab=fc.vocabulary(name)) == (
        declared.get(name)
    )
    assert plan.executor == ("bundle" if name in declared else "search")
    assert _only(_plan(name, interp, _state())).executor != "bundle"


def test_the_furniture_bundle_is_only_for_its_shelf():
    plan = _only(_plan("furniture", _interp(acts=[{"kind": "bundle"}]), _state("mese")))
    assert plan.executor == "search"


# --- D3 (`kernel.v2.1`): familia rutinei a planului `bundle` -------------------------------------


def _sole_with_family(shelf: str, family: str = "fata") -> Any:
    pack = fc.pack("sole-ro")
    spec = dataclasses.replace(pack.routine_steps, family_by_shelf={shelf: family})
    return dataclasses.replace(pack, routine_steps=spec)


def _bundle_turn(changes=()):
    shelf = _shelf("sole-ro")
    pid = _ids("sole-ro")[0]
    needs = (_need("skin_type", "dry", "hard"), _need("budget_max", 200.0, "hard"))
    interp = _interp(
        acts=[{"kind": "bundle", "targets": ["r1"]}],
        references=[{"id": "r1", "text": "asta", "kind": "deictic"}],
        changes=list(changes),
    )
    planned = _plan(
        "sole-ro",
        interp,
        _state(shelf, needs=needs),
        resolved=(_ref("r1", "exact", [pid]),),
        loaded=_sole_with_family(shelf),
    )
    return planned, pid


def test_a_bundle_with_a_declared_family_carries_it_and_the_state_args():
    """Familia din pachet pe raftul subiectului; argumentele din STARE, ca la căutare (I2);
    ancora = ținta `exact`. Bugetul intră doar ca SUMĂ spusă în turul rutinei."""
    price = {"op": "set", "dimension": "price", "relation": "lte", "number": 200.0, "quote": "200"}
    planned, pid = _bundle_turn([price])
    plan = _only(planned)
    assert (plan.executor, plan.family, plan.product_ids) == ("bundle", "fata", [pid])
    assert plan.search_args is not None and plan.search_args.price_max == 200.0
    # `skin_type` nu poate filtra dur pe SOLE (`enforce_ready: false`): ordonează, ca la căutare
    assert plan.search_args.prefer == {"skin_type": ["dry"]}
    assert "routine_budget" not in planned.gaps


def test_a_conversation_budget_does_not_cap_the_routine_total():
    """Recenzia D3, P1: bugetul CONVERSAȚIEI e al unui produs («o cremă sub 50»); pe o rutină ar
    plafona SUMA pașilor. Fără o sumă spusă în turul rutinei, nu intră, iar golul se numără."""
    planned, _ = _bundle_turn()
    assert _only(planned).search_args.price_max is None
    assert "routine_budget" in planned.gaps


def test_the_subject_type_picks_the_family_before_the_shelf():
    """Recenzia D3: «cremă de față» e rutina de față chiar pe raftul `machiaj-fata` (NX-313)."""
    from types import SimpleNamespace as NS

    pack = NS(
        routine_steps=NS(
            families={"fata": ("hidratare",), "machiaj": ("baza",)},
            by_product_type={"crema de fata": "fata:hidratare"},
            family_by_shelf={"machiaj-fata": "machiaj"},
        )
    )
    state = _state("machiaj-fata", product_type="crema de fata")
    assert tp.routine_family(state, pack=pack, vocab=None) == "fata"
    assert tp.routine_family(_state("machiaj-fata"), pack=pack, vocab=None) == "machiaj"


def test_a_bundle_without_a_declared_family_stays_on_todays_path():
    """Fără legătură raft → familie, planul rămâne `bundle` fără argumente: calea de azi."""
    shelf = _shelf("sole-ro")
    plan = _only(_plan("sole-ro", _interp(acts=[{"kind": "bundle"}]), _state(shelf)))
    assert (plan.executor, plan.family, plan.search_args) == ("bundle", None, None)


def test_routine_family_reads_the_shelf_then_its_root():
    from types import SimpleNamespace as NS

    pack = NS(routine_steps=NS(family_by_shelf={"ten": "fata", "machiaj-fata": "machiaj"}))
    vocab = NS(
        categories=[NS(key="ten-ingrijirea-tenului", path="ten/ingrijirea-tenului")],
        is_empty=lambda: False,
    )
    assert tp.routine_family(_state("ten-ingrijirea-tenului"), pack=pack, vocab=vocab) == "fata"
    assert tp.routine_family(_state("machiaj-fata"), pack=pack, vocab=vocab) == "machiaj"
    assert tp.routine_family(_state("corp-x"), pack=pack, vocab=vocab) is None
    assert tp.routine_family(_state(), pack=pack, vocab=vocab) is None


@pytest.mark.parametrize("name", PACKS)
@pytest.mark.parametrize(
    "kind,executor",
    [
        ("store_info", "faq"),
        ("order_status", "order"),
        ("chitchat", "reply_only"),
        ("other", "delegate"),
    ],
)
def test_acts_with_a_fixed_executor(name, kind, executor):
    plan = _only(_plan(name, _interp(acts=[{"kind": kind}]), _state(_shelf(name))))
    assert (plan.executor, plan.product_ids, plan.search_args) == (executor, [], None)


# --- verdictul porții -----------------------------------------------------------------------------


def test_must_ask_on_a_cart_plans_one_question_and_no_mutation():
    a, b = _ids("electronics")[:2]
    interp = _interp(
        acts=[{"kind": "cart", "targets": ["r1"]}],
        references=[{"id": "r1", "text": "Samsung-ul", "kind": "name", "name": "Samsung"}],
    )
    key = target_question_key([a, b])
    gate = _gate("must_ask", "mutation_not_exact", "La care te referi?", asked_key=key)
    resolved = (_ref("r1", "ambiguous", [a, b], kind="name", reason="name_shared"),)
    planned = _plan("electronics", interp, resolved=resolved, gate=gate)
    plan = _only(planned)
    assert (plan.executor, plan.product_ids) == ("ask", [a, b])
    assert not any(p.executor == "cart" for p in planned.plans)


def test_a_question_asked_on_the_screen_brings_no_ids_into_the_plan():
    """I1: o întrebare pusă pe ecran (cheia nu e a candidaților țintei) nu aduce id-urile ecranului
    în plan; plannerul nu le vede decât prin `resolved`."""
    a = _ids("electronics")[0]
    interp = _interp(
        acts=[{"kind": "cart", "targets": ["r1"]}],
        references=[{"id": "r1", "text": "Nokia", "kind": "name", "name": "Nokia"}],
    )
    gate = _gate("must_ask", "mutation_not_exact", "La care te referi?", asked_key="ref:deadbeef")
    resolved = (_ref("r1", "not_found", [], kind="name", reason="name_not_found"),)
    screen = References(displayed_products=(DisplayedRef(a), DisplayedRef("el-02")))
    state = dataclasses.replace(_state(), references=screen)
    plan = _only(_plan("electronics", interp, state, resolved=resolved, gate=gate))
    assert (plan.executor, plan.product_ids) == ("ask", [])
    assert a not in plan.product_ids


def test_must_ask_without_a_question_is_reply_only_and_counted():
    planned = _plan(
        "fashion",
        _interp(acts=[{"kind": "cart", "targets": ["r1"]}], references=[_ordinal("r1", 1)]),
        resolved=(_ref("r1", "exact", ["fa-01"], reason="unavailable"),),
        gate=_gate("must_ask", "mutation_unavailable"),
    )
    assert _only(planned).executor == "reply_only" and planned.gaps == ("no_question",)


def _ordinal(ref_id: str, n: int) -> dict:
    return {"id": ref_id, "text": f"#{n}", "kind": "ordinal", "ordinal": n}


def test_act_both_reads_about_every_candidate():
    a, b, c = _ids("electronics")[:3]
    interp = _interp(
        acts=[{"kind": "detail", "targets": ["r1"]}],
        references=[{"id": "r1", "text": "Samsung-ul", "kind": "name", "name": "Samsung"}],
    )
    resolved = (_ref("r1", "ambiguous", [a, b, c], kind="name", reason="name_shared"),)
    both = _only(_plan("electronics", interp, resolved=resolved, gate=_gate("act_both", "x")))
    assert (both.executor, both.product_ids) == ("detail", [a, b, c])
    act = _plan("electronics", interp, resolved=resolved, gate=_gate("act", "clear"))
    assert _only(act).executor == "reply_only" and act.disclosures == ((0, "no_target"),)


def test_skipped_acts_are_not_planned_and_are_disclosed():
    interp = _interp(
        acts=[{"kind": "link", "targets": ["r1"]}, {"kind": "find", "query": "rochie"}],
        references=[{"id": "r1", "text": "roșeață", "kind": "attribute", "value": "x"}],
    )
    resolved = (_ref("r1", "not_found", [], kind="attribute", reason="denotes_property"),)
    planned = _plan(
        "fashion", interp, _state("rochii"), resolved=resolved, gate=_gate(skipped=(0,))
    )
    assert [p.executor for p in planned.plans] == ["search"]
    assert planned.disclosures == ((tp.TURN_LEVEL, "invalid_target"),)
    everything = _plan(
        "fashion",
        _interp(
            acts=[{"kind": "link", "targets": ["r1"]}],
            references=[{"id": "r1", "text": "roșeață", "kind": "attribute", "value": "x"}],
        ),
        resolved=resolved,
        gate=_gate(skipped=(0,)),
    )
    assert _only(everything).executor == "reply_only"


@pytest.mark.parametrize("kind", ["detail", "link", "compare", "cart"])
def test_an_act_that_needs_a_target_and_has_none(kind):
    planned = _plan("electronics", _interp(acts=[{"kind": kind}]), _state("telefoane"))
    assert _only(planned).executor == "reply_only"
    assert planned.disclosures == ((0, "no_target"),)


# --- multi-act ------------------------------------------------------------------------------------


def test_c12_cart_then_a_search_that_depends_on_it():
    """§C.12: «adaugă-l în coș și arată-mi o husă pentru el» ⇒ `cart` apoi `search`, iar căutarea
    depinde de coș: schimbarea turului e relativă la ținta lui."""
    planned = _journey_turn("c12-deictic-then-multi-act", 1)
    assert [p.executor for p in planned.plans] == ["cart", "search"]
    assert planned.plans[0].product_ids == ["el-01"]
    assert planned.plans[1].depends_on == 0 and planned.plans[0].depends_on is None
    assert planned.primary == 1 and planned.dropped_acts == 0


def test_three_acts_keep_two_and_report_the_third():
    planned = _journey_turn("n14-electronics-three-acts-keep-two", 1)
    assert [p.executor for p in planned.plans] == ["cart", "search"]
    assert planned.dropped_acts == 1
    assert planned.disclosures == ((tp.TURN_LEVEL, "dropped_act"),)
    assert planned.plans[planned.primary].executor == "search"


def test_a_mutation_runs_before_a_read_whatever_the_order_the_client_used():
    a, b = _ids("electronics")[:2]
    interp = _interp(
        acts=[
            {"kind": "detail", "targets": ["r2"]},
            {"kind": "cart", "targets": ["r1"]},
        ],
        references=[_ordinal("r1", 1), _ordinal("r2", 2)],
    )
    resolved = (_ref("r1", "exact", [a]), _ref("r2", "exact", [b]))
    planned = _plan("electronics", interp, resolved=resolved)
    assert [p.executor for p in planned.plans] == ["cart", "detail"]
    assert planned.primary == 0  # actul principal e coșul (ultimul act al clientului)
    assert planned.plans[1].depends_on is None  # ținte diferite, nicio schimbare relativă


def test_a_read_on_the_same_target_depends_on_the_mutation():
    a = _ids("electronics")[0]
    interp = _interp(
        acts=[{"kind": "cart", "targets": ["r1"]}, {"kind": "detail", "targets": ["r2"]}],
        references=[_ordinal("r1", 1), {"id": "r2", "text": "el", "kind": "deictic"}],
    )
    resolved = (_ref("r1", "exact", [a]), _ref("r2", "exact", [a], kind="deictic"))
    planned = _plan("electronics", interp, resolved=resolved)
    assert [p.executor for p in planned.plans] == ["cart", "detail"]
    assert planned.plans[1].depends_on == 0


def test_three_acts_prefer_the_mutation_for_the_second_slot():
    a, b = _ids("electronics")[:2]
    interp = _interp(
        acts=[
            {"kind": "detail", "targets": ["r2"]},
            {"kind": "cart", "targets": ["r1"]},
            {"kind": "find", "query": "huse"},
        ],
        references=[_ordinal("r1", 1), _ordinal("r2", 2)],
    )
    resolved = (_ref("r1", "exact", [a]), _ref("r2", "exact", [b]))
    planned = _plan("electronics", interp, _state("telefoane"), resolved=resolved)
    assert [p.executor for p in planned.plans] == ["cart", "search"]
    assert planned.dropped_acts == 1


# --- `SearchArgs` derivat și golurile (§2, §3) ----------------------------------------------------


def _hard_pack(name: str, *keys: str):
    """Pachetul `name`, cu fațetele `keys` declarate `enforce_ready` (auditul de precizie trecut).
    Datele de azi n-au niciuna, deci drumul dur se exersează pe o copie."""
    loaded = fc.pack(name)
    facets = tuple(
        dataclasses.replace(f, enforce_ready=True) if f.key in keys else f for f in loaded.facets
    )
    return dataclasses.replace(loaded, facets=facets)


def _search(planned: PlannedTurn):
    plan = planned.plans[planned.primary]
    assert plan.executor == "search", planned
    return plan.search_args


def test_c2_electronics_units_price_is_hard_and_storage_is_a_gap():
    """§C.2 prin lanțul real: «un telefon cu minim 256 GB, sub 3000 lei» ⇒ raftul și plafonul din
    stare, iar pragul pe `storage` în `gaps`: `SearchArgs` n-are câmp pentru el."""
    planned = _journey_turn("c02-electronics-units", 0)
    args = _search(planned)
    assert (args.category, args.price_max, args.query) == ("telefoane", 3000.0, "telefon")
    assert planned.gaps == ("numeric_facet",)


def test_a_budget_min_is_a_gap_and_never_reaches_search_args():
    needs = (_need("budget_min", 1000.0, "hard"),)
    planned = _plan(
        "electronics", _interp(acts=[{"kind": "find"}]), _state("telefoane", needs=needs)
    )
    args = _search(planned)
    assert args.price_max is None and planned.gaps == ("price_min",)


def test_a_soft_budget_never_becomes_price_max_through_the_real_chain():
    """Un buget pe care clientul nu l-a spus ca număr («nu prea scump», iar modelul propune 2000):
    citatul n-are numărul, deci proveniența îl face `implicit` ⇒ `soft` ⇒ nu intră în `WHERE`
    (I7), ci în `gaps`. Lanțul real (validator → delta → reducer → planner).

    Corectura premisei din card: «cam 2000 de lei» NU iese `soft`. Un preț gol pe `lte` (și `eq`,
    scris ca plafon de NX-334) e `explicit` după regula contractului („a bare price number for
    `lte`"), deci e un plafon dur, iar planul îl pune în `price_max` (testul de mai jos)."""
    step = fc.kernel_step(
        "electronics",
        ConversationStateV2(),
        _interp(
            acts=[{"kind": "find", "query": "telefon"}],
            changes=[
                {
                    "op": "set",
                    "dimension": "category",
                    "relation": "eq",
                    "value": "telefoane",
                    "quote": "telefon",
                },
                {
                    "op": "set",
                    "dimension": "price",
                    "relation": "lte",
                    "number": 2000,
                    "unit": "lei",
                    "quote": "nu prea scump",
                },
            ],
        ),
        "Vreau un telefon nu prea scump.",
    )
    budget = step.gate_state.need_for("budget_max")
    assert budget is not None and budget.strength == "soft"
    assert budget.source == "user_implicit"
    args = _search(step.planned)
    assert args.price_max is None
    assert step.planned.gaps == ("soft_budget",)


def test_a_bare_price_with_eq_is_a_hard_ceiling():
    """«cam 2000 de lei»: `eq` pe preț, număr gol în citat ⇒ `explicit`, iar NX-334 scrie doar
    `budget_max`. Planul: `price_max`, fără `price_min` în `gaps`."""
    step = fc.kernel_step(
        "electronics",
        ConversationStateV2(),
        _interp(
            acts=[{"kind": "find", "query": "telefon"}],
            changes=[
                {
                    "op": "set",
                    "dimension": "category",
                    "relation": "eq",
                    "value": "telefoane",
                    "quote": "telefon",
                },
                {
                    "op": "set",
                    "dimension": "price",
                    "relation": "eq",
                    "number": 2000,
                    "unit": "lei",
                    "quote": "2000 de lei",
                },
            ],
        ),
        "Vreau un telefon de cam 2000 de lei.",
    )
    assert step.gate_state.need_for("budget_min") is None
    assert _search(step.planned).price_max == 2000.0
    assert step.planned.gaps == ()


def test_an_exclusion_is_a_gap():
    step = fc.kernel_step(
        "fashion",
        ConversationStateV2(),
        _interp(
            acts=[{"kind": "find", "query": "rochie"}],
            changes=[
                {
                    "op": "set",
                    "dimension": "category",
                    "relation": "eq",
                    "value": "rochii",
                    "quote": "rochie",
                },
                {
                    "op": "add",
                    "dimension": "material",
                    "relation": "avoid",
                    "value": "poliester",
                    "quote": "să nu fie din poliester",
                },
            ],
        ),
        "Vreau o rochie, să nu fie din poliester.",
    )
    assert "exclusion" in step.planned.gaps
    args = _search(step.planned)
    assert "poliester" not in str(args.model_dump())


def test_a_need_without_a_field_or_a_facet_is_a_gap():
    needs = (_need("use_case", "birou"),)
    planned = _plan(
        "electronics", _interp(acts=[{"kind": "find"}]), _state("telefoane", needs=needs)
    )
    _search(planned)
    assert planned.gaps == ("unsupported_need",)


def test_a_variant_asked_and_not_found_is_a_gap():
    interp = _interp(
        acts=[{"kind": "find", "query": "rochie"}],
        references=[
            {
                "id": "r1",
                "text": "în verde",
                "kind": "attribute",
                "dimension": "variant",
                "value": "verde",
            }
        ],
    )
    resolved = (_ref("r1", "not_found", [], kind="attribute", reason="attribute_not_in_sets"),)
    planned = _plan("fashion", interp, _state("rochii"), resolved=resolved)
    assert _search(planned).variant_label is None
    assert planned.gaps == ("variant",)


@pytest.mark.parametrize("name", PACKS)
def test_every_gap_code_is_in_the_closed_vocabulary(name):
    needs = (
        _need("budget_max", 100.0, "soft"),
        _need("budget_min", 10.0, "hard"),
        _need("restriction", "x", "hard"),
        _need("use_case", "y"),
    )
    planned = _plan(name, _interp(acts=[{"kind": "find"}]), _state(_shelf(name), needs=needs))
    assert set(planned.gaps) <= set(GAPS)
    assert {"soft_budget", "price_min", "exclusion", "unsupported_need"} <= set(planned.gaps)


def test_soft_facet_needs_go_to_prefer_with_the_catalog_key():
    """Pe datele de azi nicio fațetă nu e `enforce_ready`, deci o nevoie de fațetă e `soft` ⇒
    `prefer`. Valoarea din stare e normalizată («samsung»); fuziunea compară cu atributul literal,
    deci plannerul o duce înapoi la cheia din catalog («Samsung»)."""
    needs = (_need("brand", "samsung"), _need("color", "negru"))
    args = _search(
        _plan("electronics", _interp(acts=[{"kind": "find"}]), _state("telefoane", needs=needs))
    )
    assert args.brand is None and args.concerns is None
    assert args.prefer == {"brand": ["Samsung"], "color": ["negru"]}


def test_hard_facet_needs_go_to_concerns_features_and_brand():
    """Drumul dur, pe o copie cu fațetele auditate: `concerns` (fațetă obișnuită), `features`
    (fațetă din `searchable_facets`), `brand`. O nevoie dură dintr-o sursă care nu poate susține
    un filtru rămâne preferință (I7)."""
    loaded = dataclasses.replace(
        _hard_pack("electronics", "color", "brand"), searchable_facets=("color",)
    )
    needs = (
        _need("brand", "samsung", "hard"),
        _need("color", "negru", "hard"),
    )
    args = _search(
        _plan(
            "electronics",
            _interp(acts=[{"kind": "find"}]),
            _state("telefoane", needs=needs),
            loaded=loaded,
        )
    )
    assert (args.brand, args.features, args.concerns) == ("samsung", ["negru"], None)
    loaded = _hard_pack("fashion", "material")
    needs = (_need("material", "bumbac", "hard"), _need("color", "negru", "hard", "model_inferred"))
    args = _search(
        _plan(
            "fashion",
            _interp(acts=[{"kind": "find"}]),
            _state("rochii", needs=needs),
            loaded=loaded,
        )
    )
    assert args.concerns == ["bumbac"]
    assert args.prefer == {"color": ["negru"]}


def test_a_value_shared_by_two_facets_resolves_ambiguously_in_the_tool():
    """Riscul declarat în card (§2): codul canonic își pierde dimensiunea, iar unealta îl
    re-rezolvă peste TOATE fațetele. Măsurat: o valoare purtată de două fațete iese pe una
    dintre ele după dovadă, nu pe cea din stare. Pe datele de azi drumul nu rulează (nicio fațetă
    `enforce_ready`), deci planul pune astfel de nevoi în `prefer`, unde cheia e a fațetei."""
    vocab = CatalogVocabulary(
        business_id="b",
        dimensions={
            "color": (VocabEntry(key="natur", label="natur", count=2),),
            "material": (VocabEntry(key="natur", label="natur", count=9),),
        },
    )
    res = resolve_any(vocab, "natur")
    assert res.status is ResolutionStatus.KNOWN and res.dimension == "material"
    needs = (_need("color", "natur"),)
    args = _search(
        _plan("furniture", _interp(acts=[{"kind": "find"}]), _state("canapele", needs=needs))
    )
    assert args.prefer == {"color": ["natur"]} and args.concerns is None


def test_unmapped_needs_and_signals_become_rank_terms_capped():
    needs = tuple(_need("unmapped", f"u{i}") for i in range(3))
    ranking = (RankingSignal("unmapped", "gaming", "contains"),)
    args = _search(
        _plan(
            "electronics",
            _interp(acts=[{"kind": "find"}]),
            _state("telefoane", needs=needs),
            ranking=ranking,
        )
    )
    assert args.rank_terms == ["u0", "u1", "u2"]
    assert len(args.rank_terms) <= MAX_UNMAPPED_PER_TOPIC
    args = _search(
        _plan("electronics", _interp(acts=[{"kind": "find"}]), _state("telefoane"), ranking=ranking)
    )
    assert args.rank_terms == ["gaming"]


def test_ranking_signals_on_facets_prefer_and_on_price_are_a_gap():
    ranking = (
        RankingSignal("color", "negru", "eq"),
        RankingSignal("price", 100.0, "lte"),
        RankingSignal("storage", 256.0, "gte"),
        RankingSignal("color", "alb", "avoid"),
    )
    planned = _plan(
        "electronics", _interp(acts=[{"kind": "find"}]), _state("telefoane"), ranking=ranking
    )
    args = _search(planned)
    assert args.prefer == {"color": ["negru"]} and args.price_max is None
    assert planned.gaps == ("soft_budget", "numeric_facet", "exclusion")


@pytest.mark.parametrize(
    "dimension,direction,mode",
    [
        ("price", "min", "price_asc"),
        ("price", "max", "price_desc"),
        (None, "min", "price_asc"),
        ("rating", "max", "rating_desc"),
        ("width", "max", "relevance"),
    ],
)
def test_an_extreme_reference_sorts_the_search(dimension, direction, mode):
    interp = _interp(
        acts=[{"kind": "find", "query": "ceva"}],
        references=[
            {
                "id": "r1",
                "text": "cel mai",
                "kind": "extreme",
                "dimension": dimension,
                "direction": direction,
            }
        ],
    )
    assert _search(_plan("furniture", interp, _state("canapele"))).sort_mode == mode


def test_the_subject_label_follows_a_regional_locale_and_the_product_type():
    """Locale-ul regional („ro-RO") cade pe limbă (recenzia NX-332); un subiect fără raft (doar
    tipul) ia eticheta tipului din pachet, altfel cheia."""
    loaded = fc.pack("sole-ro")
    state = _state(product_type="crema")
    plan = _only(_plan("sole-ro", _interp(acts=[{"kind": "find"}]), state, locale="ro-RO"))
    label = loaded.value_label("product_type", "crema", "ro") or "crema"
    assert plan.search_args.query == label and plan.search_args.category is None
    region = _only(
        _plan("electronics", _interp(acts=[{"kind": "find"}]), _state("telefoane"), locale="ro-RO")
    )
    assert region.search_args.query == "Telefoane"


def test_without_a_pack_or_a_vocabulary_the_planner_still_plans():
    """Vocabular `None`, pachet `None`: niciun crash, subiectul e cheia, nevoile de fațetă n-au
    unde merge (nicio fațetă), bugetul dur rămâne plafon."""
    needs = (_need("budget_max", 100.0, "hard"), _need("color", "negru"))
    planned = plan_turn(
        _interp(acts=[{"kind": "find"}, {"kind": "bundle"}]),
        _state("telefoane", needs=needs),
        (),
        (),
        _gate(),
        changed=False,
        pack=None,
        vocab=None,
        locale="ro",
    )
    args = planned.plans[0].search_args
    assert (args.query, args.price_max) == ("telefoane", 100.0)
    assert planned.plans[1].executor == "search"
    assert planned.gaps == ("unsupported_need",)


def test_a_replace_or_supersede_leaves_one_active_budget_for_the_planner():
    """Drumul „supersede" (NX-334, recenzia): un buget nou explicit înlocuiește plafonul, iar
    plannerul citește doar nevoia ACTIVĂ."""
    first = fc.kernel_step(
        "electronics",
        ConversationStateV2(),
        _interp(
            acts=[{"kind": "find", "query": "telefon"}],
            changes=[
                {
                    "op": "set",
                    "dimension": "category",
                    "relation": "eq",
                    "value": "telefoane",
                    "quote": "telefon",
                },
                {
                    "op": "set",
                    "dimension": "price",
                    "relation": "lte",
                    "number": 3000,
                    "unit": "lei",
                    "quote": "sub 3000 lei",
                },
            ],
        ),
        "Vreau un telefon sub 3000 lei.",
        turn_id="t0",
    )
    assert _search(first.planned).price_max == 3000.0
    second = fc.kernel_step(
        "electronics",
        first.state_after,
        _interp(
            acts=[{"kind": "find"}],
            changes=[
                {
                    "op": "replace",
                    "target": "c1",
                    "dimension": "price",
                    "relation": "lte",
                    "number": 2000,
                    "unit": "lei",
                    "quote": "sub 2000 lei",
                },
            ],
        ),
        "De fapt sub 2000 lei.",
        earlier=("Vreau un telefon sub 3000 lei.",),
        turn_id="t1",
    )
    assert _search(second.planned).price_max == 2000.0


# --- proprietăți: I1 și I7, pe cinci pachete ------------------------------------------------------

_PROPERTY = settings(
    max_examples=120,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
#: Sonda unei mutații trebuie s-o prindă MEREU, nu cu noroc: derandomizată, fără baza de exemple.
_PROBE = settings(
    max_examples=300,
    deadline=None,
    derandomize=True,
    database=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
_ACT_KINDS = (
    "find",
    "show_more",
    "compare",
    "detail",
    "link",
    "cart",
    "bundle",
    "order_status",
    "store_info",
    "chitchat",
    "other",
)
_OUTCOMES = ("exact", "ambiguous", "not_found", "stale")
_VERDICTS = ("act", "resolve_from_context", "act_both", "must_ask")
_SOURCES = ("user_explicit", "user_implicit", "model_inferred", "catalog", "policy", "action")
_LOCALES = ("ro", "ro-RO", "", "en")
_VALUES = ("negru", "Samsung", "dry", "gaming", "bumbac", "ea")


def _audited(name: str):
    """Pachetul `name` cu TOATE fațetele de atribut `enforce_ready` și prima dintre ele în
    `searchable_facets`: fără varianta asta, pe datele de fixture (nicio fațetă auditată), drumul
    `brand`/`concerns`/`features` nu rulează niciodată și proprietatea I7 n-ar avea ce verifica
    acolo (recenzia NX-333, riscul 5)."""
    loaded = fc.pack(name)
    attrs = [f for f in loaded.facets if getattr(f.source, "value", None) == "attribute"]
    facets = tuple(
        dataclasses.replace(f, enforce_ready=True) if f in attrs else f for f in loaded.facets
    )
    # O fațetă de listă/enum care nu e marca (marca are câmpul ei): drumul `features`.
    listed = [
        f.key
        for f in attrs
        if f.key != "brand" and f.key not in NeedVocabulary.from_pack(loaded).bounds
    ]
    searchable = (listed[-1],) if listed else ()
    return dataclasses.replace(loaded, facets=facets, searchable_facets=searchable)


def _need_keys(pack) -> list[str]:
    """Cheile de nevoi pe care le poate scrie reducerul pe pachetul `pack`: fațetele, limitele
    numerice NX-334 (`<fațetă>_min`/`_max`), cheile universale (buget, excludere, `unmapped`)."""
    vocab = NeedVocabulary.from_pack(pack)
    keys = [f.key for f in getattr(pack, "facets", ()) or () if f.key not in ("price", "category")]
    keys += [k for pair in vocab.bounds.values() for k in pair if k]
    keys += ["brand", "unmapped", "budget_min", "restriction", "use_case"]
    # Marca de două ori: e singurul drum spre `SearchArgs.brand`, iar pe patru pachete din cinci
    # n-are fațetă, deci fără pondere generatorul ar ajunge rar la ea.
    return [*sorted(set(keys)), "brand"]


@dataclasses.dataclass(frozen=True)
class _Case:
    name: str
    pack: Any
    vocab: Any
    interp: TurnInterpretation
    state: ConversationStateV2
    resolved: tuple[ResolvedRef, ...]
    gate: GateOutcome
    changed: bool
    ranking: tuple[RankingSignal, ...]
    locale: str


@st.composite
def _cases(draw):
    name = draw(st.sampled_from(PACKS))
    pack = draw(st.sampled_from(["fixture", "audited", "audited", "none"]))
    loaded = {"fixture": fc.pack(name), "audited": _audited(name), "none": None}[pack]
    vocab = fc.vocabulary(name) if draw(st.booleans()) else None
    catalog = _ids(name) + [f"x{i}" for i in range(4)]  # x* = pe ecran, dar nerezolvate în tur
    refs = ("r1", "r2", "r3")
    acts = []
    for _ in range(draw(st.integers(0, 3))):
        targets = draw(st.lists(st.sampled_from(refs), max_size=2, unique=True))
        acts.append(
            {
                "kind": draw(st.sampled_from(("find", "find", "find", *_ACT_KINDS))),
                "targets": targets,
                "query": draw(st.sampled_from([None, "ceva", "  "])),
            }
        )
    references = [
        {
            "id": r,
            "text": r,
            "kind": draw(st.sampled_from(["ordinal", "name", "extreme", "attribute"])),
            "name": draw(st.sampled_from([None, "Nume X", " "])),
            "dimension": draw(st.sampled_from([None, "price", "rating", "variant"])),
            "direction": draw(st.sampled_from(["min", "max"])),
        }
        for r in refs
    ]
    resolved = []
    for r in refs:
        if not draw(st.booleans()):
            continue
        outcome = draw(st.sampled_from(_OUTCOMES))
        size = {"exact": 1, "ambiguous": draw(st.integers(2, 4))}.get(outcome, 0)
        ids = draw(
            st.lists(st.sampled_from(catalog[:4]), min_size=size, max_size=size, unique=True)
        )
        resolved.append(
            _ref(
                r,
                outcome,
                ids,
                kind=draw(st.sampled_from(["ordinal", "name", "attribute"])),
                reason=draw(st.sampled_from([None, "not_in_catalog", "name_not_found"])),
            )
        )
    shown = draw(st.lists(st.sampled_from(catalog), max_size=6, unique=True))
    keys = _need_keys(loaded if loaded is not None else fc.pack(name))
    budget = _need(
        "budget_max",
        draw(st.sampled_from([10.0, 250.0, 4999.5, 120.0, -1.0, float("nan")])),
        draw(st.sampled_from(["hard", "soft"])),
        draw(st.sampled_from(_SOURCES)),
    )
    others = tuple(
        _need(
            draw(st.sampled_from(keys)),
            draw(st.sampled_from([*_VALUES, *_VALUES, 256.0, True])),
            draw(st.sampled_from(["hard", "hard", "soft"])),
            draw(st.sampled_from(("user_explicit", "user_explicit", *_SOURCES))),
        )
        for _ in range(draw(st.integers(0, 5)))
    )
    state = ConversationStateV2(
        revision=2,
        topic=Topic(
            category_key=draw(st.sampled_from([None, _shelf(name)])),
            product_type=draw(st.sampled_from([None, "crema"])),
        ),
        needs=(budget, *others) if draw(st.booleans()) else others,
        active_search=draw(st.sampled_from([None, {"fp": "x", "pool": shown, "cursor": 0}])),
    )
    state = dataclasses.replace(
        state,
        references=dataclasses.replace(
            state.references,
            displayed_products=tuple(DisplayedRef(p) for p in shown),
            selected_product=draw(st.sampled_from([None, *shown])) if shown else None,
        ),
    )
    ranking = tuple(
        RankingSignal(
            draw(st.sampled_from([*keys, "price", "unmapped"])),
            draw(st.sampled_from([*_VALUES, 99.0])),
            draw(st.sampled_from(["eq", "contains", "avoid", "lte", "gte"])),
        )
        for _ in range(draw(st.integers(0, 3)))
    )
    verdict = draw(st.sampled_from(_VERDICTS))
    question = draw(st.sampled_from([None, "?"]))
    asked = None
    if question and resolved:
        pick = draw(st.sampled_from(resolved))
        asked = target_question_key(pick.product_ids) if pick.product_ids else None
    skipped = (
        tuple(sorted(draw(st.sets(st.integers(0, len(acts) - 1), max_size=1)))) if acts else ()
    )
    return _Case(
        name=name,
        pack=loaded,
        vocab=vocab,
        interp=_interp(acts=acts, references=references),
        state=state,
        resolved=tuple(resolved),
        gate=_gate(verdict, "r", question, asked_key=asked, skipped=skipped),
        changed=draw(st.booleans()),
        ranking=ranking,
        locale=draw(st.sampled_from(_LOCALES)),
    )


def _i1_violations(planned: PlannedTurn, resolved: tuple[ResolvedRef, ...]) -> list[str]:
    allowed = {p for r in resolved for p in r.product_ids}
    return [p for plan in planned.plans for p in plan.product_ids if p not in allowed]


def _run(case: _Case) -> PlannedTurn:
    return plan_turn(
        case.interp,
        case.state,
        case.ranking,
        case.resolved,
        case.gate,
        changed=case.changed,
        pack=case.pack,
        vocab=case.vocab,
        locale=case.locale,
    )


@_PROPERTY
@given(_cases())
def test_i1_every_planned_id_comes_from_a_reference_resolved_this_turn(case):
    """I1 pe plan: `TurnPlan.product_ids` ⊆ `ResolvedRef.product_ids` ale turului. Ecranul,
    selecția și sesiunea din stare au id-uri (inclusiv `x*`, nerezolvate), iar plannerul nu le
    poate folosi decât prin resolver. Pe drum: niciun crash (pachet/vocabular absent, locale
    regional sau gol, valori corupte), vocabularele închise, cel mult două planuri."""
    planned = _run(case)
    assert not _i1_violations(planned, case.resolved)
    assert len(planned.plans) <= tp.MAX_PLANS
    assert all(code in tp.DISCLOSURES for _, code in planned.disclosures)
    assert set(planned.gaps) <= set(GAPS)


def test_the_i1_property_fails_on_a_planner_that_reads_the_screen(monkeypatch):
    """Mutația: un planner care completează ținta din ecran (cum ar face „ultimul arătat") pică
    proprietatea. Fără exemplul ăsta, proprietatea ar putea trece pe o construcție care o
    scutește."""
    original = tp._Planner._refs_of

    def leaky(self, act):
        refs = original(self, act)
        screen = [d.product_id for d in self.state.references.displayed_products]
        return [*refs, _ref("leak", "exact", screen[:1])] if screen else refs

    monkeypatch.setattr(tp._Planner, "_refs_of", leaky)
    found = []

    @_PROBE
    @given(_cases())
    def probe(case):
        if _i1_violations(_run(case), case.resolved):
            found.append(case)

    probe()
    assert found, "mutația n-a fost prinsă: proprietatea I1 nu generează cazul relevant"


def _i7_violations(planned: PlannedTurn, case: _Case) -> list[tuple[str, str]]:
    """(câmp, valoare) din `SearchArgs` care ajung în `WHERE` fără o nevoie DURĂ în spate: tărie
    `hard`, sursă hard-capable, dimensiune hard-capable (I8) pe pachetul turului."""
    from src.conversation.provenance import hard_capable

    vocab = NeedVocabulary.from_pack(case.pack)
    hard = {
        n.normalized_value
        for n in case.state.active_needs()
        if n.strength == "hard"
        and n.source in HARD_CAPABLE_SOURCES
        and hard_capable(vocab.dimension_of(n.key), case.pack)
    }
    out = []
    for plan in planned.plans:
        args = plan.search_args
        if args is None:
            continue
        fields = [("price_max", args.price_max), ("brand", args.brand)]
        fields += [("concerns", v) for v in args.concerns or []]
        fields += [("features", v) for v in args.features or []]
        out += [(f, str(v)) for f, v in fields if v is not None and v not in hard]
    return out


@_PROPERTY
@given(_cases())
def test_i7_nothing_but_hard_explicit_evidence_reaches_a_filter(case):
    """I7: `price_max`, `brand`, `concerns`, `features` (tot ce ajunge în `WHERE`) vin DOAR din
    nevoi dure, de la o sursă care poate susține un filtru, pe o dimensiune hard-capable. Un buget
    `soft` sau `user_implicit` n-ajunge niciodată `price_max`, un semnal `inferred` niciodată un
    filtru."""
    assert not _i7_violations(_run(case), case)


_COVERAGE = settings(_PROBE, max_examples=1500)


def test_the_generator_reaches_every_filter_field():
    """Recenzia NX-333 (riscul 5): proprietatea I7 era goală pe `brand`/`concerns`/`features` (zero
    apariții în 2.000 de exemple). Pe generatorul de acum, fiecare câmp de filtru apare."""
    seen: dict[str, int] = {"price_max": 0, "brand": 0, "concerns": 0, "features": 0}

    @_COVERAGE
    @given(_cases())
    def probe(case):
        for plan in _run(case).plans:
            args = plan.search_args
            if args is None:
                continue
            seen["price_max"] += args.price_max is not None
            seen["brand"] += args.brand is not None
            seen["concerns"] += bool(args.concerns)
            seen["features"] += bool(args.features)

    probe()
    assert all(n > 0 for n in seen.values()), seen


def test_the_i7_property_fails_on_a_planner_that_trusts_any_strength(monkeypatch):
    """Mutația: un planner care crede orice tărie pică proprietatea, și pe alte câmpuri decât
    prețul (recenzia: mutația prindea doar bugetul)."""
    monkeypatch.setattr(tp._Planner, "_hard", lambda self, need, dimension: True)
    fields: set[str] = set()

    @_PROBE
    @given(_cases())
    def probe(case):
        fields.update(f for f, _ in _i7_violations(_run(case), case))

    probe()
    assert {"price_max", "brand", "concerns", "features"} <= fields, fields


def test_the_planner_is_deterministic():
    planned = [_journey_turn("c12-deictic-then-multi-act", 1) for _ in range(3)]
    assert planned[0] == planned[1] == planned[2]


# --- pachetul: `bundle_executors` ---------------------------------------------------------------


def _business(pack_doc: dict) -> BusinessConfig:
    return BusinessConfig(
        id="b", slug="b", name="b", vertical="ecommerce", settings={"domain_pack": pack_doc}
    )


def test_the_loader_keeps_known_tools_and_rejects_unknown_ones(caplog):
    with caplog.at_level(logging.WARNING):
        loaded = load_domain_pack(
            _business({"bundle_executors": {"*": "routine_plan", "ten": "nu_exista", "x": 3}})
        )
    assert loaded.bundle_executors == {"*": "routine_plan"}
    assert "nu_exista" in caplog.text
    # raftul cu unealta respinsă cade pe `*`; fără `*`, `bundle` devine căutare
    assert tp.bundle_executor(_state("ten"), pack=loaded, vocab=None) == "routine_plan"
    only_bad = load_domain_pack(_business({"bundle_executors": {"*": "nu_exista"}}))
    assert only_bad.bundle_executors == {}
    plan = plan_turn(
        _interp(acts=[{"kind": "bundle"}]),
        _state("ten"),
        (),
        (),
        _gate(),
        changed=False,
        pack=only_bad,
        vocab=None,
        locale="ro",
    )
    assert _only(plan).executor == "search"


def test_sole_declares_routine_plan_and_the_ecommerce_defaults_declare_nothing():
    assert fc.pack("sole-ro").bundle_executors == {"*": "routine_plan"}
    assert load_domain_pack(_business({})).bundle_executors == {}


def test_the_bundle_key_can_be_the_shelf_root():
    """Pe un catalog cu arbore (SOLE), cheia e RĂDĂCINA raftului (`topic_root_of`)."""
    vocab = CatalogVocabulary(
        business_id="b",
        dimensions={
            CATEGORY_DIMENSION: (
                VocabEntry(key="ten", label="Ten", count=9, path="ten"),
                VocabEntry(key="ten-creme", label="Creme", count=4, path="ten/ten-creme"),
            )
        },
    )
    loaded = dataclasses.replace(fc.pack("electronics"), bundle_executors={"ten": "routine_plan"})
    assert tp.bundle_executor(_state("ten-creme"), pack=loaded, vocab=vocab) == "routine_plan"
    assert tp.bundle_executor(_state("altceva"), pack=loaded, vocab=vocab) is None


# --- calea planificată a uneltei ------------------------------------------------------------------


def _tool_ctx(body: str, state_active_search: dict | None = None) -> TurnContext:
    ctx = TurnContext(
        turn_id="t",
        business=BusinessConfig(id="biz-1", slug="s", name="n"),
        contact=Contact(id="c", business_id="biz-1"),
        message=InboundMessage(provider_msg_id="m", body=body),
        conversation_id="conv",
    )
    ctx.state.active_search = state_active_search
    return ctx


class _SqlConn:
    """Înregistrează SQL-ul REAL generat de `search_products_lexical` (fără DB)."""

    def __init__(self) -> None:
        self.statements: list[tuple[str, tuple]] = []

    async def fetch(self, sql: str, *params):
        self.statements.append((sql, params))
        return []


@pytest.fixture
def sql_conn(monkeypatch):
    conn = _SqlConn()

    async def empty_vocab(deps, business_id):
        return CatalogVocabulary(
            business_id=business_id,
            dimensions={
                CATEGORY_DIMENSION: (
                    VocabEntry(key="telefoane", label="Telefoane", count=10),
                    VocabEntry(key="tablete", label="Tablete", count=7),
                )
            },
        )

    monkeypatch.setattr(ct, "get_vocabulary", empty_vocab)
    return conn


def _run_tool(conn, coro_fn, *args):
    deps = PipelineDeps(conn=conn, redis=None, llm=None)
    return asyncio.run(coro_fn(*args[:1], deps, *args[1:]))


def _typed_constraints_on(monkeypatch):
    """`TYPED_CONSTRAINTS_ENABLED` aprins, cu un registru de unități pe `storage` (NX-266)."""
    from src.config import get_settings

    monkeypatch.setenv("TYPED_CONSTRAINTS_ENABLED", "true")
    get_settings.cache_clear()
    return fc.pack("electronics")


def test_the_planned_path_has_no_session_inheritance_and_no_raw_text_constraints(
    sql_conn, monkeypatch
):
    """Cardul, §2: aceeași `SearchArgs`, cu `active_search` plin (raftul altei căutări) și un mesaj
    «minim 256 GB» cu `TYPED_CONSTRAINTS_ENABLED` aprins. Calea planificată: SQL fără raftul
    sesiunii și fără clauza de constrângere. Calea de azi: amândouă (controlul)."""
    from src.config import get_settings

    loaded = _typed_constraints_on(monkeypatch)
    try:
        session = {"filters": {"category": "tablete"}, "fp": "old", "pool": [], "cursor": 0}
        args = ct.SearchArgs(query="telefon")

        planned_ctx = _tool_ctx("un telefon cu minim 256 GB", session)
        planned_ctx.business = dataclasses.replace(planned_ctx.business, domain_pack=loaded)
        _run_tool(sql_conn, ct.run_planned_search, planned_ctx, args)
        planned_sql = " ".join(s for s, _ in sql_conn.statements)
        planned_params = [p for _, ps in sql_conn.statements for p in ps]
        assert not _events(planned_ctx, "search_filter_inherited")
        assert "tablete" not in str(planned_params)
        assert "jsonb_typeof" not in planned_sql and "storage" not in planned_params

        sql_conn.statements.clear()
        today_ctx = _tool_ctx("un telefon cu minim 256 GB", session)
        today_ctx.business = dataclasses.replace(today_ctx.business, domain_pack=loaded)
        _run_tool(sql_conn, ct.search_products_tool, today_ctx, {"query": "telefon"})
        today_sql = " ".join(s for s, _ in sql_conn.statements)
        today_params = [p for _, ps in sql_conn.statements for p in ps]
        assert _events(today_ctx, "search_filter_inherited")
        assert "tablete" in str(today_params)
        assert "jsonb_typeof" in today_sql and "storage" in today_params, (
            "controlul: calea de azi citește pragul din mesajul brut"
        )
    finally:
        get_settings.cache_clear()


def test_after_a_clear_topic_the_planned_search_has_no_shelf(sql_conn):
    """Cardul, Edge Cases: `active_search.filters.category` plin, `topic.category_key` gol (după un
    `clear topic`) ⇒ SQL fără categorie."""
    planned = _plan("electronics", _interp(acts=[{"kind": "find", "query": "telefon"}]), _state())
    args = _search(planned)
    assert args.category is None
    ctx = _tool_ctx("vreau un telefon", {"filters": {"category": "telefoane"}, "fp": "x"})
    _run_tool(sql_conn, ct.run_planned_search, ctx, args)
    params = [p for _, ps in sql_conn.statements for p in ps]
    assert "telefoane" not in str(params)


def _events(ctx, kind):
    return [e for e in ctx.events if e.type == kind]


def test_the_price_guard_keeps_the_planners_hard_budget():
    """NX-319 rămâne apărare în adâncime: pe un buget `hard` (rostit, cu comparator) garda trece
    prin construcție, deci nu scoate nimic din ce a pus plannerul (§C.2)."""
    args = _search(_journey_turn("c02-electronics-units", 0))
    ctx = _tool_ctx("Vreau un telefon cu minim 256 GB, sub 3000 lei.")
    texts = ct.client_texts(ctx)
    source, _ = ct.price_bound_verdict(
        ctx,
        args.price_max,
        texts=texts,
        relative_request=ct._is_relative_price_request(texts[0]),
        session_price_max=None,
    )
    assert source is not None


def test_a_refinement_with_rank_terms_opens_a_new_session(monkeypatch):
    """«și bun pentru gaming» pe o sesiune activă: amprenta se schimbă, deci sesiune NOUĂ, nu
    pagina veche (NX-119/NX-223)."""
    calls: list[dict] = []

    async def fake_lex(conn, business_id, **kwargs):
        calls.append(kwargs)
        return [{"id": f"p{i}", "name": f"P{i}", "price": 10.0 * i} for i in range(1, 9)]

    async def no_vocab(deps, business_id):
        return CatalogVocabulary(business_id=business_id, dimensions={})

    monkeypatch.setattr(ct, "search_products_lexical", fake_lex)
    monkeypatch.setattr(ct, "get_vocabulary", no_vocab)
    first = _tool_ctx("vreau un telefon")
    _run_tool(_SqlConn(), ct.run_planned_search, first, ct.SearchArgs(query="telefon"))
    session = first.state_patch["active_search"]
    calls.clear()
    second = _tool_ctx("și bun pentru gaming", session)
    refined = ct.SearchArgs(query="telefon", rank_terms=["gaming"])
    _run_tool(_SqlConn(), ct.run_planned_search, second, refined)
    assert calls, "a paginat pool-ul vechi în loc să caute din nou"
    assert second.state_patch["active_search"]["fp"] != session["fp"]
    same = _tool_ctx("mai arată-mi", session)
    calls.clear()
    _run_tool(_SqlConn(), ct.run_planned_search, same, ct.SearchArgs(query="telefon"))
    assert not calls, "aceeași cerere pe aceeași sesiune paginează"


# --- §4: ponderea lui `rank_terms` sub o potrivire de fațetă --------------------------------------


def _pool(n: int, *, low_pref: int, top: int) -> list[dict]:
    """`n` candidați în ordinea lexicală; `top` e cel urcat de `rank_terms`, `low_pref` e cel de
    DEDESUBT, cu potrivirea pe fațeta preferată. Restul sunt identici (rating, stoc, preț), ca
    singurele diferențe să fie poziția și preferința."""
    out = []
    for i in range(n):
        attrs = {"color": "negru" if i == low_pref else "alb"}
        out.append(
            {
                "id": f"p{i:02d}",
                "price": 100.0,
                "rating": 4.5,
                "review_count": 10,
                "availability": "in_stock",
                "attributes": attrs,
            }
        )
    return out


def test_a_facet_match_beats_the_adjacent_rank_term_position_with_todays_weights():
    """Decizia din §4, măsurată: doi candidați ADIACENȚI în ordinea lexicală doar prin
    `rank_terms` (cel de sus are termenul, cel de jos potrivirea pe fațeta preferată). Pe
    ponderile de azi (`blended_rerank`, `need_preference` 0,25) și pe pool-ul real de fuziune
    (`_FUSION_POOL`), fuziunea îl pune sus pe al doilea, oriunde în pool. Deci `rank_terms` rămâne
    cheie SECUNDARĂ în `ORDER BY`, iar I25 („sub orice potrivire de fațetă") ține pe adiacență."""
    n = ct._FUSION_POOL
    for top in range(n - 1):
        pool = _pool(n, low_pref=top + 1, top=top)
        ranked = fuse_candidates(
            pool, [], sort_mode="relevance", weights={}, prefer={"color": ["negru"]}
        )
        order = [p["id"] for p in ranked]
        assert order.index(f"p{top + 1:02d}") < order.index(f"p{top:02d}"), top
    assert RANK_WEIGHTS["need_preference"] == 0.25


def test_the_limit_of_the_rank_term_weight_is_measured_not_assumed():
    """Limita, declarată în card: `rank_terms` e cheie secundară, deci mută un produs doar în
    grupul de egalitate al rangului de text. Când grupul e MARE (treapta `filters_only`, unde sute
    de produse au rang de text zero), termenul poate urca un produs peste mai multe poziții deodată,
    iar peste un salt destul de mare preferința de fațetă nu-l mai întoarce. Măsurat pe ponderile de
    azi, pe pool-ul real: preferința câștigă până la saltul de mai jos, apoi pierde."""
    n = ct._FUSION_POOL

    def pref_wins(jump: int) -> bool:
        pool = _pool(n, low_pref=jump, top=0)
        ranked = fuse_candidates(
            pool, [], sort_mode="relevance", weights={}, prefer={"color": ["negru"]}
        )
        order = [p["id"] for p in ranked]
        return order.index(f"p{jump:02d}") < order.index("p00")

    max_jump = max(j for j in range(1, n) if pref_wins(j))
    assert all(pref_wins(j) for j in range(1, max_jump + 1))
    assert max_jump == 7
    assert not pref_wins(max_jump + 1)


# --- snapshot-urile golden (§5) -------------------------------------------------------------------


def test_the_plan_snapshots_match_the_planner_on_five_packs():
    """`scripts/kernel_plan_snapshot.py`: planurile complete ale fiecărui tur din corpus, pe cele
    cinci pachete. Un diff pică până la regenerarea conștientă (`--write`)."""
    from scripts import kernel_plan_snapshot as snap

    assert {p.stem for p in snap.PLANS_DIR.glob("*.json")} == set(PACKS)
    assert snap.differences() == []


def test_a_changed_plan_fails_the_snapshot(tmp_path, monkeypatch):
    import json as _json

    from scripts import kernel_plan_snapshot as snap

    for path in snap.PLANS_DIR.glob("*.json"):
        (tmp_path / path.name).write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    doc = _json.loads((tmp_path / "electronics.json").read_text(encoding="utf-8"))
    doc["c02-electronics-units"][0]["plans"][0]["search_args"]["price_max"] = 3500.0
    (tmp_path / "electronics.json").write_text(snap.dump(doc), encoding="utf-8")
    monkeypatch.setattr(snap, "PLANS_DIR", tmp_path)
    assert snap.differences() == ["electronics"]


# --- drumurile care nu sunt „happy path" ---------------------------------------------------------


@pytest.mark.parametrize("name", PACKS)
def test_a_turn_without_acts_is_reply_only(name):
    planned = _plan(name, _interp(), _state(_shelf(name)))
    assert _only(planned).executor == "reply_only"
    assert planned.gaps == () and planned.disclosures == ()


def test_every_skipped_act_is_disclosed_once_per_act():
    interp = _interp(
        acts=[
            {"kind": "link", "targets": ["r1"]},
            {"kind": "detail", "targets": ["r2"]},
            {"kind": "chitchat"},
        ],
        references=[
            {"id": "r1", "text": "a", "kind": "attribute", "value": "x"},
            {"id": "r2", "text": "b", "kind": "attribute", "value": "y"},
        ],
    )
    planned = _plan("fashion", interp, gate=_gate(skipped=(0, 1)))
    assert [p.executor for p in planned.plans] == ["reply_only"]
    assert planned.disclosures == ((tp.TURN_LEVEL, "invalid_target"),) * 2


def test_a_target_the_resolver_did_not_return_brings_no_id():
    """Un act care numește o referință fără `ResolvedRef` (validarea I22 e a pasului 5): ținta
    lipsește, deci nu apare niciun id și actul rămâne fără țintă."""
    interp = _interp(acts=[{"kind": "detail", "targets": ["r1"]}], references=[_ordinal("r1", 1)])
    planned = _plan("electronics", interp, _state("telefoane"))
    assert _only(planned).executor == "reply_only"
    assert planned.disclosures == ((0, "no_target"),)


def test_a_name_reference_without_a_name_falls_back_to_the_subject():
    """Un `name` negăsit al cărui câmp `name` e gol: nu se inventează un nume, se caută
    subiectul."""
    interp = _interp(
        acts=[{"kind": "link", "targets": ["r1"]}],
        references=[{"id": "r1", "text": "ăla", "kind": "name", "name": "  "}],
    )
    resolved = (_ref("r1", "not_found", [], kind="name", reason="name_not_found"),)
    planned = _plan("electronics", interp, _state("telefoane"), resolved=resolved)
    args = _search(planned)
    assert (args.query, args.product_name) == ("Telefoane", None)
    assert planned.disclosures == ((0, "not_exact_match"),)


def test_an_empty_query_with_only_spaces_is_not_a_search_text():
    """`Act.query` gol sau doar spații nu devine cerere: cade pe subiect (niciun `query=" "`)."""
    plan = _only(
        _plan("electronics", _interp(acts=[{"kind": "find", "query": "   "}]), _state("telefoane"))
    )
    assert plan.search_args.query == "Telefoane"


def test_the_c12_plan_survives_an_ro_ro_locale_through_the_real_chain():
    """Același lanț cu locale regional: subiectul, `depends_on` și ecranul nu depind de forma
    locale-i (recenzia NX-332: „ro-RO" nu vedea nicio etichetă)."""
    journey = next(
        j for j in replay.load_journeys() if j.journey_id == "c12-deictic-then-multi-act"
    )
    regional = replay.Journey(**{**journey.__dict__, "locale": "ro-RO"})
    planned = fc.planned_turn(regional, 1)
    assert [p.executor for p in planned.plans] == ["cart", "search"]
    assert planned.plans[1].depends_on == 0


# --- corecturi după recenzia adversarială ---------------------------------------------------------


def _chain(name: str, turns: list[tuple[str, dict, tuple[str, ...]]]):
    """Turele, în lanț, prin kernelul real (ca `plan_trace`), cu sesiunea DERIVATĂ din plan."""
    state = ConversationStateV2()
    earlier: list[str] = []
    step = None
    for i, (text, compact, shown) in enumerate(turns):
        step = fc.kernel_step(
            name,
            state,
            _interp(**compact),
            text,
            earlier=tuple(earlier[::-1]),
            shown_ids=shown,
            turn_id=f"t{i}",
        )
        state = step.state_after
        earlier.append(text)
    return step


_PHONE = (
    "Vreau un telefon.",
    {
        "acts": [{"kind": "find", "query": "telefon"}],
        "changes": [
            {
                "op": "set",
                "dimension": "category",
                "relation": "eq",
                "value": "telefoane",
                "quote": "telefon",
            }
        ],
    },
    ("el-01", "el-02", "el-03"),
)


def test_review_1_the_planned_budget_survives_the_price_guard(sql_conn):
    """Recenzia, defectul 1: «sub prețul primului» dă un `budget_max` dur (valoarea o calculează
    codul din prețul recitit), iar planul pune `price_max`. Pe calea planificată garda NX-319 îl
    re-judeca pe textul recent și îl ARUNCA (numărul nu e rostit). Sursa lui e starea redusă."""
    step = _chain(
        "electronics",
        [
            _PHONE,
            (
                "Arată-mi ceva sub prețul primului.",
                {
                    "acts": [{"kind": "find"}],
                    "changes": [
                        {
                            "op": "set",
                            "dimension": "price",
                            "relation": "lte",
                            "relative_to": "r1",
                            "quote": "sub prețul primului",
                        }
                    ],
                    "references": [_ordinal("r1", 1)],
                },
                (),
            ),
        ],
    )
    args = _search(step.planned)
    assert args.price_max == 1500.0
    ctx = _tool_ctx("Arată-mi ceva sub prețul primului.")
    _run_tool(sql_conn, ct.run_planned_search, ctx, args)
    params = [p for _, ps in sql_conn.statements for p in ps]
    assert 1500.0 in params, "plafonul dur al planului a dispărut din SQL"
    [event] = _events(ctx, "price_bound_provenance")
    assert (event.properties["source"], event.properties["kept"]) == ("state", True)


def test_review_1_todays_path_still_judges_the_models_budget(sql_conn):
    """Controlul: pe calea de azi (`planned=False`), același plafon trimis de MODEL fără sursă în
    text e tot aruncat, byte-identic cu `main`."""
    ctx = _tool_ctx("Arată-mi ceva sub prețul primului.")
    _run_tool(sql_conn, ct.search_products_tool, ctx, {"query": "telefon", "price_max": 1500})
    params = [p for _, ps in sql_conn.statements for p in ps]
    assert 1500.0 not in params
    [event] = _events(ctx, "price_bound_provenance")
    assert (event.properties["source"], event.properties["kept"]) == ("unsupported", False)


def test_review_2_show_more_after_a_resume_searches_the_resumed_subject():
    """Recenzia, defectul 2: telefoane → huse → «Înapoi la telefoane, mai arată-mi». Reluarea nu
    produce nicio propunere, iar sesiunea e încă a huselor: planul pagina husele sub telefoane."""
    step = _chain(
        "electronics",
        [
            _PHONE,
            (
                "Arată-mi niște huse.",
                {
                    "acts": [{"kind": "find", "query": "huse"}],
                    "changes": [
                        {
                            "op": "set",
                            "dimension": "category",
                            "relation": "eq",
                            "value": "huse",
                            "quote": "huse",
                        }
                    ],
                },
                ("el-25", "el-26", "el-27"),
            ),
            (
                "Înapoi la telefoane, mai arată-mi.",
                {"thread": "resume", "acts": [{"kind": "show_more"}]},
                (),
            ),
        ],
    )
    assert step.delta.proposals == ()
    assert step.gate_state.topic.category_key == "telefoane"
    plan = _only(step.planned)
    assert plan.executor == "search"
    assert plan.search_args.category == "telefoane"


def test_review_3_an_avoided_unmapped_word_never_ranks_up():
    """Recenzia, defectul 3: «Dar nu pentru gaming» (`unmapped` + `avoid`) devenea o nevoie
    `unmapped` POZITIVĂ, deci `rank_terms=["gaming"]`: exact produsele ocolite urcau. Delta o scrie
    acum ca excludere (`restriction`), iar plannerul o duce în `gaps`."""
    step = _chain(
        "electronics",
        [
            _PHONE,
            (
                "Dar nu pentru gaming.",
                {
                    "acts": [{"kind": "find"}],
                    "changes": [
                        {
                            "op": "add",
                            "dimension": "unmapped",
                            "relation": "avoid",
                            "value": "gaming",
                            "quote": "nu pentru gaming",
                        }
                    ],
                },
                (),
            ),
        ],
    )
    args = _search(step.planned)
    assert args.rank_terms == []
    assert "exclusion" in step.planned.gaps
    assert step.gate_state.need_for("unmapped") is None
    excluded = step.gate_state.need_for("restriction")
    assert excluded is not None and excluded.normalized_value == "gaming"
    assert excluded.strength == "soft"  # I25: `unmapped` nu e niciodată dur


def test_review_3_an_avoided_ranking_signal_is_an_exclusion_gap():
    ranking = (RankingSignal("unmapped", "gaming", "avoid"), RankingSignal("color", "alb", "avoid"))
    planned = _plan(
        "electronics", _interp(acts=[{"kind": "find"}]), _state("telefoane"), ranking=ranking
    )
    args = _search(planned)
    assert args.rank_terms == [] and args.prefer == {}
    assert planned.gaps == ("exclusion",)


def test_review_4_a_brand_that_is_not_an_attribute_is_a_gap_not_a_preference():
    """Recenzia, riscul 4: pe catalogul real marca e coloana `b.name`, nu un atribut, iar
    `preference_level` citește doar `attributes`: `prefer["brand"]` n-ar ordona nimic și ar dilua
    celelalte preferințe (media pe dimensiuni). Pe SOLE (fără fațetă `brand`) marca preferată e un
    gol numărat; pe electronice, unde pachetul declară marca fațetă de ATRIBUT, rămâne
    preferință."""
    needs = (_need("brand", "cerave"), _need("skin_type", "dry"))
    planned = _plan("sole-ro", _interp(acts=[{"kind": "find"}]), _state(SOLE_SHELF, needs=needs))
    args = _search(planned)
    assert args.prefer == {"skin_type": ["dry"]}
    assert planned.gaps == ("soft_brand",)
    electronics = _search(
        _plan(
            "electronics",
            _interp(acts=[{"kind": "find"}]),
            _state("telefoane", needs=(_need("brand", "samsung"),)),
        )
    )
    assert electronics.prefer == {"brand": ["Samsung"]}


def test_review_7_the_delegated_loop_has_no_mutation_and_no_model_chosen_product():
    from src.agent.tool_budget import spec_for

    assert not {"cart_add", "checkout_link", "subscribe_back_in_stock"} & tp.DELEGATE_TOOLS
    assert all(not spec_for(t).is_mutation for t in tp.DELEGATE_TOOLS)
    assert tp.DELEGATE_TOOLS == {"clarify_options", "faq_lookup", "check_order"}


def test_review_7_a_bundle_executor_must_be_a_read_tool(caplog):
    with caplog.at_level(logging.WARNING):
        loaded = load_domain_pack(
            _business({"bundle_executors": {"*": "cart_add", "ten": "routine_plan"}})
        )
    assert loaded.bundle_executors == {"ten": "routine_plan"}
    assert "cart_add" in caplog.text


def test_review_8_the_subject_type_orders_the_shelf():
    """Recenzia, riscul 8: «Ce recomanzi?» pe raftul de îngrijire cu tipul `crema` servea tot
    raftul, fără urmă (NX-314 pe calea interpretată). Tipul ordonează (`prefer`), nu exclude; un
    pachet fără fațeta de tip îl raportează ca gol."""
    state = _state(SOLE_SHELF, product_type="crema")
    args = _search(_plan("sole-ro", _interp(acts=[{"kind": "find"}]), state))
    assert args.prefer == {"product_type": ["crema"]}
    planned = _plan(
        "electronics", _interp(acts=[{"kind": "find"}]), _state("telefoane", product_type="telefon")
    )
    assert _search(planned).prefer == {}
    assert planned.gaps == ("subject_type",)


@pytest.mark.parametrize("value", [-5.0, float("nan"), float("inf"), 0.0])
def test_review_9_a_corrupt_budget_does_not_raise_and_does_not_filter(value):
    needs = (_need("budget_max", value, "hard"),)
    planned = _plan(
        "electronics", _interp(acts=[{"kind": "find"}]), _state("telefoane", needs=needs)
    )
    assert _search(planned).price_max is None
    assert planned.gaps == ("soft_budget",)


def test_review_10_changed_is_required_and_an_inferred_signal_is_a_refinement():
    import inspect

    assert inspect.signature(plan_turn).parameters["changed"].default is inspect.Parameter.empty
    state = _state("telefoane", active_search={"fp": "x", "pool": ["el-01"], "cursor": 0})
    ranking = (RankingSignal("color", "negru", "eq"),)
    plan = _only(
        _plan("electronics", _interp(acts=[{"kind": "show_more"}]), state, ranking=ranking)
    )
    assert plan.executor == "search" and plan.search_args.prefer == {"color": ["negru"]}


def test_review_6_the_fixture_session_follows_the_plan_like_production():
    """Recenzia, riscul 6: sesiunea din fixture era setată de mână. Acum o scrie planul, ca în
    producție: o căutare CU produse deschide sesiunea; o citire de catalog fără produse (sau o
    întrebare) o închide; o paranteză fără catalog o păstrează."""
    opened = _chain("electronics", [_PHONE])
    assert opened.state_after.active_search is not None
    empty = _chain("electronics", [(_PHONE[0], _PHONE[1], ())])
    assert empty.state_after.active_search is None
    aside = _chain(
        "electronics",
        [
            _PHONE,
            ("Cât durează livrarea?", {"thread": "aside", "acts": [{"kind": "store_info"}]}, ()),
        ],
    )
    assert aside.state_after.active_search is not None
