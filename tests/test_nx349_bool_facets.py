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
from tests.test_kernel_provenance import SOLE, SOLE_VOCAB, ch, sole

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


def test_a_negation_right_before_the_label_flips_the_state():
    """Recenzia (3): «nu fara parfum» spune `false`, deci e singurul fel explicit de a-l spune pe
    SOLE (fără aliasuri); cu `true` e o contrazicere."""
    said = "nu fara parfum, vreau cu parfum"
    checked = sole(flag("false", quote="nu fara parfum"), said)
    assert checked.rejected is None and checked.provenance == "explicit"
    assert checked.canonical_value == "false" and checked.matched == ("nu", "fara", "parfum")
    assert sole(flag(quote="nu fara parfum"), said).rejected == "semantic_mismatch"


def test_a_list_of_without_items_does_not_flip_the_next_one():
    """Recenzia (3): «fara» e și marcator de negație, dar e primul cuvânt al etichetei: o
    enumerare «fara alcool fara parfum» nu întoarce starea."""
    said = "un sampon fara alcool fara parfum"
    checked = sole(flag(quote="fara alcool fara parfum"), said)
    assert checked.rejected is None and checked.provenance == "explicit"


@pytest.mark.parametrize("relation", ["avoid", "lte", "gte"])
def test_a_flag_carries_its_own_polarity(relation):
    said = "vreau un sampon fara parfum"
    checked = sole(flag(quote="fara parfum", relation=relation), said)
    assert checked.rejected == "polarity_conflict"


@pytest.mark.parametrize("value", ["TRUE", " true "])
def test_the_flag_value_is_folded(value):
    said = "vreau un sampon fara parfum"
    assert sole(flag(value, quote="fara parfum"), said).canonical_value == "true"


def test_a_value_that_is_not_a_state_is_rejected_not_moved_to_unmapped():
    """Recenzia (2): pe `unmapped`, «fara parfum» ar fi devenit termen de ordonare și text de
    căutare (adică exact produsele parfumate). O valoare care nu e o stare se respinge."""
    said = "un sampon cu parfum discret"
    checked = sole(flag("discret", quote="parfum discret"), said)
    assert checked.dimension == FLAG and checked.rejected == "semantic_mismatch"
    assert sole(flag("da", quote="parfum discret"), said).rejected == "semantic_mismatch"


def test_a_pack_phrase_written_as_the_value_is_its_state():
    said = "vreau un sampon fara parfum"
    checked = sole(flag("fara parfum", quote="fara parfum"), said)
    assert checked.dimension == FLAG and checked.canonical_value == "true"
    assert checked.provenance == "explicit"


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


def test_matched_is_the_phrase_the_planner_keeps_out_of_the_search_text():
    said = "vreau un sampon fara parfum"
    assert sole(flag(quote="sampon fara parfum"), said).matched == ("fara", "parfum")


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
    step = _step(said, flag(quote=quote))
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
    args = _args(_step(said, flag(quote="fara parfum")))
    assert "parfum" not in (args.query or "").split()


def _step(said: str, *changes: StateChange, state=None, **kw):
    """Un tur prin kernel pe VOCABULARUL SOLE (recenzia: pe vocabularul gol al fixture-ului
    `_canonical` păstra dimensiunea, deci testul trecea și pe codul vechi)."""
    return fc.kernel_step(
        "sole-ro",
        state or ConversationStateV2(),
        _find(said, *changes),
        said,
        vocab=SOLE_VOCAB,
        **kw,
    )


def _args(step):
    args = step.planned.plans[step.planned.primary].search_args
    assert args is not None
    return args


def test_an_inferred_flag_is_a_gap_not_a_preference():
    """Recenzia (1): un fanion nespus devenea `prefer={fragrance_free: ['true']}`, iar
    `preference_level` compară „true" cu `str(True)`: exact produsele fără parfum ieșeau pe 0."""
    step = _step("vreau un sampon", flag(quote="fara parfum"))
    args = _args(step)
    assert FLAG not in (args.prefer or {}) and "true" not in (args.rank_terms or [])
    assert "unsupported_need" in step.planned.gaps


def test_the_subject_word_stays_in_the_fallback_text():
    """Recenzia (5): din citatul fanionului ies doar cuvintele frazei, nu și «sampon»."""
    args = _args(_step("vreau un sampon fara parfum", flag(quote="sampon fara parfum")))
    words = (args.query or "").split()
    assert "sampon" in words and "parfum" not in words


def test_a_rejected_flag_keeps_its_words_out_of_the_fallback_text():
    said = "vreau un sampon fara parfum"
    args = _args(_step(said, flag("false", quote="fara parfum")))
    assert "parfum" not in (args.query or "").split()


def test_a_description_only_flag_leaves_no_stopword_only_text():
    """Citatul descriptiv iese întreg din rezervă; dacă rămân doar cuvinte goale, nu e o căutare."""
    step = _step("sa nu contina parfum", flag(quote="sa nu contina parfum"))
    for plan in step.planned.plans:
        # fără subiect și fără text, turul nu caută nimic (poarta întreabă); oricum, nu «parfum»
        text = plan.search_args.query if plan.search_args is not None else None
        assert text is None or "parfum" not in text.split()


def test_replace_on_a_stored_flag_is_the_new_state():
    """Recenzia (4): `replace c1 false` ca `supersede` dintr-o sursă `implicit` era respins tăcut
    (`hard_downgrade`), deși același lucru ca `set` trecea."""
    first = _step("vreau un sampon fara parfum", flag(quote="fara parfum"))
    assert {n.key: n.normalized_value for n in first.state_after.active_needs()}[FLAG] is True
    said = "de fapt vreau cu parfum"
    replace = ch("replace", target="c1", dimension=FLAG, relation="eq", value="false", quote=said)
    second = _step(said, replace, state=first.state_after, turn_id="t2")
    assert {n.key: n.normalized_value for n in second.state_after.active_needs()}[FLAG] is False
