"""NX-328 felia 1c — pachetele de fixture pe alte verticale + harnessul de replay pe straturi.

Designul kernelului §G: patru pachete sintetice lângă SOLE, ca un test să poată pica atunci când
skincare-ul se scurge în nucleu (I14). Pachetele trec prin ACELAȘI `load_domain_pack` ca producția,
iar un pachet din care loaderul respinge tăcut o fațetă (fail-closed per intrare) pică aici, nu
peste trei pași, când un test de reducer ar da verde pe o fațetă care nu există.

Journey-urile din §C sunt etichetate pe `interpretation`; etichetele trebuie să fie ele însele
conforme cu contractul (I22, citatul chiar scris de client). Zero model, zero DB."""

from __future__ import annotations

import pytest

from src.conversation.interpretation import (
    KERNEL_CONTRACT_VERSION,
    UNIVERSAL_DIMENSIONS,
    AmbiguityDecision,
    ResolvedRef,
    TurnPlan,
)
from src.conversation.kernel_trace import KernelTrace
from src.domain.loader import load_domain_pack
from src.models import BusinessConfig
from tests.kernel import replay

ALL_PACKS = (*replay.FIXTURE_PACKS, "sole-ro")


def _load(name: str):
    doc = replay.load_pack(name)
    business = BusinessConfig(
        id=f"b-{name}",
        slug=name,
        name=name,
        vertical=doc["vertical"],
        settings={"domain_pack": doc["domain_pack"]},
    )
    return doc, load_domain_pack(business)


# --- pachetele -----------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ALL_PACKS)
def test_the_pack_loads_through_the_production_loader_without_losing_anything(name):
    doc, pack = _load(name)
    declared = [f for f in doc["domain_pack"]["facets"] if isinstance(f, dict) and f.get("key")]
    assert pack is not None
    assert {f.key for f in pack.facets} == {f["key"] for f in declared}, (
        "loaderul a respins tăcut o fațetă (fail-closed per intrare)"
    )
    assert set(pack.units.specs) == set(doc["domain_pack"].get("units", {}))


@pytest.mark.parametrize("name", replay.FIXTURE_PACKS)
def test_the_fixture_catalog_is_consistent_with_its_pack(name):
    doc, pack = _load(name)
    products = doc["products"]
    assert 20 <= len(products) <= 40
    assert len({p["id"] for p in products}) == len(products)
    categories = {c["key"] for c in doc["categories"]}
    enums = {f.source_key: set(f.values) for f in pack.facets if f.values}
    for product in products:
        assert product["category"] in categories, product["id"]
        for key, value in product["attributes"].items():
            if key in enums:
                assert value in enums[key], f"{product['id']}: {key}={value!r} nedeclarat"


@pytest.mark.parametrize("name", replay.FIXTURE_PACKS)
def test_the_kernel_scope_names_only_declared_facets(name):
    doc, pack = _load(name)
    keys = {f.key for f in pack.facets}
    scope = doc["kernel"]
    for group in ("conversation_scoped", "topic_scoped", "subject"):
        assert set(scope[group]) <= keys, (group, set(scope[group]) - keys)
    assert not set(scope["conversation_scoped"]) & set(scope["topic_scoped"])


def test_the_packs_disagree_on_scope_so_a_hardcoded_rule_would_fail_one_of_them():
    """Fără pachete care se contrazic, un reducer care hardcodează „culoarea e a subiectului"
    ar trece toate testele. La modă culoarea e a persoanei, la mobilă e a subiectului."""
    fashion, _ = _load("fashion")
    furniture, _ = _load("furniture")
    assert "color" in fashion["kernel"]["conversation_scoped"]
    assert "color" in furniture["kernel"]["topic_scoped"]


# --- journey-urile -------------------------------------------------------------------------------


def test_the_design_section_c_journeys_load_and_validate():
    journeys = replay.load_journeys()
    ids = [j.journey_id for j in journeys]
    assert len(ids) == len(set(ids))
    assert sum(j.source.startswith("KERNEL-DESIGN §C.") for j in journeys) == 12
    assert all(t.expect.get("interpretation") is not None for j in journeys for t in j.turns)


