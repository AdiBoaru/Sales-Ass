"""NX-317 — comparația: axe din date, fără axe egale pe sursă, verdictul o singură dată.

Turul real «Compara SOME BY MI Yuja Niacin cu By Wishtrend Vitamin» (`sole-ro`, 2026-09-23): cinci
axe, dintre care una („Cum se simte pe ten?") aproape identică pe ambele coloane, verdictul în lead,
în subtitlu și în închidere, iar «hidratare», nevoia clientului, lipsă din celula By Wishtrend deși
era în `concerns`. Fișele și payload-ul modelului de mai jos sunt cele reale, prescurtate.

Pure (fișa, regula pe sursă, nevoile, deduplicarea, chips) + cap-coadă pe `compose_comparison` cu un
model FALS care întoarce payload-ul observat. ZERO OpenAI, zero DB."""

from __future__ import annotations

from dataclasses import replace

import pytest

from src.agent import compare_narrative as cn
from src.agent import prompt_builder
from src.agent.fallbacks import _compare_chips
from src.config import get_settings
from src.domain.pack import FacetSpec
from src.models import (
    BusinessConfig,
    Comparison,
    ComparisonColumn,
    Contact,
    InboundMessage,
    TurnContext,
)
from src.worker import compose

FACETS = (
    FacetSpec(key="routine_time", labels={"ro": "Când"}),
    FacetSpec(key="skin_type", labels={"ro": "Tip de ten"}, value_labels={
        "sensitive": {"ro": "ten sensibil"}}),
    FacetSpec(key="concerns", labels={"ro": "Preocupări"}, value_labels={
        "hydration": {"ro": "hidratare"}, "dullness": {"ro": "ten tern"},
        "wrinkles": {"ro": "riduri si fermitate"}}),
)  # fmt: skip

A = {
    "id": "a",
    "name": "SOME BY MI Yuja Niacin Anti-Blemish Cream - 60 gr",
    "price": 110.0,
    "rating": 4.7,
    "availability": "in_stock",
    "attributes": {
        "routine_time": "dimineata si seara",
        "skin_type": ["sensitive"],
        "concerns": ["dullness", "hydration"],
        "product_type": "crema de fata",
    },
    "top_pros": ["se absoarbe rapid", "are o textură ușoară și plăcută"],
    "review_summary": "Multe recenzii spun că se absoarbe rapid și are o textură ușoară.",
}
B = {
    "id": "b",
    "name": "By Wishtrend Vitamin A-mazing Bakuchiol Night Cream - 30 ml",
    "price": 200.0,
    "rating": 4.6,
    "availability": "in_stock",
    "attributes": {
        "routine_time": "seara",
        "skin_type": ["sensitive"],
        "concerns": ["wrinkles", "hydration"],
        "product_type": "crema de noapte",
    },
    "top_pros": ["se absoarbe rapid", "dă rezultate vizibile"],
    "review_summary": "Multe recenzii spun că dă rezultate vizibile.",
}


@pytest.fixture
def v2(monkeypatch):
    monkeypatch.setattr(get_settings(), "comparison_axes_v2_enabled", True, raising=False)


@pytest.fixture
def v1(monkeypatch):
    monkeypatch.setattr(get_settings(), "comparison_axes_v2_enabled", False, raising=False)


def _comparison() -> Comparison:
    return Comparison(
        columns=[
            ComparisonColumn(product_id="a", name="SOME BY MI Yuja Niacin", price=110.0),
            ComparisonColumn(product_id="b", name="By Wishtrend Vitamin", price=200.0),
        ],
        rows=[],
        intro="Uite-le față în față.",
    )


# --- 1. fișa de fapte ----------------------------------------------------------------------


def test_flag_off_sheets_are_todays_sheets(v1):
    sheets = compose.comparison_sheets([A, B], FACETS, "ro")
    assert sheets == {
        "a": compose.product_fact_sheet(A, FACETS, "ro"),
        "b": compose.product_fact_sheet(B, FACETS, "ro"),
    }
    assert not set(compose.COMPARISON_V2_SOURCES) & set(sheets["a"])


