"""NX-331 (kernel.v1.0, pasul 3b) — reducerul parchează, reia și corectează.

Regresiile din card, scrise ÎNAINTEA codului (pe `origin/main` pică la import: `reduce_turn`,
`ParkedTopic` și `recent_sets` nu există), plus proprietățile Hypothesis pe cele 5 pachete (cele 4
de fixture + pachetul real SOLE). Invarianții contractului dovediți aici: I4 (niciun reset orb), I5
(paranteza e identitate), I6 pe lanț (o cheie retrasă de client revine doar prin `explicit`), I17
(6 KB, `recent_sets` pleacă primul), I19 (un slot, nu o stivă), I20 (executorii nu ating nevoile
și subiectul), I21 (corecția cere o contradicție), I23 (`inferred` nu se persistă). Zero model, zero
DB.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from src.agent.deterministic import _state_v2_sources
from src.conversation.delta import TurnDelta, to_delta
from src.conversation.interpretation import (
    CheckedChange,
    Reference,
    ResolvedRef,
    StateChange,
    TurnInterpretation,
)
from src.conversation.needs import MAX_UNMAPPED_PER_TOPIC, NeedVocabulary
from src.conversation.provenance import need_handles
from src.conversation.references import (
    MAX_LOOKUP_IDS,
    ReferenceSources,
    ShownItem,
    plan_lookup,
    resolve_references,
)
from src.conversation.state_reducer import (
    EXECUTOR_OPS,
    REJECT_REASONS,
    ReducerPolicy,
    StateUpdateProposal,
    reduce_all,
    reduce_turn,
)
from src.conversation.state_v2 import (
    MAX_DISPLAYED,
    MAX_PARKED_NEEDS,
    MAX_RECENT_SETS,
    MAX_STATE_BYTES,
    REVIVE_CAPABLE_SOURCES,
    ConversationStateV2,
    DisplayedRef,
    Need,
    ParkedTopic,
    References,
    Topic,
    _byte_size,
    enforce_caps,
    project_v1,
    serialize,
)
from tests.kernel import fixture_catalog as fc
from tests.kernel import replay

PACKS: tuple[str, ...] = (*replay.FIXTURE_PACKS, "sole-ro")


def policy_for(name: str) -> ReducerPolicy:
    return ReducerPolicy(vocabulary=NeedVocabulary.from_pack(fc.pack(name)))


ELECTRONICS = policy_for("electronics")
CORE = ReducerPolicy(vocabulary=NeedVocabulary.from_pack(None))


def said(op: str, *, source: str = "user_explicit", **kw) -> StateUpdateProposal:
    """O propunere a interpretării (ce scrie `delta.py`)."""
    return StateUpdateProposal(op, source=source, origin="interpretation", turn_id="t", **kw)  # type: ignore[arg-type]


def shown(*ids: str, source: str = "catalog") -> StateUpdateProposal:
    """Ce a arătat executorul turului (setul afișat)."""
    return StateUpdateProposal(
        "set_references",
        source=source,
        payload={"displayed_products": [{"product_id": i, "name": i, "price": 10.0} for i in ids]},
    )


def turn(
    state: ConversationStateV2,
    *proposals: StateUpdateProposal,
    thread: str = "continue",
    executor=(),
    resolved=(),
    primary: str | None = None,
    corrects: bool = False,
    policy: ReducerPolicy = ELECTRONICS,
):
    return reduce_turn(
        state,
        TurnDelta(thread=thread, proposals=tuple(proposals)),  # type: ignore[arg-type]
        tuple(executor),
        tuple(resolved),
        primary,
        corrects,
        policy,
    )


def ids(refs) -> list[str]:
    return [d.product_id for d in refs]


def active(state: ConversationStateV2) -> set[tuple[str, object]]:
    return {(n.key, n.normalized_value) for n in state.active_needs()}


def outcomes(reduced, op: str) -> list[str]:
    return [a.outcome for a in reduced.applied if a.op == op]


def phones_then_tablets():
    """Telefoane (buget ≤ 3000, Samsung) → «arată-mi tablete». Stările după fiecare tur."""
    phones = turn(
        ConversationStateV2(),
        said("set_topic", category_key="telefoane"),
        said("set_need", key="budget_max", value=3000, strength="hard"),
        said("set_need", key="brand", value="samsung"),
        executor=[shown("el-01", "el-07")],
    )
    tablets = turn(
        phones.state,
        said("set_topic", category_key="tablete"),
        executor=[shown("el-15", "el-11")],
    )
    return phones, tablets


# --- Happy path ----------------------------------------------------------------------------------


def test_a_subject_change_parks_the_subject_its_needs_and_its_shown_set():
    phones, tablets = phones_then_tablets()
    state = tablets.state
    assert state.topic.category_key == "tablete"
    assert state.parked is not None and state.parked.topic.category_key == "telefoane"
    # Nevoia cu scope pe subiect pleacă în parcare, cea pe conversație rămâne activă (I4).
    assert [(n.key, n.normalized_value) for n in state.parked.needs] == [("brand", "samsung")]
    assert ("budget_max", 3000.0) in active(state)
    assert ("brand", "samsung") not in active(state)
    assert ids(state.parked.shown) == ["el-01", "el-07"]
    assert ids(state.references.displayed_products) == ["el-15", "el-11"]
    assert outcomes(tablets, "park") == ["parked"]


def test_resume_swaps_with_the_parked_subject_and_reactivates_its_needs_exactly():
    phones, tablets = phones_then_tablets()
    before = next(n for n in phones.state.needs if n.key == "brand" and n.is_active)
    resumed = turn(tablets.state, thread="resume")
    state = resumed.state
    assert state.topic.category_key == "telefoane"
    brand = state.need_for("brand")
    assert brand is not None
    assert (brand.normalized_value, brand.strength, brand.source, brand.confirmed) == (
        before.normalized_value,
        before.strength,
        before.source,
        before.confirmed,
    )
    assert ("budget_max", 3000.0) in active(state)
    assert not any(r.key == "brand" and r.reason_code == "topic_reset" for r in state.revocations)
    # Tabletele devin parcate, iar pe ecran revin telefoanele (revizie nouă: ordinalele vechi
    # devin stale).
    assert state.parked.topic.category_key == "tablete"
    assert ids(state.parked.shown) == ["el-15", "el-11"]
    assert ids(state.references.displayed_products) == ["el-01", "el-07"]
    assert state.references.displayed_revision == state.revision
    assert outcomes(resumed, "resume") == ["swapped"]


def test_the_resolver_gets_the_parked_and_earlier_sets_from_the_v2_state():
    _, tablets = phones_then_tablets()
    resumed = turn(tablets.state, thread="resume").state
    sources = _state_v2_sources(SimpleNamespace(state_v2=resumed))
    assert [s.product_id for s in sources["parked"]] == ["el-15", "el-11"]
    assert sources["focus"] is None
    # Fără stare v2 (flagul stins) sursele rămân goale: calea v1 e neschimbată (I16).
    assert _state_v2_sources(SimpleNamespace(state_v2=None)) == {}


def test_a_new_shown_set_pushes_the_previous_one_into_recent_sets():
    state = turn(ConversationStateV2(), executor=[shown("el-01", "el-02")]).state
    state = turn(state, executor=[shown("el-03", "el-04")]).state
    state = turn(state, executor=[shown("el-05")]).state
    recent = [ids(s) for s in state.references.recent_sets]
    assert recent == [["el-03", "el-04"], ["el-01", "el-02"]]
    # Al treilea set de mai devreme cade (plafonul de 2).
    state = turn(state, executor=[shown("el-06")]).state
    assert [ids(s) for s in state.references.recent_sets] == [["el-05"], ["el-03", "el-04"]]
    assert len(state.references.recent_sets) == MAX_RECENT_SETS


def test_the_earlier_one_resolves_on_recent_sets():
    """«cel de mai devreme» după două seturi arătate: resolverul găsește setul anterior."""
    state = turn(ConversationStateV2(), executor=[shown("el-02")]).state
    state = turn(state, executor=[shown("el-11", "el-12")]).state
    sources = ReferenceSources(
        shown_now=fc.shown("electronics", *ids(state.references.displayed_products)),
        **{
            k: v
            for k, v in _state_v2_sources(SimpleNamespace(state_v2=state)).items()
            if k != "shown_earlier"
        },
        shown_earlier=tuple(fc.shown("electronics", *ids(s)) for s in state.references.recent_sets),
    )
    earlier = Reference(
        id="r1",
        text="cel de mai devreme",
        kind="earlier",
        ordinal=None,
        name=None,
        dimension=None,
        value=None,
        direction=None,
    )
    lookup = plan_lookup([earlier], sources, pack=fc.pack("electronics"), locale="ro")
    [resolved] = resolve_references(
        [earlier], sources, fc.facts("electronics", lookup), pack=fc.pack("electronics")
    )
    assert (resolved.outcome, resolved.source, resolved.product_ids) == (
        "exact",
        "shown_earlier",
        ["el-02"],
    )
    # Un id din `recent_sets` șters din catalog între ture: resolverul nu-l servește (I1).
    [gone] = resolve_references(
        [earlier],
        sources,
        fc.facts("electronics", lookup, drop=["el-02"]),
        pack=fc.pack("electronics"),
    )
    assert gone.outcome == "stale" and gone.product_ids == []


def test_on_resume_the_lookup_cap_never_cuts_the_parked_set_in_focus():
    """Peste `MAX_LOOKUP_IDS` se taie ce e mai departe de focus. Pe `resume` focusul e setul parcat:
    cu seturi la plafonul stării (4 × 8 = 32 de id-uri > 30), el trebuie citit întreg."""

    def synthetic(prefix: str) -> tuple[ShownItem, ...]:
        return tuple(ShownItem(f"{prefix}-{i}", f"{prefix} {i}", 10.0) for i in range(8))

    parked = tuple(sorted(fc.products("electronics"))[:8])
    sources = ReferenceSources(
        shown_now=synthetic("now"),
        shown_earlier=(synthetic("a"), synthetic("b")),
        parked=fc.shown("electronics", *parked),
        thread="resume",
    )
    assert len(sources.sets_in_order()) == 4
    cheapest = Reference(
        id="r1",
        text="cel mai ieftin",
        kind="extreme",
        ordinal=None,
        name=None,
        dimension="price",
        value=None,
        direction="min",
    )
    lookup = plan_lookup([cheapest], sources, pack=fc.pack("electronics"), locale="ro")
    assert len(lookup.ids) == MAX_LOOKUP_IDS
    assert set(parked) <= set(lookup.ids)


def test_a_correction_of_the_previous_turn_supersedes_and_retracts_its_implicit_siblings():
    """«Nu, vreau Apple», turul anterior a scris `brand=samsung` (și un prag de preț dedus)."""
    first = turn(
        ConversationStateV2(),
        said("set_topic", category_key="telefoane"),
        said("set_need", key="brand", value="samsung"),
        said("set_need", key="budget_max", value=3000, strength="hard"),
        said("set_need", key="budget_min", value=1000, source="user_implicit"),
    ).state
    corrected = turn(first, said("set_need", key="brand", value="apple"), corrects=True)
    assert corrected.state.need_for("brand").normalized_value == "apple"
    assert outcomes(corrected, "correction") == ["applied"]
    price = turn(first, said("set_need", key="budget_max", value=2000), corrects=True)
    # Aceeași dimensiune (prețul): pragul IMPLICIT al turului anterior se retrage cu `correction`.
    assert price.state.need_for("budget_max").normalized_value == 2000.0
    assert price.state.need_for("budget_min") is None
    assert any(
        r.key == "budget_min" and r.reason_code == "correction" for r in price.state.revocations
    )


# --- Edge cases ----------------------------------------------------------------------------------


def test_resume_without_anything_parked_is_a_counted_noop():
    state = turn(ConversationStateV2(), said("set_topic", category_key="telefoane")).state
    resumed = turn(state, thread="resume")
    assert outcomes(resumed, "resume") == ["not_available"]
    assert resumed.state.topic == state.topic
    assert resumed.state.needs == state.needs


def test_i19_a_third_subject_evicts_the_parked_one_it_is_not_a_stack():
    """tablete → resume telefoane → laptopuri: telefoanele parcate, tabletele evacuate."""
    _, tablets = phones_then_tablets()
    resumed = turn(tablets.state, thread="resume").state
    laptops = turn(resumed, said("set_topic", category_key="laptopuri"))
    state = laptops.state
    assert state.topic.category_key == "laptopuri"
    assert state.parked.topic.category_key == "telefoane"
    assert [(n.key, n.normalized_value) for n in state.parked.needs] == [("brand", "samsung")]
    assert outcomes(laptops, "park") == ["evicted"]


def test_i5_an_aside_is_the_identity_whatever_the_executors_proposed():
    _, tablets = phones_then_tablets()
    state = tablets.state
    aside = turn(
        state,
        thread="aside",
        executor=[
            StateUpdateProposal("set_active_search", source="catalog", payload={"fp": "faq"}),
            shown("el-20"),
        ],
    )
    assert aside.state is state
    assert aside.applied == () and aside.rejected == ()


def test_a_safety_prune_shrinks_the_set_without_pushing_it():
    state = turn(ConversationStateV2(), executor=[shown("el-01")]).state
    state = turn(state, executor=[shown("el-02", "el-03", "el-04")]).state
    pruned = turn(state, executor=[shown("el-02", "el-04", source="policy")]).state
    assert [ids(s) for s in pruned.references.recent_sets] == [["el-01"]]
    assert ids(pruned.references.displayed_products) == ["el-02", "el-04"]


def test_a_reordered_set_is_not_a_new_set():
    state = turn(ConversationStateV2(), executor=[shown("el-01", "el-02")]).state
    state = turn(state, executor=[shown("el-02", "el-01")]).state
    assert state.references.recent_sets == ()


def test_learn_subject_changing_the_type_on_the_same_shelf_does_not_park():
    """NX-314: `_learn_subject` (propunere veche, `subject=True`) schimbă tipul fără parcare."""
    state = turn(
        ConversationStateV2(),
        said("set_topic", category_key="telefoane"),
        said("set_need", key="brand", value="samsung"),
    ).state
    learned = reduce_all(
        state,
        [
            StateUpdateProposal(
                "set_topic",
                category_key="telefoane",
                product_type="smartphone",
                subject=True,
                source="catalog",
            )
        ],
        ELECTRONICS,
    )
    assert learned.state.topic.product_type == "smartphone"
    assert learned.state.parked is None
    assert learned.state.need_for("brand") is not None
    assert not [a for a in learned.applied if a.op == "park"]


def test_an_interpreted_type_change_on_the_same_shelf_is_a_subject_change():
    """Pe calea interpretată subiectul e PERECHEA (raft, tip): alt tip pe același raft parchează."""
    state = turn(
        ConversationStateV2(),
        said("set_topic", category_key="telefoane", product_type="smartphone"),
    ).state
    changed = turn(state, said("set_topic", category_key="telefoane", product_type="accesoriu"))
    assert changed.state.topic.product_type == "accesoriu"
    assert changed.state.parked.topic.product_type == "smartphone"
    assert outcomes(changed, "park") == ["parked"]


def test_i21_an_add_with_the_correction_flag_is_an_ordinary_add():
    """«Nu, vreau și protecție solară»: `add` obișnuit, `correction{unconfirmed}`, hidratarea
    rămâne."""
    first = turn(
        ConversationStateV2(), said("set_need", key="concerns", value="hydration"), policy=CORE
    ).state
    second = turn(
        first, said("set_need", key="concerns", value="sun_protection"), corrects=True, policy=CORE
    )
    assert {("concerns", "hydration"), ("concerns", "sun_protection")} <= active(second.state)
    assert outcomes(second, "correction") == ["unconfirmed"]
    assert not any(r.reason_code == "correction" for r in second.state.revocations)


def test_i21_the_flag_alone_revokes_nothing_when_an_older_turn_wrote_the_need():
    first = turn(ConversationStateV2(), said("set_need", key="brand", value="samsung")).state
    idle = turn(first).state  # un tur fără schimbări: `brand` nu mai e „al turului anterior"
    later = turn(idle, said("set_need", key="brand", value="apple"), corrects=True)
    assert outcomes(later, "correction") == ["unconfirmed"]
    assert later.state.need_for("brand").normalized_value == "apple"  # set obișnuit (supersede)


def test_the_primary_act_target_becomes_the_selected_product_last():
    resolved = [
        ResolvedRef(
            ref_id="r1",
            kind="extreme",
            outcome="exact",
            product_ids=["el-01"],
            source="shown_now",
            reason="extreme",
        ),
        ResolvedRef(
            ref_id="r2",
            kind="name",
            outcome="ambiguous",
            product_ids=["el-02", "el-08"],
            source="shown_now",
            reason="name_shared",
        ),
    ]
    state = turn(
        ConversationStateV2(),
        executor=[
            StateUpdateProposal(
                "set_references", source="catalog", payload={"selected_product": "el-09"}
            )
        ],
        resolved=resolved,
        primary="r1",
    ).state
    assert state.references.selected_product == "el-01"
    ambiguous = turn(ConversationStateV2(), resolved=resolved, primary="r2").state
    assert ambiguous.references.selected_product is None


def test_budget_survives_a_subject_change_and_a_new_explicit_budget_replaces_it():
    """«cremă de față sub 100 lei» → «arată-mi ce ai la corp» → «pentru corp pot da până la 200»."""
    state = turn(
        ConversationStateV2(),
        said("set_topic", category_key="fata"),
        said("set_need", key="budget_max", value=100),
        policy=CORE,
    ).state
    state = turn(state, said("set_topic", category_key="corp"), policy=CORE).state
    assert state.need_for("budget_max").normalized_value == 100.0
    state = turn(state, said("set_need", key="budget_max", value=200), policy=CORE).state
    assert state.need_for("budget_max").normalized_value == 200.0


def test_unmapped_keeps_at_most_three_per_subject_replacing_the_oldest():
    state = turn(ConversationStateV2(), said("set_topic", category_key="laptopuri")).state
    for word in ("gaming", "birou", "calatorii", "editare"):
        state = turn(state, said("set_need", key="unmapped", value=word, strength="soft")).state
    live = [n.normalized_value for n in state.active_needs() if n.key == "unmapped"]
    assert len(live) == MAX_UNMAPPED_PER_TOPIC
    assert "gaming" not in live and "editare" in live
    assert all(n.strength == "soft" for n in state.active_needs() if n.key == "unmapped")


def test_clear_topic_resets_the_subject_needs_without_parking():
    state = turn(
        ConversationStateV2(),
        said("set_topic", category_key="telefoane"),
        said("set_need", key="brand", value="samsung"),
        said("set_need", key="budget_max", value=3000),
    ).state
    cleared = turn(state, said("clear_topic")).state
    assert cleared.need_for("brand") is None
    assert cleared.need_for("budget_max") is not None  # pe conversație
    assert cleared.parked is None and cleared.topic.category_key == "telefoane"


def test_clear_all_retracts_everything_but_the_protected_needs_and_empties_the_parked_slot():
    _, tablets = phones_then_tablets()
    safety = Need(
        key="restriction",
        operator="not_contains",
        normalized_value="lactoza",
        strength="hard",
        source="policy",
        sensitive_class="health",
    )
    state = replace(tablets.state, needs=(*tablets.state.needs, safety))
    cleared = turn(state, said("clear_all")).state
    assert active(cleared) == {("restriction", "lactoza")}
    assert cleared.parked is None
    # Un tur care nu vine de la client nu poate șterge criteriile clientului (D7).
    blocked = turn(state, said("clear_all", source="user_implicit"))
    assert [r.reason for r in blocked.rejected] == ["unsupported_revoke"]


# --- Failure cases -------------------------------------------------------------------------------


def test_i20_an_executor_cannot_write_needs_or_subject():
    _, tablets = phones_then_tablets()
    rogue = [
        StateUpdateProposal("set_need", key="brand", value="apple", source="catalog"),
        StateUpdateProposal("set_topic", category_key="huse", source="catalog"),
        shown("el-16"),
    ]
    reduced = turn(tablets.state, executor=rogue)
    assert sorted(r.reason for r in reduced.rejected) == ["executor_state_scope"] * 2
    assert reduced.state.topic == replace(tablets.state.topic)
    assert active(reduced.state) == active(tablets.state)
    assert ids(reduced.state.references.displayed_products) == ["el-16"]
    assert "executor_state_scope" in REJECT_REASONS


# --- Regresiile găsite la review (fiecare pica înainte de reparație) -----------------------------


def test_an_interpreted_shelf_key_is_kept_verbatim_like_the_subject_owner_writes_it():
    """Cheile de raft sunt slug-uri de catalog (`ten-ingrijirea-tenului`). `norm_key` le făcea
    `ten_ingrijirea_tenului`, deci același raft repetat de interpretare arăta ca alt subiect."""
    shelf = "ten-ingrijirea-tenului"
    state = reduce_all(
        ConversationStateV2(),
        [StateUpdateProposal("set_topic", category_key=shelf, subject=True, source="catalog")],
        CORE,
    ).state
    state = turn(state, said("set_need", key="brand", value="cerave"), policy=CORE).state
    again = turn(state, said("set_topic", category_key=shelf), policy=CORE)
    assert again.state.topic.category_key == shelf
    assert again.state.parked is None and again.state.need_for("brand") is not None
    assert outcomes(again, "park") == []
    # Nici corecția nu vede o schimbare de subiect pe același raft.
    fixed = turn(state, said("set_topic", category_key=shelf), corrects=True, policy=CORE)
    assert outcomes(fixed, "correction") == ["unconfirmed"]


def _chain(state, *changes, corrects=False, policy=CORE):
    """Un tur prin `to_delta` real, cu handle-urile stării (ce ar face pasul 6)."""
    handles = need_handles(state.needs, policy.vocabulary)
    checked = list(changes)
    interp = TurnInterpretation(
        thread="continue",
        acts=[],
        changes=[c.change for c in checked],
        references=[],
        ambiguities=[],
        corrects_previous_turn=corrects,
    )
    delta = to_delta(interp, checked, handles=handles, needs=policy.vocabulary, turn_id="t")
    return reduce_turn(state, delta, (), (), None, corrects, policy)


def _handle_of(state, key, value) -> str:
    handles = need_handles(state.needs, CORE.vocabulary)
    return next(h.handle for h in handles if (h.key, h.value) == (key, value))


def _on_handle(op: str, handle: str, dimension: str, value=None) -> CheckedChange:
    base = _checked(dimension, value, "explicit", op=op)
    return base.model_copy(update={"change": base.change.model_copy(update={"target": handle})})


def test_replace_on_a_list_handle_replaces_that_value_and_is_a_contradiction():
    """«Nu acnee, hidratare»: `replace` pe un handle de listă înlocuiește ACEA valoare. Înainte,
    `supersede` purta doar valoarea nouă, deci pe o listă ADĂUGA (acneea rămânea), iar corecția nu
    vedea contradicția și lăsa în viață nevoia implicită a turului anterior."""
    first = turn(
        ConversationStateV2(),
        said("set_need", key="concerns", value="acne"),
        said("set_need", key="concerns", value="pores", source="user_implicit"),
        policy=CORE,
    ).state
    handle = _handle_of(first, "concerns", "acne")
    fixed = _chain(first, _on_handle("replace", handle, "concerns", "hydration"), corrects=True)
    assert active(fixed.state) == {("concerns", "hydration")}
    assert outcomes(fixed, "correction") == ["applied"]
    assert any(r.reason_code == "correction" for r in fixed.state.revocations)


def test_remove_on_a_list_handle_removes_only_that_value():
    first = turn(
        ConversationStateV2(),
        said("set_need", key="concerns", value="acne"),
        said("set_need", key="concerns", value="pores"),
        policy=CORE,
    ).state
    removed = _chain(
        first, _on_handle("remove", _handle_of(first, "concerns", "pores"), "concerns")
    )
    assert active(removed.state) == {("concerns", "acne")}


def test_resume_is_blocked_only_by_a_retraction_of_the_parked_value():
    """O retragere pe ALT subiect («fără brandul ăsta» pe tablete, altă valoare) nu blochează marca
    parcată a telefoanelor. Retragerea chiar a valorii parcate o blochează (I6)."""
    _, tablets = phones_then_tablets()
    other = turn(tablets.state, said("set_need", key="brand", value="apple")).state
    other = turn(other, said("revoke", key="brand")).state
    assert turn(other, thread="resume").state.need_for("brand").normalized_value == "samsung"
    same = turn(tablets.state, said("revoke", key="brand", value="samsung")).state
    assert turn(same, thread="resume").state.need_for("brand") is None


def test_a_budget_written_before_the_card_with_a_shelf_scope_stays_on_a_subject_change():
    """Documentele v2 scrise înainte de NX-331 poartă `scope=<raft>` pe buget. Scope-ul se judecă pe
    vocabularul turului, nu pe ce s-a scris atunci: bugetul rămâne al conversației."""
    old_budget = Need(
        key="budget_max",
        operator="lte",
        normalized_value=100.0,
        strength="hard",
        source="user_explicit",
        updated_revision=1,
        scope="fata",
    )
    state = ConversationStateV2(revision=1, topic=Topic(category_key="fata"), needs=(old_budget,))
    moved = turn(state, said("set_topic", category_key="corp"), policy=CORE).state
    assert moved.need_for("budget_max").normalized_value == 100.0
    assert moved.parked.needs == ()


def test_an_unmapped_value_that_does_not_normalize_evicts_nothing():
    state = turn(ConversationStateV2(), said("set_topic", category_key="laptopuri")).state
    for word in ("gaming", "birou", "calatorii"):
        state = turn(state, said("set_need", key="unmapped", value=word)).state
    junk = "bun pentru jocuri grele seara tarziu"  # peste 4 cuvinte ⇒ nu e token canonic
    after = turn(state, said("set_need", key="unmapped", value=junk)).state
    live = {n.normalized_value for n in after.active_needs() if n.key == "unmapped"}
    assert live == {"gaming", "birou", "calatorii"}


def test_resume_drops_references_that_belong_to_the_subject_leaving():
    _, tablets = phones_then_tablets()
    state = replace(
        tablets.state,
        references=replace(
            tablets.state.references,
            selected_product="el-15",
            compared_products=("el-15", "el-11"),
        ),
    )
    resumed = turn(state, thread="resume").state
    assert resumed.references.selected_product is None
    assert resumed.references.compared_products == ()


def test_resume_without_a_current_subject_keeps_the_shown_set_as_an_earlier_one():
    parked = ParkedTopic(topic=Topic(category_key="telefoane"), shown=(DisplayedRef("el-01"),))
    state = ConversationStateV2(
        revision=3,
        references=References(displayed_products=(DisplayedRef("el-20"),)),
        parked=parked,
    )
    resumed = turn(state, thread="resume").state
    assert ids(resumed.references.displayed_products) == ["el-01"]
    assert [ids(s) for s in resumed.references.recent_sets] == [["el-20"]]


def test_the_parked_cap_keeps_the_newest_needs_like_the_active_cap():
    needs = tuple(
        Need(key=f"s{i}", operator="eq", normalized_value="v", scope="p", updated_revision=4)
        for i in range(MAX_PARKED_NEEDS + 1)
    )
    state = ConversationStateV2(parked=ParkedTopic(topic=Topic(category_key="p"), needs=needs))
    kept = [n.key for n in enforce_caps(state).parked.needs]
    assert "s0" not in kept and f"s{MAX_PARKED_NEEDS}" in kept


def test_i6_a_key_revoked_after_parking_stays_revoked_on_resume():
    _, tablets = phones_then_tablets()
    revoked = turn(tablets.state, said("revoke", key="brand")).state
    resumed = turn(revoked, thread="resume").state
    assert resumed.need_for("brand") is None
    assert "brand" in resumed.revoked_keys()


def _full_state(name_chars: int) -> ConversationStateV2:
    """16 nevoi (8 dure), 8 produse pe ecran, parcat plin (8 nevoi dure + 8 produse), 2 seturi
    recente. Numele de produs de `name_chars` caractere (plafonul e `MAX_NAME_CHARS` = 80)."""
    refs = tuple(
        DisplayedRef(product_id=f"p{i:02d}", name="N" * name_chars, price=1.0) for i in range(8)
    )
    hard = [
        Need(key=f"k{i}", operator="eq", normalized_value="v" * 30, strength="hard", scope="t")
        for i in range(8)
    ]
    soft = [
        Need(key=f"s{i}", operator="eq", normalized_value="v" * 30, strength="soft", scope="t")
        for i in range(8)
    ]
    return ConversationStateV2(
        revision=9,
        topic=Topic(category_key="t"),
        needs=tuple(hard + soft),
        references=References(displayed_products=refs, recent_sets=(refs, refs)),
        parked=ParkedTopic(
            topic=Topic(category_key="p"),
            needs=tuple(replace(n, scope="p") for n in hard[:MAX_PARKED_NEEDS]),
            shown=refs,
        ),
    )


def test_i17_a_full_state_fits_and_no_hard_need_is_lost():
    """Documentul plin din card, cu nume de produs la plafon: MĂSURAT, nici fără seturile recente
    nu încape (≈ 6,3 KB), deci degradarea merge mai departe pe ordinea declarată. Nicio nevoie
    dură, activă sau parcată, nu se pierde."""
    full = _full_state(80)
    doc, size, degraded = serialize(full)
    assert degraded and size <= MAX_STATE_BYTES
    assert "recent_sets" not in doc["references"]
    hard_active = [n["key"] for n in doc["needs"] if n["strength"] == "hard"]
    hard_parked = [n["key"] for n in doc["parked"]["needs"] if n["strength"] == "hard"]
    assert sorted(hard_active) == sorted(hard_parked) == [f"k{i}" for i in range(8)]


def test_i17_recent_sets_are_the_first_thing_lost():
    """Când seturile de mai devreme ajung ca să încapă, doar ele pleacă: ecranul, parcatul și
    nevoile rămân întregi."""
    full = _full_state(40)
    bare = replace(full, references=replace(full.references, recent_sets=()))
    assert _byte_size(bare.to_jsonb()) <= MAX_STATE_BYTES < _byte_size(full.to_jsonb())
    doc, size, degraded = serialize(full)
    assert degraded and size <= MAX_STATE_BYTES
    assert doc == bare.to_jsonb()
    assert len(doc["references"]["displayed_products"]) == MAX_DISPLAYED
    assert len(doc["parked"]["shown"]) == MAX_DISPLAYED
    assert len(doc["needs"]) == 16 and len(doc["parked"]["needs"]) == MAX_PARKED_NEEDS


def test_i17_hard_needs_of_the_parked_topic_survive_like_the_active_ones():
    bulky = tuple(
        Need(key=f"s{i}", operator="eq", normalized_value="v" * 40, scope="p") for i in range(8)
    )
    hard = Need(key="k", operator="eq", normalized_value="x", strength="hard", scope="p")
    state = ConversationStateV2(
        parked=ParkedTopic(topic=Topic(category_key="p"), needs=(hard, *bulky)),
        passthrough={"blob": "x" * (MAX_STATE_BYTES - 600)},
    )
    doc, size, degraded = serialize(state)
    assert degraded and size <= MAX_STATE_BYTES
    assert [n["key"] for n in doc["parked"]["needs"]] == ["k"]


def test_a_v2_document_written_before_the_card_hydrates_without_loss():
    old = {
        "schema_version": 2,
        "revision": 4,
        "topic": {"category_key": "telefoane", "changed_at_revision": 2},
        "needs": [
            {
                "key": "brand",
                "operator": "eq",
                "normalized_value": "samsung",
                "strength": "soft",
                "status": "active",
                "source": "user_explicit",
                "updated_revision": 2,
                "scope": "telefoane",
            }
        ],
        "references": {"displayed_products": [{"product_id": "el-01", "name": "x"}]},
        "cart": [{"sku": "a"}],
    }
    state = ConversationStateV2.from_jsonb(old)
    assert state.parked is None and state.references.recent_sets == ()
    assert state.to_jsonb() == old


def test_project_v1_does_not_see_the_new_fields():
    _, tablets = phones_then_tablets()
    state = turn(tablets.state, executor=[shown("el-17")]).state
    assert state.parked is not None and state.references.recent_sets
    bare = replace(state, parked=None, references=replace(state.references, recent_sets=()))
    assert project_v1(state) == project_v1(bare)


# --- Proprietăți (Hypothesis, 5 pachete) ---------------------------------------------------------

_PRODUCTS = tuple(f"p{i}" for i in range(10))


def _catalogue(name: str) -> dict[str, list[tuple[str, object]]]:
    """Cheile generabile ale pachetului, cu valori normalizabile, pe scope."""
    vocab = NeedVocabulary.from_pack(fc.pack(name))
    out: dict[str, list[tuple[str, object]]] = {"topic": [], "conversation": []}
    for key, spec in sorted(vocab.specs.items()):
        if spec.kind.value in ("numeric_max", "numeric_min"):
            values: list[object] = [100.0, 200.0]
        elif spec.kind.value in ("scalar", "list") and key != "unmapped":
            values = sorted(spec.values)[:3] or ["alfa", "beta"]
        else:
            continue
        bucket = "topic" if spec.scoped else "conversation"
        out[bucket] += [(key, v) for v in values]
    return out


_CATEGORIES = ("c1", "c2", "c3")


@st.composite
def _turns(draw, name: str):
    keys = _catalogue(name)
    pairs = keys["topic"] + keys["conversation"]
    # Ponderi: schimbările de subiect și `set` domină, ca secvențele să ajungă des în situația
    # care contează (o nevoie scrisă, apoi un alt subiect). Pe distribuția uniformă, mutantul
    # „fără parcare" supraviețuia pe două pachete din cinci.
    kinds = st.sampled_from(("topic", "topic", "set", "set", "set", "remove", "clear"))

    def change_of(kind: str):
        if kind == "topic":
            return st.tuples(st.just("topic"), st.sampled_from(_CATEGORIES))
        if kind == "set":
            return st.tuples(
                st.just("set"),
                st.sampled_from(pairs),
                st.sampled_from(("user_explicit", "user_explicit", "user_implicit")),
            )
        if kind == "remove":
            return st.tuples(st.just("remove"), st.sampled_from(pairs))
        return st.tuples(st.just("clear"), st.sampled_from(("clear_topic", "clear_all")))

    change = kinds.flatmap(change_of)
    executor = st.one_of(
        st.tuples(st.just("shown"), st.lists(st.sampled_from(_PRODUCTS), max_size=4, unique=True)),
        st.tuples(st.just("rogue"), st.sampled_from(pairs)),
        st.tuples(st.just("search"), st.integers(0, 3)),
    )
    return draw(
        st.lists(
            st.tuples(
                st.sampled_from(("continue", "continue", "resume", "aside")),
                st.lists(change, max_size=3),
                st.lists(executor, max_size=2),
            ),
            min_size=2,
            max_size=8,
        )
    )


def _proposal(change) -> StateUpdateProposal:
    kind = change[0]
    if kind == "topic":
        return said("set_topic", category_key=change[1])
    if kind == "set":
        (key, value), source = change[1], change[2]
        return said("set_need", key=key, value=value, source=source)
    if kind == "remove":
        key, value = change[1]
        return said("revoke", key=key, value=value)
    return said(change[1])


def _executor(item) -> StateUpdateProposal:
    kind = item[0]
    if kind == "shown":
        return shown(*item[1])
    if kind == "rogue":
        key, value = item[1]
        return StateUpdateProposal("set_need", key=key, value=value, source="catalog")
    return StateUpdateProposal("set_active_search", source="catalog", payload={"page": item[1]})


def _touched(proposals, key: str) -> bool:
    return any(
        p.op in ("clear_all", "clear_topic")
        or (p.op in ("set_need", "revoke", "supersede") and p.key == key)
        for p in proposals
    )


def _walk(name: str, script):
    """Rulează scriptul; întoarce (stare înainte, tur, rezultat, propuneri) per tur."""
    policy = policy_for(name)
    state = ConversationStateV2()
    for thread, changes, executor in script:
        proposals = tuple(_proposal(c) for c in changes)
        # `delta.py` pune schimbarea de subiect prima, iar paranteza cu schimbări devine `continue`.
        proposals = tuple(p for p in proposals if p.op == "set_topic") + tuple(
            p for p in proposals if p.op != "set_topic"
        )
        if thread == "aside" and proposals:
            thread = "continue"
        delta = TurnDelta(thread=thread, proposals=proposals)  # type: ignore[arg-type]
        executors = tuple(_executor(e) for e in executor)
        reduced = reduce_turn(state, delta, executors, (), None, False, policy)
        yield state, delta, executors, reduced, policy
        state = reduced.state


_PROPERTY = settings(
    max_examples=120,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)


@pytest.mark.parametrize("name", PACKS)
@_PROPERTY
@given(data=st.data())
def test_i4_no_blind_reset(name, data):
    """O nevoie pe conversație dispare doar prin `remove`/`set` pe cheia ei sau `clear all`. Una pe
    subiect dispare doar prin astea sau printr-o schimbare de subiect, și atunci e în `parked` (sau
    slotul a fost evacuat, numărat). NX-334: plus limita pe care o încrucișează limita opusă nouă
    (contractul, „Corrections and conflicts"), înlocuire numărată ca `bound_crossed`."""
    script = data.draw(_turns(name))
    for before, delta, _, reduced, policy in _walk(name, script):
        after = reduced.state
        crossed = {a.key for a in reduced.applied if a.op == "bound_crossed"}
        kept = active(after)
        parked = {(n.key, n.normalized_value) for n in (after.parked.needs if after.parked else ())}
        evicted = "evicted" in outcomes(reduced, "park")
        switched = delta.thread == "resume" or any(
            a.op == "set_topic" and a.outcome == "reset" for a in reduced.applied
        )
        for need in before.active_needs():
            mark = (need.key, need.normalized_value)
            if mark in kept or _touched(delta.proposals, need.key):
                continue
            if policy.vocabulary.dimension_of(need.key) in crossed:
                continue
            assert need.scope is not None, f"nevoie pe conversație pierdută: {mark}"
            assert switched, f"nevoie pe subiect pierdută fără schimbare de subiect: {mark}"
            assert mark in parked or evicted, f"nici parcată, nici evacuată: {mark}"


@pytest.mark.parametrize("name", PACKS)
@_PROPERTY
@given(data=st.data())
def test_i5_aside_and_i20_executors_never_move_needs_or_subject(name, data):
    script = data.draw(_turns(name))
    for before, delta, executors, reduced, policy in _walk(name, script):
        if delta.thread == "aside":
            assert reduced.state is before
        allowed = tuple(p for p in executors if p.op in EXECUTOR_OPS)
        bare = reduce_turn(before, delta, (), (), None, False, policy).state
        again = reduce_turn(before, delta, allowed, (), None, False, policy).state
        for other in (bare, again):
            assert other.needs == reduced.state.needs
            assert other.topic == reduced.state.topic
            assert other.parked == reduced.state.parked
        # Determinism: aceleași intrări ⇒ același document, byte cu byte.
        twin = reduce_turn(before, delta, executors, (), None, False, policy).state
        assert twin.to_jsonb() == reduced.state.to_jsonb()


@pytest.mark.parametrize("name", PACKS)
@_PROPERTY
@given(data=st.data())
def test_i6_on_the_chain_a_client_revoked_key_returns_only_through_explicit_evidence(name, data):
    script = data.draw(_turns(name))
    for before, delta, _, reduced, _ in _walk(name, script):
        revoked = {r.key for r in before.revocations if r.reason_code == "user_explicit"} - {
            n.key for n in before.active_needs()
        }
        for need in reduced.state.active_needs():
            if need.key not in revoked:
                continue
            assert any(
                p.op in ("set_need", "supersede")
                and p.key == need.key
                and p.source in REVIVE_CAPABLE_SOURCES
                for p in delta.proposals
            ), f"cheie retrasă de client reînviată fără dovadă explicită: {need.key}"


@pytest.mark.parametrize("name", PACKS)
@_PROPERTY
@given(data=st.data())
def test_i17_any_reached_state_fits_the_budget(name, data):
    script = data.draw(_turns(name))
    for _, _, _, reduced, _ in _walk(name, script):
        doc, size, degraded = serialize(reduced.state)
        assert size <= MAX_STATE_BYTES
        if degraded:
            assert "recent_sets" not in doc.get("references", {})


_LEVELS = ("explicit", "implicit", "inferred")


def _checked(dimension: str, value, provenance: str, op: str = "add") -> CheckedChange:
    change = StateChange(
        op=op,  # type: ignore[arg-type]
        target=None,
        dimension=dimension,
        relation=None,
        value=str(value),
        number=None,
        unit=None,
        relative_to=None,
        quote="q",
    )
    return CheckedChange(
        change=change,
        dimension=dimension,
        canonical_value=value,
        provenance=provenance,  # type: ignore[arg-type]
        strength="soft" if provenance != "inferred" else "ranking",
        rejected=None,
    )


@pytest.mark.parametrize("name", PACKS)
@_PROPERTY
@given(data=st.data())
def test_i23_an_inferred_change_never_becomes_persistent_state(name, data):
    keys = _catalogue(name)
    pairs = [(k, v) for k, v in keys["topic"] + keys["conversation"] if not k.startswith("budget")]
    policy = policy_for(name)
    state = ConversationStateV2()
    for _ in range(data.draw(st.integers(1, 4))):
        changes = data.draw(
            st.lists(
                st.tuples(st.sampled_from(pairs), st.sampled_from(_LEVELS)),
                max_size=4,
            )
        )
        checked = [_checked(k, v, p) for (k, v), p in changes]
        interp = TurnInterpretation(
            thread="continue",
            acts=[],
            changes=[c.change for c in checked],
            references=[],
            ambiguities=[],
            corrects_previous_turn=False,
        )
        delta = to_delta(interp, checked, needs=policy.vocabulary, turn_id="t")
        state = reduce_turn(state, delta, (), (), None, False, policy).state
        assert not [n for n in state.needs if n.source == "model_inferred"]


@pytest.mark.parametrize("name", replay.FIXTURE_PACKS)
def test_the_pack_scope_metadata_and_the_facet_data_agree(name):
    """`kernel.conversation_scoped` al pachetului e exact mulțimea fațetelor cu `scope:
    conversation`, iar vocabularul de nevoi le tratează ca atare: un reducer care ar hardcoda
    „culoarea e a subiectului" ar pica pe modă."""
    doc = replay.load_pack(name)
    loaded = fc.pack(name)
    declared = {f.key for f in loaded.facets if f.scope == "conversation"}
    assert declared == set(doc["kernel"]["conversation_scoped"])
    vocab = NeedVocabulary.from_pack(loaded)

    def need_keys(key: str) -> tuple[str, ...]:
        # NX-334: o fațetă numerică are două chei de nevoie, câte una pe limită.
        pair = vocab.bounds_for(key)
        return tuple(k for k in pair if k) if pair is not None else (key,)

    for key in declared:
        assert all(vocab.spec_for(k).scoped is False for k in need_keys(key))
    for key in doc["kernel"]["topic_scoped"]:
        assert all(vocab.spec_for(k).scoped is True for k in need_keys(key))