def test_the_labels_themselves_obey_the_contract():
    problems = [p for j in replay.load_journeys() for p in replay.label_problems(j)]
    assert not problems, "\n".join(problems)


def test_every_labeled_dimension_exists_in_the_journeys_pack():
    """O etichetă care numește o dimensiune pe care tenantul n-o are cere modelului o valoare din
    afara enumului: eticheta ar fi imposibilă, nu doar greșită."""
    for journey in replay.load_journeys():
        _, pack = _load(journey.pack)
        allowed = {f.key for f in pack.facets} | set(UNIVERSAL_DIMENSIONS)
        for turn in journey.turns:
            for change in turn.expect["interpretation"].changes:
                if change.dimension is not None:
                    assert change.dimension in allowed, (journey.journey_id, change.dimension)


def test_the_corpus_covers_every_vertical():
    packs = {j.pack for j in replay.load_journeys()}
    assert set(ALL_PACKS) <= packs


def test_label_problems_catches_a_phantom_target_and_an_unwritten_quote():
    journey = replay.Journey(
        journey_id="bad",
        pack="electronics",
        family=(),
        locale="ro",
        source="test",
        turns=(
            replay.JourneyTurn(
                user_input="vreau ceva mai ieftin",
                expect=replay._parse_expect(
                    {
                        "interpretation": {
                            "acts": [{"kind": "detail", "targets": ["r9"]}],
                            "changes": [
                                {"op": "add", "dimension": "price", "quote": "sub 100 lei"}
                            ],
                        }
                    }
                ),
            ),
        ),
    )
    problems = replay.label_problems(journey)
    assert any("r9" in p for p in problems)
    assert any("sub 100 lei" in p for p in problems)


def test_a_compact_label_rejects_unknown_keys():
    with pytest.raises(ValueError):
        replay.expand_interpretation({"acts": [], "goal": "compare"})


# --- replay-ul -----------------------------------------------------------------------------------


def _trace_for(interpretation, *, refs=()) -> KernelTrace:
    return KernelTrace(
        contract_version=KERNEL_CONTRACT_VERSION,
        vocabulary_snapshot="fixture",
        interpretation=interpretation,
        checked_changes=[],
        resolved_refs=list(refs),
        state_before={},
        proposals=[],
        rejected=[],
        state_after={},
        ambiguity=AmbiguityDecision(verdict="act", reason="test", question=None),
        plan=TurnPlan(executor="reply_only", product_ids=[], search_args=None, depends_on=None),
        executor="none",
        answer_policy=None,
    )


def test_replay_passes_when_every_labeled_layer_matches():
    journeys = replay.load_journeys()

    def perfect(journey, index):
        expect = journey.turns[index].expect
        trace = _trace_for(expect["interpretation"], refs=expect.get("resolver", ()))
        return trace.model_copy(update={"checked_changes": list(expect.get("checked", ()))})

    outcomes = replay.replay(journeys, perfect)
    assert outcomes and all(o.passed for o in outcomes)


def test_the_resolver_layer_replays_green_on_every_labeled_journey():
    """NX-329: stratul `resolver` RULEAZĂ (pasul 2) pe fiecare journey etichetat pe el:
    interpretarea etichetată trece prin resolverul real, pe faptele pachetului de fixture."""
    from tests.kernel import fixture_catalog

    journeys = [j for j in replay.load_journeys() if any("resolver" in t.expect for t in j.turns)]
    assert len(journeys) >= 12
    kinds = {
        r.kind for j in journeys for t in j.turns for r in t.expect["interpretation"].references
    }
    assert kinds == {"ordinal", "deictic", "name", "attribute", "extreme", "the_other", "earlier"}
    outcomes = replay.replay(journeys, fixture_catalog.resolver_trace)
    failed = [f"{o.journey_id}#{o.turn}: {o.divergence.report()}" for o in outcomes if not o.passed]
    assert not failed, "\n".join(failed)