def test_reviews_size_and_type_become_sources(v2):
    sheets = compose.comparison_sheets([A, B], FACETS, "ro")
    assert sheets["a"]["recenzii"].startswith("Multe recenzii")
    assert (sheets["a"]["cantitate"], sheets["b"]["cantitate"]) == ("60 gr", "30 ml")
    assert (sheets["a"]["tip_produs"], sheets["b"]["tip_produs"]) == (
        "crema de fata",
        "crema de noapte",
    )


def test_size_known_on_one_column_only_is_not_a_source(v2):
    no_size = {**B, "name": "By Wishtrend Vitamin A-mazing Bakuchiol Night Cream"}
    sheets = compose.comparison_sheets([A, no_size], FACETS, "ro")
    assert "cantitate" not in sheets["a"] and "cantitate" not in sheets["b"]


def test_same_product_type_is_common_ground_not_an_axis(v2):
    same = {**B, "attributes": {**B["attributes"], "product_type": "crema de fata"}}
    sheets = compose.comparison_sheets([A, same], FACETS, "ro")
    assert "tip_produs" not in sheets["a"]


def test_new_sources_can_be_cited_and_their_digits_are_allowed(v2):
    ctx = _ctx()
    allowed = cn._allowed_numbers(ctx, _comparison(), [A, B], FACETS)
    assert {"60", "30"} <= allowed  # gramajul din fișă e o cifră citabilă


# --- 2. discriminarea pe sursă -------------------------------------------------------------


def _axis(label, source_a, text_a, source_b, text_b):
    return {
        "label": label,
        "cells": [
            {"product_id": "a", "source": source_a, "text": text_a},
            {"product_id": "b", "source": source_b, "text": text_b},
        ],
    }


def _assemble(axes):
    return cn.assemble_axes({"axes": axes}, _comparison(), [A, B], set(), FACETS, "ro")


def test_paraphrases_of_the_same_source_value_are_dropped(v2):
    """Aceeași valoare («ten sensibil» pe ambele), formulată diferit: nu e o diferență."""
    rows, rejected = _assemble(
        [
            _axis(
                "Pentru ce ten?",
                "skin_type",
                "Merge pe ten sensibil.",
                "skin_type",
                "E blândă cu pielea sensibilă, fără iritații.",
            )
        ]
    )
    assert rows == [] and rejected == {"axis_same_source": 1}


def test_different_source_values_stay(v2):
    rows, _ = _assemble(
        [
            _axis(
                "Când o folosești?",
                "routine_time",
                "Dimineața și seara.",
                "routine_time",
                "Doar seara.",
            )
        ]
    )
    assert [r.label for r in rows] == ["Când o folosești?"]


def test_flag_off_judges_prose_only(v1):
    rows, rejected = _assemble(
        [
            _axis(
                "Pentru ce ten?",
                "skin_type",
                "Merge pe ten sensibil.",
                "skin_type",
                "E blândă cu pielea sensibilă.",
            )
        ]
    )
    assert len(rows) == 1 and "axis_same_source" not in rejected


def test_different_sources_are_not_judged_as_same(v2):
    rows, _ = _assemble(
        [
            _axis(
                "Ce spun clienții?",
                "recenzii",
                "Se absoarbe rapid.",
                "avantaje",
                "Dă rezultate vizibile.",
            )
        ]
    )
    assert len(rows) == 1


# --- 3. nevoile clientului -----------------------------------------------------------------


def test_uncovered_need_is_reported_when_the_data_had_it():
    """Turul real: «hidratare» e în `concerns` la amândouă, celula By Wishtrend n-o spune."""
    real = [
        ("a", "concerns", "Ajută tenul tern, oferind hidratare și calmare."),
        ("b", "concerns", "Vizează ridurile și fermitatea."),
    ]
    assert cn.need_coverage([("concerns", "hydration")], real, [A, B], FACETS, "ro") == (
        "concerns",
    )
    both = [*real[:1], ("b", "concerns", "Vizează ridurile, fermitatea și hidratarea.")]
    assert cn.need_coverage([("concerns", "hydration")], both, [A, B], FACETS, "ro") == ()


