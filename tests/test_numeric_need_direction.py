"""NX-334 — direcția unui prag numeric ajunge neschimbată din interpretare în stare.

Pe `main` (NX-330/NX-331), «minim 256 GB» (`storage gte 256`, `explicit`) se persista ca
`storage lte 256`: `delta._need_proposals` păstra relația doar pe preț, iar `normalize_need` lua
operatorul din `NeedSpec`, unde orice fațetă numerică era plafon. O singură cheie pe fațetă făcea
și ca a doua limită s-o înlocuiască pe prima («minim 256», apoi «maxim 512» lăsa doar `lte 512`).

Forma reparată e a bugetului: o cheie pentru limita de jos, una pentru cea de sus
(`<fațetă>_min` / `<fațetă>_max`, ca `budget_min` / `budget_max`). Testele merg prin lanțul real
`check_changes → to_delta → reduce_all`, cu pachetele de fixture, fără model și fără DB."""

from __future__ import annotations

from dataclasses import replace

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from src.conversation.delta import to_delta
from src.conversation.interpretation import CheckedChange
from src.conversation.needs import HARD, SOFT, NeedVocabulary
from src.conversation.provenance import UserWords, check_changes, need_handles
from src.conversation.state_reducer import ReducerPolicy, reduce_all
from src.conversation.state_v2 import ConversationStateV2
from tests.kernel import fixture_catalog as fc
from tests.test_kernel_provenance import ch, interp

PACKS = ("electronics", "fashion", "furniture", "gifts", "sole-ro")


def say(
    state: ConversationStateV2,
    *changes,
    said: str,
    pack_name: str = "electronics",
    pack=None,
    turn_id: str = "t1",
):
    """Un tur: citatele se verifică pe `said`, apoi delta și reducerul, ca în replay."""
    pack = pack or fc.pack(pack_name)
    needs = NeedVocabulary.from_pack(pack)
    i = interp(*changes)
    handles = need_handles(state.needs, needs)
    checked = check_changes(
        i,
        words=UserWords(said),
        handles=handles,
        vocab=fc.vocabulary(pack_name),
        pack=pack,
        locale="ro",
    )
    delta = to_delta(i, checked, handles=handles, needs=needs, turn_id=turn_id)
    reduced = reduce_all(state, list(delta.proposals), ReducerPolicy(vocabulary=needs))
    return reduced, delta


def bounds(state: ConversationStateV2) -> set[tuple[str, str, float]]:
    return {
        (n.key, n.operator, float(n.normalized_value))
        for n in state.needs
        if n.is_active and isinstance(n.normalized_value, (int, float))
    }


# --- tabelul din card (§0), rând cu rând ---------------------------------------------------------


@pytest.mark.parametrize(
    ("pack_name", "change", "said", "expected"),
    [
        (
            "electronics",
            ch(dimension="storage", relation="gte", number=256, unit="gb", quote="minim 256 GB"),
            "vreau un telefon cu minim 256 GB",
            {("storage_min", "gte", 256.0)},
        ),
        (
            "electronics",
            ch(dimension="storage", relation="lte", number=512, unit="gb", quote="maxim 512 GB"),
            "maxim 512 GB",
            {("storage_max", "lte", 512.0)},
        ),
        (
            "electronics",
            ch(dimension="storage", relation="eq", number=256, unit="gb", quote="256 GB"),
            "vreau 256 GB",
            {("storage_min", "gte", 256.0), ("storage_max", "lte", 256.0)},
        ),
        (
            "electronics",
            ch(dimension="screen", relation="gte", number=6.5, unit="inch", quote="minim 6.5 inch"),
            "un ecran de minim 6.5 inch",
            {("screen_min", "gte", 6.5)},
        ),
        (
            "furniture",
            ch(dimension="seats", relation="eq", number=3, unit="locuri", quote="3 locuri"),
            "o canapea de 3 locuri",
            {("seats_min", "gte", 3.0), ("seats_max", "lte", 3.0)},
        ),
        (
            "furniture",
            ch(dimension="width", relation="lte", number=200, unit="cm", quote="maxim 200 cm"),
            "maxim 200 cm lățime",
            {("width_max", "lte", 200.0)},
        ),
        (
            # Regula contractului: un număr de preț fără comparator e plafon, deci `eq` nu mai
            # scrie și limita de jos (pe `main` scria AMBELE chei cu aceeași valoare).
            "electronics",
            ch(dimension="price", relation="eq", number=100, unit="lei", quote="100 lei"),
            "am 100 lei",
            {("budget_max", "lte", 100.0)},
        ),
    ],
    ids=[
        "storage-gte",
        "storage-lte",
        "storage-eq",
        "screen-gte",
        "seats-eq",
        "width-lte",
        "price-eq",
    ],
)
def test_the_direction_of_a_bound_reaches_the_state(pack_name, change, said, expected):
    reduced, _ = say(ConversationStateV2(), change, said=said, pack_name=pack_name)
    assert bounds(reduced.state) == expected