def test_the_checked_layer_replays_green_on_every_labeled_journey():
    """NX-330: stratul `checked` RULEAZĂ (pasul 3a): schimbările interpretării etichetate trec prin
    validatorul de proveniență real, pe vocabularul și unitățile pachetului de fixture."""
    from tests.kernel import fixture_catalog

    journeys = [j for j in replay.load_journeys() if any("checked" in t.expect for t in j.turns)]
    assert len(journeys) >= 12
    levels = {c.provenance for j in journeys for t in j.turns for c in t.expect.get("checked", [])}
    assert {"explicit", "implicit"} <= levels
    rejects = {c.rejected for j in journeys for t in j.turns for c in t.expect.get("checked", [])}
    assert {"unit_mismatch", "semantic_mismatch", "hard_conflict"} <= rejects
    outcomes = replay.replay(journeys, fixture_catalog.checked_trace)
    failed = [f"{o.journey_id}#{o.turn}: {o.divergence.report()}" for o in outcomes if not o.passed]
    assert not failed, "\n".join(failed)


def test_a_wrong_checked_label_is_blamed_on_the_checked_layer():
    from tests.kernel import fixture_catalog

    journey = next(
        j
        for j in replay.load_journeys()
        if j.journey_id == "p13-fashion-inflected-value-is-implicit"
    )
    turn = journey.turns[0]
    wrong = [c.model_copy(update={"provenance": "explicit"}) for c in turn.expect["checked"]]
    labeled = replay.JourneyTurn(
        user_input=turn.user_input, expect={**turn.expect, "checked": wrong}
    )
    bad = replay.Journey(**{**journey.__dict__, "turns": (labeled,)})
    [outcome] = replay.replay([bad], fixture_catalog.checked_trace)
    assert outcome.divergence.layer == "checked"
    assert outcome.divergence.passed == ("interpretation",)


def test_a_checked_label_needs_one_entry_per_change():
    with pytest.raises(ValueError):
        replay._parse_expect(
            {
                "interpretation": {"changes": [{"op": "add", "dimension": "color", "quote": "x"}]},
                "checked": [],
            }
        )


def test_a_wrong_resolver_label_is_blamed_on_the_resolver_layer():
    from tests.kernel import fixture_catalog

    journey = next(j for j in replay.load_journeys() if j.journey_id == "r01-electronics-ordinal")
    turn = journey.turns[0]
    wrong = [r.model_copy(update={"product_ids": ["el-03"]}) for r in turn.expect["resolver"]]
    labeled = replay.JourneyTurn(
        user_input=turn.user_input,
        expect={**turn.expect, "resolver": wrong},
        sources=turn.sources,
    )
    bad = replay.Journey(**{**journey.__dict__, "turns": (labeled,)})
    [outcome] = replay.replay([bad], fixture_catalog.resolver_trace)
    assert outcome.divergence.layer == "resolver"
    assert outcome.divergence.passed == ("interpretation",)


def test_replay_blames_the_first_wrong_layer():
    journey = next(j for j in replay.load_journeys() if j.journey_id == "c09-compare-then-cart")
    turn = journey.turns[0]
    labeled = replay.JourneyTurn(
        user_input=turn.user_input,
        expect={
            **turn.expect,
            "resolver": [
                ResolvedRef(
                    ref_id="r1",
                    kind="ordinal",
                    outcome="exact",
                    product_ids=["el-01"],
                    source="shown_now",
                    reason=None,
                )
            ],
        },
    )
    labeled_journey = replay.Journey(**{**journey.__dict__, "turns": (labeled,)})

    def wrong_resolver(j, index):
        ambiguous = ResolvedRef(
            ref_id="r1",
            kind="ordinal",
            outcome="ambiguous",
            product_ids=["el-01", "el-02"],
            source="shown_now",
            reason="set_changed",
        )
        return _trace_for(j.turns[index].expect["interpretation"], refs=[ambiguous])

    [outcome] = replay.replay([labeled_journey], wrong_resolver)
    assert not outcome.passed
    assert outcome.divergence.layer == "resolver"
    assert outcome.divergence.passed == ("interpretation",)
