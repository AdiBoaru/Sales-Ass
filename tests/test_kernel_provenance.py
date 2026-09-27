"""NX-330 (kernel v1.0, pasul 3a) — proveniența calculată de cod.

Tabelul contractului („Provenance and strength”) e reprodus rând cu rând, plus validările care
resping (I9 unitățile, I22 referințele, handle-urile, conflictele) și tăria (I7, I8). Zero model,
zero DB: vocabularul SOLE e construit explicit (pachetul de fixture `sole-ro` n-are produse), iar
celelalte pachete își derivă vocabularul din produsele de fixture."""

from __future__ import annotations

import pytest

from src.catalog.vocabulary import CatalogVocabulary, VocabEntry
from src.conversation.interpretation import Act, Reference, StateChange, TurnInterpretation
from src.conversation.provenance import (
    Handle,
    UserWords,
    check_changes,
    check_targets,
    hard_capable,
    need_handles,
    tenant_dimensions,
)
from src.conversation.state_v2 import Need
from tests.kernel import fixture_catalog as fc

SOLE = fc.pack("sole-ro")
SOLE_VOCAB = CatalogVocabulary(
    business_id="b",
    dimensions={
        "concerns": (
            VocabEntry("redness", "redness", 40),
            VocabEntry("hydration", "hydration", 90),
        ),
        "skin_type": (VocabEntry("dry", "dry", 400), VocabEntry("oily", "oily", 300)),
        "texture": (VocabEntry("greasy", "gras", 30), VocabEntry("light", "usor", 50)),
    },
)


def ch(op: str = "add", **fields) -> StateChange:
    base = {
        "target": None,
        "dimension": None,
        "relation": None,
        "value": None,
        "number": None,
        "unit": None,
        "relative_to": None,
        "quote": "",
    }
    return StateChange(op=op, **{**base, **fields})


def interp(*changes: StateChange, refs=(), acts=(), thread="continue") -> TurnInterpretation:
    return TurnInterpretation(
        thread=thread,
        acts=list(acts),
        changes=list(changes),
        references=list(refs),
        ambiguities=[],
        corrects_previous_turn=False,
    )


def ref(rid: str) -> Reference:
    return Reference(
        id=rid,
        text=rid,
        kind="deictic",
        ordinal=None,
        name=None,
        dimension=None,
        value=None,
        direction=None,
    )


def sole(change: StateChange, said: str, *, earlier=(), locale="ro", vocab=SOLE_VOCAB):
    [checked] = check_changes(
        interp(change),
        words=UserWords(said, tuple(earlier)),
        vocab=vocab,
        pack=SOLE,
        locale=locale,
    )
    return checked


# --- tabelul contractului, rând cu rând ----------------------------------------------------------


def test_contract_row_1_an_alias_of_the_value_is_explicit():
    """„Ai ceva care să reducă roșeața?” → `add concerns=redness`: „roșeața” e alias al valorii."""
    c = sole(
        ch(dimension="concerns", value="redness", quote="roșeața"),
        "Ai ceva care să reducă roșeața?",
    )
    assert (c.provenance, c.strength, c.rejected) == ("explicit", "soft", None)
    assert c.canonical_value == "redness"


def test_contract_row_2_a_quote_that_does_not_resolve_is_implicit():
    """„Pielea mea se usucă după duș” → `add skin_type=dry`: citatul e al clientului, nerezolvat."""
    c = sole(
        ch(dimension="skin_type", value="dry", quote="se usucă după duș"),
        "Pielea mea se usucă după duș",
    )
    assert (c.provenance, c.strength, c.rejected) == ("implicit", "soft", None)


def test_contract_row_3_avoid_with_a_negation_is_explicit():
    """„Să nu fie foarte gras” → `add texture avoid greasy`: alias + negația „nu”."""
    c = sole(
        ch(dimension="texture", relation="avoid", value="greasy", quote="să nu fie foarte gras"),
        "Să nu fie foarte gras",
    )
    assert (c.provenance, c.rejected) == ("explicit", None)


def test_contract_row_4_avoid_without_a_negation_is_implicit():
    c = sole(
        ch(dimension="texture", relation="avoid", value="greasy", quote="gras"),
        "cam gras",
    )
    assert c.provenance == "implicit"


def test_contract_row_5_a_unit_of_the_dimension_and_a_comparator_is_explicit():
    """„Minim 256 GB” → `add storage gte 256 GB`: unitatea e a lui storage, comparatorul „minim”."""
    pack = fc.pack("electronics")
    [c] = check_changes(
        interp(
            ch(dimension="storage", relation="gte", number=256, unit="GB", quote="Minim 256 GB")
        ),
        words=UserWords("Minim 256 GB"),
        vocab=fc.vocabulary("electronics"),
        pack=pack,
        locale="ro",
    )
    assert (c.provenance, c.canonical_value, c.rejected) == ("explicit", 256.0, None)
    assert c.strength == "soft", "storage nu e enforce_ready: explicit, dar nu dur"


