"""NX-349 — o fațetă da/nu (`value_type: bool`) nu mai ajunge `unmapped`.

Reproducerea din recenzia promptului v4: pe `21bfc40d` modelul scrisese `set fragrance_free true`,
iar validatorul o muta pe `unmapped` cu valoarea „true", fiindcă vocabularul catalogului nu ține
booleeni. În lanț asta devenea `SearchArgs.rank_terms=['true']`: fără filtru și fără gol dezvăluit.

Ce dovedește suita:
- validatorul păstrează dimensiunea, cu valoarea canonică `true`/`false`, iar proveniența se judecă
  pe frazele PACHETULUI care numesc fațeta (eticheta locale-i, aliasurile);
- starea opusă, negația chiar înaintea frazei și o relație fără sens pe un fanion sunt respinse;
- `matched` rămâne gol (altfel plannerul NX-352 ar duce «parfum» în textul căutării);
- în lanț: nevoia booleană ajunge în stare și plannerul o dezvăluie ca gol, niciodată `rank_terms`.
Zero model, zero DB."""

from __future__ import annotations

import dataclasses

import pytest

from src.conversation.interpretation import Act, StateChange, TurnInterpretation
from src.conversation.state_v2 import ConversationStateV2
from src.domain.facets import FacetType
from tests.kernel import fixture_catalog as fc
from tests.test_kernel_provenance import SOLE, ch, sole

FLAG = "fragrance_free"


def flag(value: str = "true", quote: str = "", relation: str = "eq") -> StateChange:
    return ch("set", dimension=FLAG, relation=relation, value=value, quote=quote)


# --- validatorul ----------------------------------------------------------------------------------


def test_the_pack_declares_the_facet_as_a_flag():
    facet = next(f for f in SOLE.facets if f.key == FLAG)
    assert facet.value_type is FacetType.BOOL


def test_reproduction_a_flag_is_no_longer_moved_to_unmapped():
    """Pe `main` ieșea `unmapped true implicit`."""
    said = "vreau un sampon fara parfum"
    checked = sole(flag(quote=said), said)
    assert checked.dimension == FLAG and checked.canonical_value == "true"
    assert checked.rejected is None


def test_the_label_of_the_facet_makes_it_explicit():
    said = "vreau un sampon fara parfum"
    checked = sole(flag(quote="fara parfum"), said)
    assert checked.provenance == "explicit"
    # nu e `enforce_ready`, deci tare doar pe un filtru auditat (I8)
    assert checked.strength == "soft"


def test_a_description_without_the_facet_name_is_implicit():
    said = "un sampon sa nu contina parfum"
    checked = sole(flag(quote="sa nu contina parfum"), said)
    assert checked.dimension == FLAG and checked.canonical_value == "true"
    assert checked.provenance == "implicit" and checked.rejected is None


def test_a_quote_not_said_by_the_client_is_inferred():
    checked = sole(flag(quote="fara parfum"), "vreau un sampon")
    assert checked.provenance == "inferred" and checked.strength == "ranking"


def test_the_opposite_state_of_the_label_is_a_semantic_mismatch():
    said = "vreau un sampon fara parfum"
    checked = sole(flag("false", quote="fara parfum"), said)
    assert checked.rejected == "semantic_mismatch"


def test_a_negation_right_before_the_label_is_a_polarity_conflict():
    said = "nu fara parfum, vreau cu parfum"
    checked = sole(flag(quote="nu fara parfum"), said)
    assert checked.rejected == "polarity_conflict"


@pytest.mark.parametrize("relation", ["avoid", "lte", "gte"])
def test_a_flag_carries_its_own_polarity(relation):
    said = "vreau un sampon fara parfum"
    checked = sole(flag(quote="fara parfum", relation=relation), said)
    assert checked.rejected == "polarity_conflict"


@pytest.mark.parametrize("value", ["TRUE", " true "])
def test_the_flag_value_is_folded(value):
    said = "vreau un sampon fara parfum"
    assert sole(flag(value, quote="fara parfum"), said).canonical_value == "true"


def test_a_non_flag_value_on_a_flag_facet_keeps_todays_path():
    """O valoare care nu e `true`/`false` rămâne pe calea de azi (re-rezolvare, altfel
    `unmapped`): NX-349 nu inventează un fanion din cuvintele modelului."""
    said = "un sampon cu parfum discret"
    checked = sole(flag("discret", quote="parfum discret"), said)
    assert checked.dimension == "unmapped"


def test_an_alias_of_the_pack_names_the_facet():
    facet = next(f for f in SOLE.facets if f.key == FLAG)
    aliased = dataclasses.replace(facet, aliases={"hipoalergenic parfum zero": "true"})
    pack = dataclasses.replace(
        SOLE, facets=tuple(aliased if f.key == FLAG else f for f in SOLE.facets)
    )
    said = "caut ceva hipoalergenic parfum zero"
    from src.conversation.provenance import UserWords, check_changes
    from tests.test_kernel_provenance import SOLE_VOCAB, interp

    [checked] = check_changes(
        interp(flag(quote="hipoalergenic parfum zero")),
        words=UserWords(said),
        vocab=SOLE_VOCAB,
        pack=pack,
        locale="ro",
    )
    assert checked.provenance == "explicit"


def test_matched_stays_empty_so_the_negated_word_never_becomes_search_text():
    said = "vreau un sampon fara parfum"
    assert sole(flag(quote="fara parfum"), said).matched == ()


def test_an_unknown_locale_has_no_label_so_the_flag_is_implicit():
    said = "vreau un sampon fara parfum"
    assert sole(flag(quote="fara parfum"), said, locale="xx").provenance == "implicit"


# --- lanțul: stare + plan ------------------------------------------------------------------------


def _find(query: str, *changes: StateChange) -> TurnInterpretation:
    return TurnInterpretation(
        thread="continue",
        acts=[Act(kind="find", targets=[], query=query)],
        changes=list(changes),
        references=[],
        ambiguities=[],
        corrects_previous_turn=False,
    )


@pytest.mark.parametrize(
    "quote",
    ["fara parfum", "sa nu contina parfum"],
    ids=["explicit", "implicit"],
)
def test_the_chain_keeps_the_flag_in_state_and_discloses_the_gap(quote):
    said = f"vreau un sampon {quote}"
    step = fc.kernel_step("sole-ro", ConversationStateV2(), _find(said, flag(quote=quote)), said)
    needs = {n.key: n.normalized_value for n in step.gate_state.active_needs()}
    assert needs.get(FLAG) is True
    assert "unmapped" not in needs
    plan = step.planned.plans[step.planned.primary]
    args = plan.search_args
    assert args is not None
    assert "true" not in (args.rank_terms or [])
    assert FLAG not in (args.prefer or {})
    assert "unsupported_need" in step.planned.gaps


def test_the_chain_never_puts_the_label_words_in_the_search_text():
    said = "vreau un sampon fara parfum"
    step = fc.kernel_step(
        "sole-ro", ConversationStateV2(), _find(said, flag(quote="fara parfum")), said
    )
    args = step.planned.plans[step.planned.primary].search_args
    assert args is not None and "parfum" not in (args.query or "").split()