def test_an_interval_over_two_turns_keeps_both_bounds():
    """Pe `main`: «minim 256», apoi «maxim 512» lăsa doar `storage lte 512`."""
    first, _ = say(
        ConversationStateV2(),
        ch(dimension="storage", relation="gte", number=256, unit="gb", quote="minim 256 GB"),
        said="minim 256 GB",
    )
    second, _ = say(
        first.state,
        ch(dimension="storage", relation="lte", number=512, unit="gb", quote="maxim 512 GB"),
        said="si maxim 512 GB",
        turn_id="t2",
    )
    assert bounds(second.state) == {("storage_min", "gte", 256.0), ("storage_max", "lte", 512.0)}


def test_an_interval_in_one_turn_is_not_a_conflict():
    reduced, delta = say(
        ConversationStateV2(),
        ch(dimension="storage", relation="gte", number=256, unit="gb", quote="minim 256 GB"),
        ch(dimension="storage", relation="lte", number=512, unit="gb", quote="maxim 512 GB"),
        said="intre minim 256 GB si maxim 512 GB",
    )
    assert not delta.rejected
    assert bounds(reduced.state) == {("storage_min", "gte", 256.0), ("storage_max", "lte", 512.0)}


# --- limitele încrucișate ------------------------------------------------------------------------


def test_crossing_an_earlier_bound_supersedes_it_and_is_counted():
    first, _ = say(
        ConversationStateV2(),
        ch(dimension="storage", relation="gte", number=512, unit="gb", quote="minim 512 GB"),
        said="minim 512 GB",
    )
    second, _ = say(
        first.state,
        ch(dimension="storage", relation="lte", number=256, unit="gb", quote="maxim 256 GB"),
        said="de fapt maxim 256 GB",
        turn_id="t2",
    )
    assert bounds(second.state) == {("storage_max", "lte", 256.0)}
    crossed = [a for a in second.applied if a.op == "bound_crossed"]
    assert [(a.key, a.outcome) for a in crossed] == [("storage", "superseded")]
    assert "storage_min" in {r.key for r in second.state.revocations}


def test_crossing_an_earlier_budget_supersedes_it():
    """Regula contractului lipsea și pe buget: «sub 100», apoi «minim 150» lăsa ambele active."""
    first, _ = say(
        ConversationStateV2(),
        ch(dimension="price", relation="lte", number=100, unit="lei", quote="sub 100 lei"),
        said="ceva sub 100 lei",
    )
    second, _ = say(
        first.state,
        ch(dimension="price", relation="gte", number=150, unit="lei", quote="minim 150 lei"),
        said="hai minim 150 lei",
        turn_id="t2",
    )
    assert bounds(second.state) == {("budget_min", "gte", 150.0)}
    assert [a.key for a in second.applied if a.op == "bound_crossed"] == ["price"]


def test_a_non_explicit_bound_cannot_cross_an_explicit_one():
    first, _ = say(
        ConversationStateV2(),
        ch(dimension="storage", relation="gte", number=512, unit="gb", quote="minim 512 GB"),
        said="minim 512 GB",
    )
    # Fără comparator în citat, `lte` coboară la `implicit` (provenance): nu poate înlocui un
    # prag spus explicit de client.
    second, _ = say(
        first.state,
        ch(dimension="storage", relation="lte", number=256, unit="gb", quote="256 GB"),
        said="256 GB",
        turn_id="t2",
    )
    assert bounds(second.state) == {("storage_min", "gte", 512.0)}
    assert [(r.op, r.reason) for r in second.rejected] == [("set_need", "hard_downgrade")]


def test_crossing_bounds_in_the_same_turn_persists_nothing():
    reduced, delta = say(
        ConversationStateV2(),
        ch(dimension="storage", relation="lte", number=256, unit="gb", quote="sub 256 GB"),
        ch(dimension="storage", relation="gte", number=512, unit="gb", quote="minim 512 GB"),
        said="sub 256 GB dar minim 512 GB",
    )
    assert {c.rejected for c in delta.rejected} == {"hard_conflict"}
    assert bounds(reduced.state) == set()


# --- handle-uri ----------------------------------------------------------------------------------