def test_contract_row_6_a_word_with_no_facet_stays_unmapped_and_implicit():
    """„Bun pentru gaming”, fără o asemenea fațetă → `unmapped`, `implicit`, niciodată dur."""
    pack = fc.pack("electronics")
    [c] = check_changes(
        interp(ch(dimension="unmapped", value="gaming", quote="bun pentru gaming")),
        words=UserWords("Bun pentru gaming"),
        vocab=fc.vocabulary("electronics"),
        pack=pack,
        locale="ro",
    )
    assert (c.dimension, c.provenance, c.strength) == ("unmapped", "implicit", "soft")


def test_contract_row_7_a_quote_only_the_bot_wrote_is_inferred():
    """Botul a spus „roșeață”, clientul nu: `inferred`, deci doar ordonare, doar turul ăsta."""
    c = sole(
        ch(dimension="concerns", value="redness", quote="roșeață"),
        "sincer nu știu, tu ce recomanzi?",
    )
    assert (c.provenance, c.strength) == ("inferred", "ranking")


# --- localizarea citatului ----------------------------------------------------------------------


def test_the_quote_is_found_on_whole_words_not_on_a_prefix():
    """«mat» nu e în «matreata» (`corroborated_by` le lega pe prefix)."""
    c = sole(ch(dimension="concerns", value="redness", quote="mat"), "am mătreață")
    assert c.provenance == "inferred"


def test_the_quote_can_come_from_an_earlier_user_turn():
    c = sole(
        ch(dimension="concerns", value="redness", quote="roșeață"),
        "și ce preț are?",
        earlier=("am multă roșeață pe obraji",),
    )
    assert c.provenance == "explicit"


# --- semantic_mismatch, polaritate ---------------------------------------------------------------


def test_a_quote_that_names_another_value_is_a_semantic_mismatch():
    c = sole(ch(dimension="skin_type", value="dry", quote="roșeața"), "am roșeața pe obraz")
    assert c.rejected == "semantic_mismatch"


def test_a_negation_next_to_an_eq_value_is_a_polarity_conflict():
    c = sole(
        ch(dimension="concerns", relation="contains", value="redness", quote="să nu am roșeață"),
        "vreau ceva ca să nu am roșeață",
    )
    assert c.rejected == "polarity_conflict"


def test_the_nu_inside_a_comparator_is_not_a_negation():
    """„nu mai mult de 100 lei” e un comparator, nu o negație a valorii."""
    c = sole(
        ch(
            dimension="price",
            relation="lte",
            number=100,
            unit="lei",
            quote="nu mai mult de 100 lei",
        ),
        "nu mai mult de 100 lei",
    )
    assert (c.provenance, c.strength, c.rejected) == ("explicit", "hard", None)


def test_a_bound_without_a_comparator_goes_down_to_implicit():
    c = sole(
        ch(dimension="price", relation="gte", number=100, unit="lei", quote="100 lei"),
        "100 lei",
    )
    assert c.provenance == "implicit"


def test_a_bare_price_number_is_an_upper_bound():
    c = sole(ch(dimension="price", relation="lte", number=150, quote="150"), "am 150")
    assert (c.provenance, c.strength) == ("explicit", "hard")


def test_an_unknown_locale_turns_every_avoid_implicit():
    c = sole(
        ch(dimension="texture", relation="avoid", value="greasy", quote="să nu fie gras"),
        "să nu fie gras",
        locale="xx",
    )
    assert c.provenance == "implicit"


# --- I9: unitatea aparține dimensiunii ----------------------------------------------------------


@pytest.mark.parametrize(
    ("pack_name", "dimension", "number", "unit", "ok"),
    [
        ("electronics", "storage", 256, "GB", True),
        ("electronics", "price", 256, "GB", False),
        ("electronics", "storage", 1, "TB", True),
        ("furniture", "seats", 3, "locuri", True),
        ("furniture", "price", 3, "locuri", False),
        ("furniture", "width", 2, "m", True),
        ("sole-ro", "price", 100, "ml", False),
        ("sole-ro", "price", 50, "spf", False),
        ("sole-ro", "price", 100, "lei", True),
        ("sole-ro", "price", 100, None, True),
        ("electronics", "storage", 256, None, False),
        ("gifts", "price", 200, "RON", True),
    ],
)
def test_i9_a_number_belongs_to_a_dimension_only_through_its_unit(
    pack_name, dimension, number, unit, ok
):
    pack = fc.pack(pack_name)
    said = f"{number} {unit or ''}".strip()
    [c] = check_changes(
        interp(ch(dimension=dimension, relation="lte", number=number, unit=unit, quote=said)),
        words=UserWords(said),
        pack=pack,
        locale="ro",
    )
    assert (c.rejected is None) is ok, c
    if ok:
        assert isinstance(c.canonical_value, float)


def test_i9_the_number_is_converted_to_the_canonical_unit():
    [c] = check_changes(
        interp(ch(dimension="storage", relation="gte", number=1, unit="TB", quote="minim 1 TB")),
        words=UserWords("minim 1 TB"),
        pack=fc.pack("electronics"),
        locale="ro",
    )
    assert c.canonical_value == 1024.0


# --- handle-uri, referințe, dimensiuni, plafoane ------------------------------------------------