def test_inflected_mention_counts_as_covered():
    cited = [
        ("a", "concerns", "Hidratarea e punctul ei forte."),
        ("b", "concerns", "Hidratează și vizează ridurile."),
    ]
    assert cn.need_coverage([("concerns", "hydration")], cited, [A, B], FACETS, "ro") == ()


def test_need_the_data_does_not_have_is_not_reported():
    assert cn.need_coverage([("concerns", "acne")], [], [A, B], FACETS, "ro") == ()


def test_needs_come_from_the_subject_and_the_stack():
    ctx = _ctx()
    ctx.state.search_constraints = {
        "subject": {"type": "crema de fata", "needs": [["skin_type", "sensitive"]]},
        "concerns": ["hydration"],
    }
    assert cn.comparison_needs(ctx) == (("skin_type", "sensitive"), ("concerns", "hydration"))


# --- 4. verdictul o singură dată -----------------------------------------------------------


REAL_LEAD = (
    "SOME BY MI este orientată spre luminozitate, calmare și folosire de două ori pe zi. "
    "By Wishtrend se concentrează pe fermitate și îngrijirea de seară, cu retinal și bakuchiol."
)
REAL_SUBTITLE = (
    "SOME BY MI este o cremă ușoară pentru ten tern și sensibil, iar By Wishtrend este o cremă "
    "de noapte pentru fermitate."
)
REAL_CLOSING = [
    "Alege mai întâi obiectivul principal: luminozitate și calmare pentru o rutină flexibilă, sau "
    "fermitate într-o rutină de seară.",
    "Dacă vrei o cremă pentru dimineața și seara, alege SOME BY MI. Dacă urmărești în special "
    "ridurile și fermitatea, By Wishtrend se potrivește mai bine.",
]


def test_near_literal_repeat_is_removed_from_the_closing():
    lead = "Alegerea ține de fermitate sau luminozitate."
    closing = ["Alegerea ține de fermitate sau de luminozitate. Pentru seară, By Wishtrend."]
    new_lead, _, new_closing, dropped = cn.dedupe_verdict(lead, None, closing, "ro")
    assert new_lead == lead
    assert new_closing == ["Pentru seară, By Wishtrend."] and dropped == ["closing"]


def test_the_real_turn_is_not_touched_by_the_lexical_gate():
    """Declarat: pe turul real repetiția e de SENS (0,15-0,40), nu de cuvinte. Poarta nu scoate
    nimic de acolo; structura promptului v2 e reparația, iar testul pinuie limita."""
    lead, subtitle, closing, dropped = cn.dedupe_verdict(
        REAL_LEAD, REAL_SUBTITLE, REAL_CLOSING, "ro"
    )
    assert dropped == [] and closing == REAL_CLOSING and subtitle == REAL_SUBTITLE


def test_a_closing_made_only_of_repeats_disappears_and_the_lead_stays():
    lead = "Luminozitate pentru SOME BY MI, fermitate pentru By Wishtrend."
    new_lead, _, closing, dropped = cn.dedupe_verdict(lead, None, [lead], "ro")
    assert new_lead == lead and closing == [] and dropped == ["closing"]


# --- 5. cap-coadă pe compose_comparison ------------------------------------------------------


def _ctx() -> TurnContext:
    return TurnContext(
        turn_id="t",
        business=BusinessConfig(id="b", slug="d", name="D", vertical="ecommerce"),
        contact=Contact(id="c", business_id="b"),
        message=InboundMessage(
            provider_msg_id="m", body="Compara SOME BY MI Yuja Niacin cu By Wishtrend Vitamin"
        ),
        conversation_id="conv",
    )


REAL_PAYLOAD = {
    "lead": REAL_LEAD,
    "subtitle": REAL_SUBTITLE,
    "axes": [
        _axis("Când o incluzi în rutină?", "routine_time", "Dimineața și seara, pentru o rutină "
              "simplă.", "routine_time", "Seara, ca pas de îngrijire de noapte."),
        _axis("Ce preocupări adresează?", "concerns", "Ajută tenul tern, oferind hidratare.",
              "concerns", "Vizează ridurile și fermitatea."),
        _axis("Pentru ce tip de ten?", "skin_type", "Potrivită pentru ten sensibil.", "skin_type",
              "Blândă cu tenul sensibil."),
    ],
    "closing": REAL_CLOSING,
}  # fmt: skip