def test_handles_name_the_dimension_and_remove_drops_one_bound():
    first, _ = say(
        ConversationStateV2(),
        ch(dimension="storage", relation="gte", number=256, unit="gb", quote="minim 256 GB"),
        ch(dimension="storage", relation="lte", number=512, unit="gb", quote="maxim 512 GB"),
        said="minim 256 GB si maxim 512 GB",
    )
    needs = NeedVocabulary.from_pack(fc.pack("electronics"))
    handles = need_handles(first.state.needs, needs)
    assert {h.dimension for h in handles} == {"storage"}
    low = next(h.handle for h in handles if h.key == "storage_min")
    second, _ = say(
        first.state,
        ch(op="remove", target=low, quote="nu mai conteaza minimul"),
        said="nu mai conteaza minimul",
        turn_id="t2",
    )
    assert bounds(second.state) == {("storage_max", "lte", 512.0)}


# --- vocabularul ---------------------------------------------------------------------------------


def test_the_price_facet_does_not_shadow_the_budget_keys():
    """Toate pachetele au o fațetă numerică `price`. Despicată, ar fi umbrit aliasul
    `price_max → budget_max` pe care îl folosesc scriitorii vechi."""
    needs = NeedVocabulary.from_pack(fc.pack("electronics"))
    assert needs.spec_for("price_max").key == "budget_max"
    assert needs.spec_for("price_min").key == "budget_min"
    assert needs.bounds_for("price") == ("budget_min", "budget_max")


def test_a_numeric_facet_key_alone_is_no_longer_a_need():
    needs = NeedVocabulary.from_pack(fc.pack("electronics"))
    assert needs.spec_for("storage") is None
    assert needs.bounds_for("storage") == ("storage_min", "storage_max")
    assert needs.dimension_of("storage_min") == "storage"


def test_the_default_strength_follows_enforce_ready():
    """I8: dur înseamnă o fațetă `enforce_ready`. Pe `main` orice fațetă numerică era `hard`
    implicit; pe calea interpretată tăria vine din `CheckedChange`, deci implicitul nu decidea,
    dar niciun scriitor nu trebuie să se poată baza pe el."""
    pack = fc.pack("electronics")
    assert NeedVocabulary.from_pack(pack).spec_for("storage_min").default_strength == SOFT
    ready = replace(
        pack,
        facets=tuple(
            replace(f, enforce_ready=True) if f.key == "storage" else f for f in pack.facets
        ),
    )
    assert NeedVocabulary.from_pack(ready).spec_for("storage_min").default_strength == HARD


# --- eșecuri -------------------------------------------------------------------------------------


def _with_operators(pack, key: str, operators: tuple[str, ...]):
    return replace(
        pack,
        facets=tuple(replace(f, operators=operators) if f.key == key else f for f in pack.facets),
    )


def test_a_direction_the_facet_does_not_declare_is_rejected():
    pack = _with_operators(fc.pack("electronics"), "storage", ("lte",))
    reduced, delta = say(
        ConversationStateV2(),
        ch(dimension="storage", relation="gte", number=256, unit="gb", quote="minim 256 GB"),
        said="minim 256 GB",
        pack=pack,
    )
    assert [c.rejected for c in delta.rejected] == ["polarity_conflict"]
    assert bounds(reduced.state) == set()
    assert NeedVocabulary.from_pack(pack).bounds_for("storage") == (None, "storage_max")


def test_a_facet_without_declared_operators_gets_both_bounds():
    pack = _with_operators(fc.pack("electronics"), "storage", ())
    assert NeedVocabulary.from_pack(pack).bounds_for("storage") == ("storage_min", "storage_max")
    reduced, _ = say(
        ConversationStateV2(),
        ch(dimension="storage", relation="gte", number=256, unit="gb", quote="minim 256 GB"),
        said="minim 256 GB",
        pack=pack,
    )
    assert bounds(reduced.state) == {("storage_min", "gte", 256.0)}


# --- proprietate: 5 pachete, orice fațetă numerică, orice direcție -------------------------------


def _numeric_cases():
    """Cazurile se aleg din pachet (`value_type`), nu din vocabularul de nevoi: proprietatea nu
    are voie să depindă de codul pe care îl verifică."""
    cases = []
    for name in PACKS:
        pack = fc.pack(name)
        for facet in pack.facets:
            dimension = facet.key
            if getattr(facet.value_type, "value", facet.value_type) != "number":
                continue
            for relation in ("lte", "gte", "eq"):
                if facet.operators and relation not in facet.operators:
                    continue
                cases.append((name, dimension, relation))
    return cases


_CASES = _numeric_cases()