def _needs() -> tuple[Need, ...]:
    return (
        Need(
            key="concerns", operator="contains", normalized_value="redness", source="user_explicit"
        ),
        Need(key="budget_max", operator="lte", normalized_value=100.0, source="user_explicit"),
        Need(key="old", operator="eq", normalized_value="x", status="revoked"),
    )


def test_handles_are_the_active_needs_in_a_stable_order():
    handles = need_handles(_needs())
    assert [(h.handle, h.key) for h in handles] == [("c1", "budget_max"), ("c2", "concerns")]
    assert handles[0].dimension == "price"
    assert need_handles(tuple(reversed(_needs()))) == handles


def test_remove_and_replace_need_a_known_handle():
    handles = need_handles(_needs())
    ok, unknown, wrong_dim = check_changes(
        interp(
            ch(op="remove", target="c2", quote="nu mai contează roșeața"),
            ch(op="remove", target="c9", quote="nu mai contează"),
            ch(op="replace", target="c1", dimension="concerns", value="hydration", quote="x"),
        ),
        words=UserWords("nu mai contează roșeața"),
        handles=handles,
        vocab=SOLE_VOCAB,
        pack=SOLE,
        locale="ro",
    )
    assert (ok.rejected, ok.provenance, ok.dimension) == (None, "explicit", "concerns")
    assert unknown.rejected == "unknown_handle"
    assert wrong_dim.rejected == "unknown_handle"


def test_an_unknown_dimension_is_rejected():
    [c] = check_changes(
        interp(ch(dimension="warranty", value="3 ani", quote="3 ani")),
        words=UserWords("3 ani"),
        pack=fc.pack("electronics"),
        locale="ro",
    )
    assert c.rejected == "unknown_dimension"


def test_i22_undeclared_references_are_reported():
    i = interp(
        ch(dimension="price", relation="lte", relative_to="r7", quote="mai ieftin decât ăsta"),
        refs=[ref("r1")],
        acts=[Act(kind="link", targets=["r1", "r2"], query=None)],
    )
    assert check_targets(i) == ["r2", "r7"]
    [c] = check_changes(i, words=UserWords("mai ieftin decât ăsta"), pack=SOLE, locale="ro")
    assert c.rejected == "unknown_reference"


def test_a_relative_change_is_explicit_with_a_relative_comparator():
    i = interp(
        ch(dimension="price", relation="lte", relative_to="r1", quote="mai ieftin decât ăsta"),
        refs=[ref("r1")],
    )
    [c] = check_changes(i, words=UserWords("ceva mai ieftin decât ăsta?"), pack=SOLE, locale="ro")
    assert (c.provenance, c.strength, c.canonical_value) == ("explicit", "hard", None)


def test_crossing_bounds_in_the_same_turn_is_a_hard_conflict():
    said = "sub 100 lei, minim 150 lei"
    low, high = check_changes(
        interp(
            ch(dimension="price", relation="lte", number=100, unit="lei", quote="sub 100 lei"),
            ch(dimension="price", relation="gte", number=150, unit="lei", quote="minim 150 lei"),
        ),
        words=UserWords(said),
        pack=SOLE,
        locale="ro",
    )
    assert low.rejected == high.rejected == "hard_conflict"


def test_changes_beyond_the_runtime_cap_are_truncated():
    changes = [ch(dimension="concerns", value="redness", quote="roșeață") for _ in range(12)]
    checked = check_changes(
        interp(*changes), words=UserWords("roșeață"), vocab=SOLE_VOCAB, pack=SOLE, locale="ro"
    )
    assert [c.rejected for c in checked].count("truncated") == 2
    assert all(c.rejected is None for c in checked[:10])


# --- I8: hard-capable ----------------------------------------------------------------------------


def test_i8_hard_capable_is_price_an_enforce_ready_facet_or_a_hard_universal_key():
    assert hard_capable("price", SOLE)
    assert not hard_capable("concerns", SOLE)
    assert not hard_capable("color", fc.pack("electronics"))
    assert hard_capable("size", fc.pack("fashion")), "mărimea e o cheie universală dură"

    class _Facet:
        key, enforce_ready = "skin_type", True

    class _Pack:
        facets = (_Facet(),)

    assert hard_capable("skin_type", _Pack())


def test_the_tenant_dimensions_are_the_facets_plus_the_universals():
    dims = tenant_dimensions(fc.pack("electronics"))
    assert {"storage", "color", "brand", "category", "price", "unmapped"} <= dims
    assert tenant_dimensions(None) == {"category", "price", "unmapped"}


# --- fără vocabular ------------------------------------------------------------------------------


def test_without_a_vocabulary_a_value_can_never_be_explicit():
    c = sole(
        ch(dimension="concerns", value="redness", quote="roșeața"),
        "Ai ceva pentru roșeața?",
        vocab=None,
    )
    assert c.provenance == "implicit"


def test_a_handle_carries_what_the_correction_rule_needs():
    h = Handle("c1", "concerns", "redness", "user_explicit", "t-7", 4)
    assert (h.written_turn, h.revision, h.dimension) == ("t-7", 4, "concerns")