class _FakeLLM:
    def __init__(self, payload):
        self.payload = payload
        self.calls: list[tuple[str, str]] = []

    async def complete_schema(self, system, user, schema):
        self.calls.append((system, user))
        return self.payload


async def test_real_turn_with_v2(v2):
    ctx = _ctx()
    ctx.state.search_constraints = {"concerns": ["hydration"]}
    llm = _FakeLLM(REAL_PAYLOAD)
    out = await cn.compose_comparison(llm, ctx, _comparison(), [A, B], facets=FACETS)

    system, user = llm.calls[0]
    assert "UN singur paragraf" in system and "NU spui" in system
    assert "hidratare (sursa `concerns`)" in user
    assert "recenzii:" in user and "cantitate: 60 gr" in user
    # axa „Pentru ce tip de ten?" citează aceeași valoare pe amândouă ⇒ pică
    assert "Pentru ce tip de ten?" not in [r.label for r in out.rows]
    # verdictul: un singur paragraf de închidere
    assert out.closing == [REAL_CLOSING[0]]
    events = {e.type: e.properties for e in ctx.events}
    assert events["comparison_axes"]["dropped_same_source"] == 1
    assert events["comparison_need_uncovered"]["need_dimension"] == "concerns"


async def test_flag_off_prompt_and_output_are_todays(v1):
    ctx = _ctx()
    ctx.state.search_constraints = {"concerns": ["hydration"]}
    llm = _FakeLLM(REAL_PAYLOAD)
    out = await cn.compose_comparison(llm, ctx, _comparison(), [A, B], facets=FACETS)
    system, user = llm.calls[0]
    assert system == prompt_builder.build_compare_system(cn._prompt_inputs(ctx))
    assert "Ce a spus clientul" not in user and "recenzii:" not in user
    assert "Pentru ce tip de ten?" in [r.label for r in out.rows]
    assert out.closing == REAL_CLOSING
    assert not {"comparison_axes", "comparison_need_uncovered", "comparison_dedup"} & {
        e.type for e in ctx.events
    }


def test_default_compare_system_is_unchanged():
    inp = cn._prompt_inputs(_ctx())
    assert prompt_builder.build_compare_system(inp) == prompt_builder.build_compare_system(
        inp, axes_v2=False
    )
    assert prompt_builder._COMPARE_RULES in prompt_builder.build_compare_system(inp)


def test_v2_prompt_keeps_the_hard_rules_and_the_voice():
    rules = prompt_builder._COMPARE_RULES_V2
    assert rules.startswith(prompt_builder._COMPARE_RULES.split("`lead` = 1-2 fraze.")[0])
    assert " — " not in rules and " – " not in rules
    assert ";" not in rules.split("REGULI DURE")[1].split("`lead`")[1]


# --- 6. chips-urile de după comparație ------------------------------------------------------


LONG = [
    ComparisonColumn(
        product_id="a", name="Bakuchiol Cream Retinal Alternative Night Care", price=1
    ),
    ComparisonColumn(product_id="b", name="Bakuchiol Cream Hydrating Day Formula Extra", price=1),
]


def test_compare_chips_cut_on_whole_words_down_to_the_unique_prefix(v2):
    chips = _compare_chips(LONG, "ro")
    assert all("…" not in c and len(c) <= 56 for c in chips)
    assert chips[0].startswith("Adaugă Bakuchiol Cream Retinal") and chips[0].endswith("în coș")
    assert chips[1].startswith("Adaugă Bakuchiol Cream Hydrating")
    assert chips[-1] == "Vreau ceva mai ieftin decât astea"


def test_compare_chips_flag_off_are_todays(v1):
    chips = _compare_chips(LONG, "ro")
    assert any("…" in c for c in chips)


def test_a_name_that_cannot_fit_whole_is_not_offered(v2):
    huge = [replace(LONG[0], name="Supercalifragilisticexpialidocious " * 3), LONG[1]]
    chips = _compare_chips(huge, "ro")
    assert not any(c.startswith("Adaugă Supercali") for c in chips)
    assert chips[-1] == "Vreau ceva mai ieftin decât astea"