def test_the_property_covers_every_pack_with_a_numeric_facet():
    assert {name for name, _, _ in _CASES} == set(PACKS)
    assert {"storage", "screen", "seats", "width", "spf", "price"} <= {d for _, d, _ in _CASES}


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(case=st.sampled_from(_CASES), number=st.integers(min_value=1, max_value=5000))
def test_property_the_persisted_operator_is_the_requested_one(case, number):
    """Pentru orice fațetă numerică a celor 5 pachete și orice direcție declarată, operatorul
    nevoii persistate e cel cerut, niciodată inversat (`eq` ⇒ ambele chei; pe preț, plafonul)."""
    name, dimension, relation = case
    needs = NeedVocabulary.from_pack(fc.pack(name))
    change = ch(dimension=dimension, relation=relation, number=number, quote="q")
    checked = CheckedChange(
        change=change,
        dimension=dimension,
        canonical_value=float(number),
        provenance="explicit",
        strength="soft",
        rejected=None,
    )
    delta = to_delta(interp(change), [checked], needs=needs, turn_id="t1")
    state = reduce_all(ConversationStateV2(), list(delta.proposals), ReducerPolicy(needs)).state
    operators = {op for _, op, _ in bounds(state)}
    if relation == "eq" and dimension != "price":
        assert operators == {"gte", "lte"}
    elif relation == "eq":
        assert operators == {"lte"}
    else:
        assert operators == {relation}
    assert {v for _, _, v in bounds(state)} == {float(number)}


def test_the_crossing_is_counted_without_the_facet_name():
    """`need_bound_crossed{bound}`: prețul sau o fațetă, niciodată numele fațetei (P12: cheile nu
    intră în labels)."""
    from src.worker.processor import _emit_state_v2_events

    first, _ = say(
        ConversationStateV2(),
        ch(dimension="storage", relation="gte", number=512, unit="gb", quote="minim 512 GB"),
        said="minim 512 GB",
    )
    second, _ = say(
        first.state,
        ch(dimension="storage", relation="lte", number=256, unit="gb", quote="maxim 256 GB"),
        said="de fapt maxim 256 GB",
        turn_id="t2",
    )
    emitted: list[tuple[str, dict]] = []

    class _Ctx:
        def emit(self, name, **props):
            emitted.append((name, props))

    _emit_state_v2_events(_Ctx(), second, {"needs": []}, 0, False)  # type: ignore[arg-type]
    assert ("need_bound_crossed", {"bound": "facet"}) in emitted
    assert not any("storage" in str(props) for _, props in emitted)


# --- drumurile care nu trec prin `set`/`add` ---------------------------------------------------


def _interval_state():
    first, _ = say(
        ConversationStateV2(),
        ch(dimension="storage", relation="gte", number=256, unit="gb", quote="minim 256 GB"),
        ch(dimension="storage", relation="lte", number=512, unit="gb", quote="maxim 512 GB"),
        said="minim 256 GB si maxim 512 GB",
    )
    needs = NeedVocabulary.from_pack(fc.pack("electronics"))
    handles = {h.key: h.handle for h in need_handles(first.state.needs, needs)}
    return first.state, handles


def test_replace_on_a_bound_with_the_same_direction_supersedes_it():
    """Drumul reușit al lui `supersede` nu era acoperit de niciun test de reducer (singurul test îl
    respingea din sursă), iar NX-334 îl rupsese: `_handle_set_need` întorcea un tuplu."""
    state, handles = _interval_state()
    reduced, delta = say(
        state,
        ch(
            op="replace",
            target=handles["storage_min"],
            dimension="storage",
            relation="gte",
            number=128,
            unit="gb",
            quote="de fapt minim 128 GB",
        ),
        said="de fapt minim 128 GB",
        turn_id="t2",
    )
    assert [p.op for p in delta.proposals] == ["supersede"]
    assert bounds(reduced.state) == {("storage_min", "gte", 128.0), ("storage_max", "lte", 512.0)}
    assert [(a.op, a.outcome) for a in reduced.applied] == [("supersede", "superseded")]


def test_replace_on_a_bound_with_the_opposite_direction_moves_the_value():
    """«minim 256» → «de fapt maxim 512» (`replace c1`, `lte`): pe forma de dinainte scria
    `storage_min = 512`, adică direcția inversată."""
    first, _ = say(
        ConversationStateV2(),
        ch(dimension="storage", relation="gte", number=256, unit="gb", quote="minim 256 GB"),
        said="minim 256 GB",
    )
    needs = NeedVocabulary.from_pack(fc.pack("electronics"))
    [handle] = need_handles(first.state.needs, needs)
    reduced, delta = say(
        first.state,
        ch(
            op="replace",
            target=handle.handle,
            dimension="storage",
            relation="lte",
            number=512,
            unit="gb",
            quote="de fapt maxim 512 GB",
        ),
        said="de fapt maxim 512 GB",
        turn_id="t2",
    )
    assert [(p.op, p.key) for p in delta.proposals] == [
        ("revoke", "storage_min"),
        ("set_need", "storage_max"),
    ]
    assert bounds(reduced.state) == {("storage_max", "lte", 512.0)}


def test_a_supersede_that_crosses_the_opposite_bound_retires_it():
    """`supersede` pe limita de sus sub limita de jos: înregistrările sunt două (`bound_crossed`,
    apoi înlocuirea), iar `_handle_supersede` le poartă pe amândouă."""
    state, handles = _interval_state()
    reduced, _ = say(
        state,
        ch(
            op="replace",
            target=handles["storage_max"],
            dimension="storage",
            relation="lte",
            number=128,
            unit="gb",
            quote="de fapt maxim 128 GB",
        ),
        said="de fapt maxim 128 GB",
        turn_id="t2",
    )
    assert bounds(reduced.state) == {("storage_max", "lte", 128.0)}
    assert [(a.op, a.outcome) for a in reduced.applied] == [
        ("bound_crossed", "superseded"),
        ("supersede", "superseded"),
    ]


def test_replace_in_a_direction_the_facet_does_not_declare_is_rejected():
    pack = _with_operators(fc.pack("electronics"), "storage", ("gte",))
    first, _ = say(
        ConversationStateV2(),
        ch(dimension="storage", relation="gte", number=256, unit="gb", quote="minim 256 GB"),
        said="minim 256 GB",
        pack=pack,
    )
    [handle] = need_handles(first.state.needs, NeedVocabulary.from_pack(pack))
    reduced, delta = say(
        first.state,
        ch(
            op="replace",
            target=handle.handle,
            dimension="storage",
            relation="lte",
            number=512,
            unit="gb",
            quote="de fapt maxim 512 GB",
        ),
        said="de fapt maxim 512 GB",
        pack=pack,
        turn_id="t2",
    )
    assert [c.rejected for c in delta.rejected] == ["polarity_conflict"]
    assert bounds(reduced.state) == {("storage_min", "gte", 256.0)}


def test_a_relative_value_is_only_a_price():
    """Contractul: valoarea unei schimbări relative e calculată din PREȚUL recitit al țintei. Pe
    `width` ieșea `width_max = 1999`, adică prețul pus drept lățime."""
    from src.conversation.interpretation import Reference

    pack = fc.pack("furniture")
    target = Reference(
        id="r1",
        text="asta",
        kind="deictic",
        ordinal=None,
        name=None,
        dimension=None,
        value=None,
        direction=None,
    )
    change = ch(dimension="width", relation="lte", relative_to="r1", quote="mai ingusta decat asta")
    [checked] = check_changes(
        interp(change, refs=[target]),
        words=UserWords("vreau ceva mai ingusta decat asta"),
        vocab=fc.vocabulary("furniture"),
        pack=pack,
        locale="ro",
    )
    assert checked.rejected == "unit_mismatch"


@pytest.mark.parametrize(
    ("said", "changes"),
    [
        (
            "vreau 256 GB dar maxim 128 GB",
            (
                ch(dimension="storage", relation="eq", number=256, unit="gb", quote="256 GB"),
                ch(
                    dimension="storage", relation="lte", number=128, unit="gb", quote="maxim 128 GB"
                ),
            ),
        ),
        (
            "am 100 lei dar minim 150 lei",
            (
                ch(dimension="price", relation="eq", number=100, unit="lei", quote="100 lei"),
                ch(
                    dimension="price", relation="gte", number=150, unit="lei", quote="minim 150 lei"
                ),
            ),
        ),
    ],
    ids=["eq-then-lower-ceiling", "price-eq-then-higher-floor"],
)
def test_an_eq_that_crosses_another_bound_in_the_same_turn_is_a_conflict(said, changes):
    """`eq` pune ambele limite (pe preț, plafonul): încrucișat în ACELAȘI tur cu o limită opusă e
    `hard_conflict`, pe care îl întreabă poarta, nu o răzgândire rezolvată tăcut de reducer."""
    reduced, delta = say(ConversationStateV2(), *changes, said=said)
    assert [c.rejected for c in delta.rejected] == ["hard_conflict", "hard_conflict"]
    assert bounds(reduced.state) == set()
    assert not [a for a in reduced.applied if a.op == "bound_crossed"]
