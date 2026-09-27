"""NX-332 (kernel v1.0, pasul 4a) — poarta de ambiguitate: tabelul de reguli, anti-bucla,
degradarea.

Contractul: o mutație fără țintă `exact` întreabă (I10), cel mult o întrebare pe tur și aceeași
cheie de cel mult `max_attempts_per_key` ori (I11), iar întrebarea e scrisă DOAR din șabloanele
pachetului.
Suita e tabelară pe cele patru pachete de fixture (alte verticale decât SOLE, ca un literal scurs în
poartă să pice aici), fiecare tur trecând prin validatorul de proveniență, resolverul, `to_delta` și
reducerul REALE (`fixture_catalog.kernel_step`). Proprietățile rulează pe cinci pachete (+ SOLE).
Zero model, zero DB."""

from __future__ import annotations

from dataclasses import replace

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from src.catalog.vocabulary import CATEGORY_DIMENSION, CatalogVocabulary, VocabEntry
from src.conversation.ambiguity_gate import (
    GATE_REASONS,
    GateOutcome,
    decide_ambiguity,
    target_question_key,
)
from src.conversation.clarification_policy import ClarificationPolicy
from src.conversation.interpretation import (
    Act,
    CheckedChange,
    ResolvedRef,
    StateChange,
    TurnInterpretation,
)
from src.conversation.references import ProductFacts, ReferenceFacts
from src.conversation.state_reducer import ReducerPolicy, StateUpdateProposal, reduce
from src.conversation.state_v2 import AskedQuestion, ConversationStateV2
from tests.kernel import fixture_catalog as fc
from tests.kernel import replay

PACKS = replay.FIXTURE_PACKS
ALL_PACKS = (*replay.FIXTURE_PACKS, "sole-ro")
POLICY = ClarificationPolicy()

#: Per pachet: ce e pe ecran în fiecare scenariu. Aceleași reguli, alte verticale.
SPEC: dict[str, dict] = {
    "electronics": {
        "three": ("el-01", "el-02", "el-03"),
        "pair": ("el-01", "el-07"),
        "pair_name": "Samsung",
        "five": ("el-01", "el-02", "el-03", "el-04", "el-05"),
        "five_name": "Phone",
        "sold_out": ("el-07", "el-08"),
        "same": ("color", "negru", "negri"),
        "differ": ("color", ("negru", "albastru"), ("el-01", "el-03", "el-05", "el-07")),
        "confirm": ("color", "negru", ("el-01", "el-02", "el-03", "el-04")),
    },
    "fashion": {
        "three": ("fa-01", "fa-02", "fa-03"),
        "pair": ("fa-01", "fa-09"),
        "pair_name": "Mara",
        "five": ("fa-01", "fa-02", "fa-03", "fa-04", "fa-05"),
        "five_name": "Rochie",
        "sold_out": ("fa-07", "fa-08"),
        "same": ("color", "roșu", "rosie"),
        "differ": ("color", ("negru", "albastru"), ("fa-02", "fa-04", "fa-11", "fa-13")),
        "confirm": ("color", "negru", ("fa-01", "fa-02", "fa-03", "fa-04")),
    },
    "furniture": {
        "three": ("fu-01", "fu-02", "fu-03"),
        "pair": ("fu-01", "fu-02"),
        "pair_name": "Oslo",
        "five": ("fu-01", "fu-02", "fu-03", "fu-04", "fu-05"),
        "five_name": "Oslo",
        "sold_out": ("fu-07", "fu-08"),
        "same": ("color", "bej", "bej deschis"),
        "differ": ("color", ("gri", "bej"), ("fu-01", "fu-02", "fu-04", "fu-05")),
        "confirm": ("color", "gri", ("fu-01", "fu-02", "fu-03", "fu-11")),
    },
    "gifts": {
        "three": ("gi-01", "gi-02", "gi-03"),
        "pair": ("gi-01", "gi-09"),
        "pair_name": "ceai",
        "five": ("gi-01", "gi-06", "gi-09", "gi-14", "gi-17"),
        "five_name": "Set",
        "sold_out": ("gi-07", "gi-08"),
        "same": ("recipient", "ea", "sora"),
        "differ": ("recipient", ("ea", "el"), ("gi-01", "gi-02", "gi-05", "gi-06")),
        "confirm": ("recipient", "ea", ("gi-01", "gi-02", "gi-03", "gi-04")),
    },
}


# --- utilitare -----------------------------------------------------------------------------------


def interp(**compact) -> TurnInterpretation:
    return TurnInterpretation.model_validate(replay.expand_interpretation(compact))


def screen(name: str, *ids: str, state: ConversationStateV2 | None = None) -> ConversationStateV2:
    """Ce e pe ecran ÎNAINTEA turului, pus prin reducer (ca la un executor real)."""
    catalog = fc.products(name)
    out = reduce(
        state or ConversationStateV2(),
        StateUpdateProposal(
            "set_references",
            source="catalog",
            payload={
                "displayed_products": [
                    {"product_id": p, "name": catalog[p]["name"], "price": catalog[p]["price"]}
                    for p in ids
                ]
            },
        ),
        ReducerPolicy(),
    )
    assert isinstance(out, ConversationStateV2)
    return out


def step(name: str, user: str, compact: dict, *, on=(), state=None, **kw) -> fc.KernelStep:
    base = screen(name, *on, state=state) if on else (state or ConversationStateV2())
    return fc.kernel_step(name, base, interp(**compact), user, **kw)


def verdict(outcome: GateOutcome) -> tuple[str, str]:
    return outcome.decision.verdict, outcome.decision.reason


def names(name: str, *ids: str) -> list[str]:
    return [fc.products(name)[pid]["name"] for pid in ids]


def find(query: str | None = None) -> dict:
    return {"kind": "find", **({"query": query} if query else {})}


def ref(rid: str, kind: str, **fields) -> dict:
    return {"id": rid, "text": fields.pop("text", rid), "kind": kind, **fields}


# --- regula 9 și țintele exacte ------------------------------------------------------------------


@pytest.mark.parametrize("name", PACKS)
def test_cart_on_an_exact_target_acts(name):
    s = SPEC[name]
    out = step(
        name,
        "Adaugă-l pe primul în coș.",
        {
            "acts": [{"kind": "cart", "targets": ["r1"]}],
            "references": [ref("r1", "ordinal", ordinal=1)],
        },
        on=s["three"],
    ).outcome
    assert verdict(out) == ("act", "clear")
    assert out.decision.question is None and out.asked_key is None and out.asked_kind is None


@pytest.mark.parametrize("name", PACKS)
def test_c12_the_cart_as_first_act_is_checked_even_when_find_is_primary(name):
    """§C.12: «adaugă-l pe primul în coș și arată-mi o husă». Coșul e PRIMUL act, principalul e
    `find`; ținta exactă ⇒ `act`."""
    s = SPEC[name]
    out = step(
        name,
        "Adaugă-l pe primul în coș și arată-mi un accesoriu.",
        {
            "acts": [{"kind": "cart", "targets": ["r1"]}, find("accesoriu")],
            "references": [ref("r1", "ordinal", ordinal=1)],
        },
        on=s["three"],
    ).outcome
    assert verdict(out) == ("act", "clear")


def test_a_model_reference_ambiguity_decides_nothing():
    """`Ambiguity{about: reference}` e semnalul modelului; verdictul pe țintă e al resolverului."""
    out = step(
        "electronics",
        "Adaugă-l pe primul în coș.",
        {
            "acts": [{"kind": "cart", "targets": ["r1"]}],
            "references": [ref("r1", "ordinal", ordinal=1)],
            "ambiguities": [{"about": "reference", "target": "r1", "readings": ["a", "b"]}],
        },
        on=SPEC["electronics"]["three"],
    ).outcome
    assert verdict(out) == ("act", "clear")


# --- regula 1: mutația (I10) ---------------------------------------------------------------------


@pytest.mark.parametrize("name", PACKS)
def test_cart_on_an_ambiguous_target_asks_with_the_candidates(name):
    s = SPEC[name]
    out = step(
        name,
        f"Adaugă {s['pair_name']} în coș.",
        {
            "acts": [{"kind": "cart", "targets": ["r1"]}],
            "references": [ref("r1", "name", name=s["pair_name"])],
        },
        on=s["pair"],
    ).outcome
    assert verdict(out) == ("must_ask", "mutation_not_exact")
    for full in names(name, *s["pair"]):
        assert full in out.decision.question
    assert out.asked_key == target_question_key(s["pair"])
    assert out.asked_kind == "pending"


@pytest.mark.parametrize("name", PACKS)
def test_c12_an_ambiguous_cart_as_first_act_asks(name):
    """Regresia care pică pe o poartă scrisă pe actul PRINCIPAL: aici principalul e `find`."""
    s = SPEC[name]
    out = step(
        name,
        f"Adaugă {s['pair_name']} în coș și arată-mi un accesoriu.",
        {
            "acts": [{"kind": "cart", "targets": ["r1"]}, find("accesoriu")],
            "references": [ref("r1", "name", name=s["pair_name"])],
        },
        on=s["pair"],
    ).outcome
    assert verdict(out) == ("must_ask", "mutation_not_exact")


@pytest.mark.parametrize("name", PACKS)
def test_cart_on_a_sold_out_exact_target_asks_without_a_question(name):
    """Resolverul întoarce un produs epuizat `exact` + `reason="unavailable"`, NU `stale`: o regulă
    scrisă pe `outcome != "exact"` lasă coșul să treacă."""
    s = SPEC[name]
    step_ = step(
        name,
        "Adaugă-l pe primul în coș.",
        {
            "acts": [{"kind": "cart", "targets": ["r1"]}],
            "references": [ref("r1", "ordinal", ordinal=1)],
        },
        on=s["sold_out"],
    )
    [resolved] = step_.resolved
    assert (resolved.outcome, resolved.reason) == ("exact", "unavailable")
    assert verdict(step_.outcome) == ("must_ask", "mutation_unavailable")
    assert step_.outcome.decision.question is None


def test_cart_on_a_target_gone_from_the_catalog_asks_on_the_screen():
    step_ = step(
        "electronics",
        "Adaugă-l pe primul în coș.",
        {
            "acts": [{"kind": "cart", "targets": ["r1"]}],
            "references": [ref("r1", "ordinal", ordinal=1)],
        },
        on=("el-01", "el-02", "el-03"),
        drop=("el-01",),
    )
    assert step_.resolved[0].outcome == "stale"
    assert verdict(step_.outcome) == ("must_ask", "mutation_not_exact")
    for full in names("electronics", "el-02", "el-03"):
        assert full in step_.outcome.decision.question
    assert names("electronics", "el-01")[0] not in step_.outcome.decision.question


def test_cart_on_a_changed_price_acts():
    step_ = step(
        "electronics",
        "Adaugă-l pe primul în coș.",
        {
            "acts": [{"kind": "cart", "targets": ["r1"]}],
            "references": [ref("r1", "ordinal", ordinal=1)],
        },
        on=("el-01", "el-02"),
        override={"el-01": {"price": 1600}},
    )
    assert step_.resolved[0].reason == "price_changed"
    assert verdict(step_.outcome) == ("act", "clear")


def test_cart_on_an_empty_screen_has_no_options_and_no_empty_placeholder():
    out = step(
        "electronics",
        "Adaugă-l în coș.",
        {"acts": [{"kind": "cart", "targets": ["r1"]}], "references": [ref("r1", "deictic")]},
    ).outcome
    assert verdict(out) == ("must_ask", "no_options")
    assert out.decision.question is None and out.asked_key is None


@pytest.mark.parametrize("reason", ["unavailable", "price_changed"])
def test_a_read_on_an_exact_target_with_a_reason_acts(reason):
    # el-07 e epuizat în pachet; el-08 e în stoc, cu prețul schimbat după ce l-a văzut clientul.
    on = ("el-07", "el-08") if reason == "unavailable" else ("el-08", "el-09")
    override = {"el-08": {"price": 3300}} if reason == "price_changed" else None
    step_ = step(
        "electronics",
        "Spune-mi mai multe despre primul.",
        {
            "acts": [{"kind": "detail", "targets": ["r1"]}],
            "references": [ref("r1", "ordinal", ordinal=1)],
        },
        on=on,
        override=override,
    )
    assert step_.resolved[0].reason == reason
    assert verdict(step_.outcome) == ("act", "clear")


def test_a_read_on_a_stale_target_acts():
    step_ = step(
        "electronics",
        "Spune-mi mai multe despre primul.",
        {
            "acts": [{"kind": "detail", "targets": ["r1"]}],
            "references": [ref("r1", "ordinal", ordinal=1)],
        },
        on=("el-01", "el-02"),
        drop=("el-01",),
    )
    assert step_.resolved[0].outcome == "stale"
    assert verdict(step_.outcome) == ("act", "clear")


# --- regula 0: ținte invalide --------------------------------------------------------------------


@pytest.mark.parametrize("name", PACKS)
def test_an_undeclared_target_skips_only_its_act(name):
    out = step(
        name,
        "Trimite-mi linkul și arată-mi un accesoriu.",
        {
            "acts": [{"kind": "link", "targets": ["r2"]}, find("accesoriu")],
            "references": [ref("r1", "ordinal", ordinal=1)],
        },
        on=SPEC[name]["three"],
    ).outcome
    assert verdict(out) == ("act", "invalid_target")
    assert out.skipped_acts == (0,)


def test_a_target_that_names_a_property_is_skipped_and_the_turn_acts():
    """I24: «linkul la zi de naștere» numește o valoare (ocazie), nu un produs."""
    step_ = step(
        "gifts",
        "Trimite-mi linkul la zi de nastere.",
        {
            "acts": [{"kind": "link", "targets": ["r1"]}],
            "references": [ref("r1", "name", name="zi de nastere")],
        },
    )
    assert step_.resolved[0].reason == "denotes_property"
    assert verdict(step_.outcome) == ("act", "invalid_target")
    assert step_.outcome.skipped_acts == (0,)


# --- regula 2: limitele încrucișate --------------------------------------------------------------


@pytest.mark.parametrize("name", PACKS)
def test_crossed_bounds_in_one_turn_ask_which_one_stays(name):
    user = "Ceva sub 200 de lei, dar minim 300 lei."
    step_ = step(
        name,
        user,
        {
            "acts": [find("ceva")],
            "changes": [
                {
                    "op": "set",
                    "dimension": "price",
                    "relation": "lte",
                    "number": 200,
                    "unit": "lei",
                    "quote": "sub 200 de lei",
                },
                {
                    "op": "set",
                    "dimension": "price",
                    "relation": "gte",
                    "number": 300,
                    "unit": "lei",
                    "quote": "minim 300 lei",
                },
            ],
        },
    )
    assert [c.rejected for c in step_.checked] == ["hard_conflict", "hard_conflict"]
    out = step_.outcome
    assert verdict(out) == ("must_ask", "hard_conflict")
    assert "sub 200 lei" in out.decision.question and "minim 300 lei" in out.decision.question
    # Opțiunile vin din valoarea canonică, nu din citat („200 de lei").
    assert "200 de lei" not in out.decision.question
    assert out.asked_key == "conflict:price"


# --- regula 3: fără subiect ----------------------------------------------------------------------


def _shelves(name: str) -> list[str]:
    vocab = fc.vocabulary(name)
    top = sorted(vocab.categories, key=lambda e: (-e.count, e.key))
    return [e.label for e in top[:4]]


@pytest.mark.parametrize("name", PACKS)
def test_find_without_a_subject_asks_on_the_top_shelves(name):
    out = step(name, "Vreau ceva.", {"acts": [find()]}).outcome
    assert verdict(out) == ("must_ask", "no_subject")
    template = fc.pack(name).clarify_templates["ro"]["subject"]
    assert out.decision.question.startswith(template.split("{options}")[0])
    for label in _shelves(name):
        assert label in out.decision.question
    assert out.asked_key == "subject"


def test_the_gifts_pack_asks_with_its_own_wording():
    out = step("gifts", "Vreau un cadou.", {"acts": [find()]}).outcome
    assert out.decision.question.startswith("Pentru ce fel de cadou")


def test_find_with_query_words_or_a_facet_need_is_not_a_subject_question():
    assert (
        step(
            "gifts", "Vreau ceva pentru bucătărie.", {"acts": [find("bucătărie")]}
        ).outcome.decision.reason
        == "clear"
    )
    out = step(
        "gifts",
        "Este pentru sora mea.",
        {
            "acts": [find()],
            "changes": [
                {
                    "op": "set",
                    "dimension": "recipient",
                    "relation": "eq",
                    "value": "ea",
                    "quote": "pentru sora mea",
                }
            ],
        },
    ).outcome
    assert verdict(out) == ("act", "clear")


def test_find_without_a_subject_on_a_single_shelf_has_no_gain():
    single = CatalogVocabulary(
        business_id="b-gifts",
        dimensions={CATEGORY_DIMENSION: (VocabEntry(key="casa", label="Casa", count=12),)},
    )
    out = step("gifts", "Vreau ceva.", {"acts": [find()]}, vocab=single).outcome
    assert verdict(out) == ("act", "no_subject_low_gain")
    assert out.decision.question is None


# --- regulile 4 și 5: lecturile ------------------------------------------------------------------


@pytest.mark.parametrize("name", PACKS)
def test_readings_that_resolve_to_the_same_value_resolve_from_context(name):
    dim, a, b = SPEC[name]["same"]
    out = step(
        name,
        f"Vreau ceva {a}.",
        {
            "acts": [find("model")],
            "ambiguities": [{"about": "value", "readings": [a, b]}],
        },
    ).outcome
    assert verdict(out) == ("resolve_from_context", "same_reading")
    assert out.decision.question is None


@pytest.mark.parametrize("name", PACKS)
def test_readings_on_different_values_ask_when_the_screen_splits(name):
    dim, values, on = SPEC[name]["differ"]
    out = step(
        name,
        "Ceva din astea, dar altfel.",
        {
            "acts": [find("altfel")],
            "ambiguities": [{"about": "value", "readings": list(values)}],
        },
        on=on,
    ).outcome
    assert verdict(out) == ("must_ask", "readings_differ")
    for value in values:
        assert value in out.decision.question
    assert out.asked_key == dim and out.asked_kind == "pending"


def test_readings_on_different_values_with_a_low_gain_act_on_the_subject():
    out = step(
        "fashion",
        "Ceva din astea, dar altfel.",
        {
            "acts": [find("altfel")],
            "ambiguities": [{"about": "value", "readings": ["negru", "rosu"]}],
        },
        on=("fa-01", "fa-02", "fa-04", "fa-05", "fa-06"),  # un negru, zero roșii din cinci
    ).outcome
    assert verdict(out) == ("act", "readings_low_gain")


def test_readings_differ_on_the_first_turn_ask_because_nothing_was_searched():
    """Fără set pe ecran, câștigul nu se poate estima: `decide_clarification` întreabă."""
    out = step(
        "fashion",
        "Ceva elegant.",
        {
            "acts": [find("elegant")],
            "ambiguities": [{"about": "scope", "readings": ["negru", "rosu"]}],
        },
    ).outcome
    assert verdict(out) == ("must_ask", "readings_differ")


def test_unresolved_readings_do_not_apply_and_never_reach_a_question():
    out = step(
        "fashion",
        "Ceva frumos.",
        {
            "acts": [find("frumos")],
            "ambiguities": [{"about": "value", "readings": ["elegant", "sport"]}],
        },
    ).outcome
    assert verdict(out) == ("act", "readings_unresolved")


def test_no_question_carries_the_models_readings_or_the_clients_quote():
    """Lectura „rosie" (text de model) se rezolvă la `rosu`: întrebarea poartă eticheta canonică."""
    out = step(
        "fashion",
        "Ceva închis la culoare.",
        {
            "acts": [find("inchis")],
            "ambiguities": [{"about": "value", "readings": ["rosie", "negru inchis la culoare"]}],
        },
    ).outcome
    # „negru inchis la culoare" nu se rezolvă ⇒ o singură valoare ⇒ nimic de întrebat.
    assert verdict(out) == ("resolve_from_context", "same_reading")
    out = step(
        "fashion",
        "Ceva închis la culoare.",
        {
            "acts": [find("inchis")],
            "ambiguities": [{"about": "value", "readings": ["rosie", "neagra"]}],
        },
    ).outcome
    assert out.decision.question is not None
    assert "rosie" not in out.decision.question and "neagra" not in out.decision.question
    assert "rosu" in out.decision.question and "negru" in out.decision.question


# --- regulile 6 și 7: ținte ambigue la citire ----------------------------------------------------


@pytest.mark.parametrize("name", PACKS)
def test_a_link_to_a_shared_name_answers_about_both(name):
    s = SPEC[name]
    out = step(
        name,
        f"Trimite-mi linkul la {s['pair_name']}.",
        {
            "acts": [{"kind": "link", "targets": ["r1"]}],
            "references": [ref("r1", "name", name=s["pair_name"])],
        },
        on=s["pair"],
    ).outcome
    assert verdict(out) == ("act_both", "ambiguous_read")
    assert out.decision.question is None and out.asked_key is None


@pytest.mark.parametrize("name", PACKS)
def test_five_ambiguous_candidates_on_a_read_ask_with_the_first_four(name):
    s = SPEC[name]
    step_ = step(
        name,
        f"Spune-mi mai multe despre {s['five_name']}.",
        {
            "acts": [{"kind": "detail", "targets": ["r1"]}],
            "references": [ref("r1", "name", name=s["five_name"])],
        },
        on=s["five"],
    )
    assert len(step_.resolved[0].product_ids) == 5
    out = step_.outcome
    assert verdict(out) == ("must_ask", "ambiguous_too_many")
    shown = names(name, *s["five"])
    for full in shown[:4]:
        assert full in out.decision.question
    assert shown[4] not in out.decision.question
    assert out.asked_key == target_question_key(s["five"])


# --- regula 8: confirmarea unei nevoi implicite --------------------------------------------------


def _implicit(name: str) -> dict:
    dim, value, _ = SPEC[name]["confirm"]
    quote = "cineva drag" if name == "gifts" else "ceva închis"
    return {
        "acts": [find(quote)],
        "changes": [
            {"op": "set", "dimension": dim, "relation": "eq", "value": value, "quote": quote}
        ],
    }


def _user(name: str) -> str:
    return "E pentru cineva drag." if name == "gifts" else "Vreau ceva închis."


@pytest.mark.parametrize("name", PACKS)
def test_an_implicit_need_with_gain_is_confirmed_as_the_closing_line(name):
    dim, value, on = SPEC[name]["confirm"]
    step_ = step(name, _user(name), _implicit(name), on=on)
    assert [c.provenance for c in step_.checked] == ["implicit"]
    out = step_.outcome
    assert verdict(out) == ("act", "confirm_implicit")
    assert value in out.decision.question
    assert (out.asked_key, out.asked_kind) == (dim, "noted")


@pytest.mark.parametrize("name", PACKS)
def test_an_implicit_need_on_the_first_turn_is_not_confirmed(name):
    out = step(name, _user(name), _implicit(name)).outcome
    assert verdict(out) == ("act", "clear")
    assert out.decision.question is None


# --- anti-bucla (I11) ----------------------------------------------------------------------------


def _asked(state: ConversationStateV2, key: str, attempts: int) -> ConversationStateV2:
    return replace(state, asked_questions=(AskedQuestion(key, state.revision, attempts),))


def test_a_read_key_asked_twice_answers_about_all_instead_of_a_third_question():
    s = SPEC["furniture"]
    base = _asked(screen("furniture", *s["five"]), target_question_key(s["five"]), 2)
    out = step(
        "furniture",
        "Spune-mi mai multe despre Oslo.",
        {
            "acts": [{"kind": "detail", "targets": ["r1"]}],
            "references": [ref("r1", "name", name="Oslo")],
        },
        state=base,
    ).outcome
    assert verdict(out) == ("act_both", "already_asked")
    assert out.decision.question is None and out.asked_key is None


def test_a_mutation_key_asked_twice_stops_the_cart_without_a_new_question():
    s = SPEC["electronics"]
    base = _asked(screen("electronics", *s["pair"]), target_question_key(s["pair"]), 2)
    out = step(
        "electronics",
        "Adaugă Samsung în coș.",
        {
            "acts": [{"kind": "cart", "targets": ["r1"]}],
            "references": [ref("r1", "name", name="Samsung")],
        },
        state=base,
    ).outcome
    assert verdict(out) == ("must_ask", "already_asked")
    assert out.decision.question is None


def _three_turns(name: str, compact: dict, user: str, *, on=(), resolve_between: bool):
    """Același tur de trei ori, cu memoria întrebării scrisă prin reducer între ture (a doua trecere
    a pasului 6). `resolve_between` = clientul răspunde între ture (tot ambiguu), deci întrebarea în
    așteptare se închide și cheia rămâne în `asked_questions`."""
    state = screen(name, *on) if on else ConversationStateV2()
    policy = ReducerPolicy()
    outcomes: list[GateOutcome] = []
    for i in range(3):
        step_ = fc.kernel_step(name, state, interp(**compact), user, turn_id=f"t{i}")
        outcomes.append(step_.outcome)
        state = step_.state_after
        if resolve_between and state.pending_clarification is not None:
            out = reduce(
                state, StateUpdateProposal("resolve_question", source="user_explicit"), policy
            )
            assert isinstance(out, ConversationStateV2)
            state = out
    return outcomes, state


@pytest.mark.parametrize("resolve_between", [False, True])
def test_i11_the_same_mutation_question_is_never_asked_beyond_the_cap_over_three_turns(
    resolve_between,
):
    s = SPEC["electronics"]
    compact = {
        "acts": [{"kind": "cart", "targets": ["r1"]}],
        "references": [ref("r1", "name", name="Samsung")],
    }
    outcomes, state = _three_turns(
        "electronics",
        compact,
        "Adaugă Samsung în coș.",
        on=s["pair"],
        resolve_between=resolve_between,
    )
    asked = [o for o in outcomes if o.decision.question is not None]
    assert 1 <= len(asked) <= POLICY.max_attempts_per_key
    assert all(o.decision.verdict == "must_ask" for o in outcomes)  # coșul nu trece niciodată
    key = target_question_key(s["pair"])
    assert all(o.asked_key == key for o in asked)
    if resolve_between:
        assert len(asked) == POLICY.max_attempts_per_key
        assert verdict(outcomes[-1]) == ("must_ask", "already_asked")
        assert state.asked(key).attempts == POLICY.max_attempts_per_key
    else:
        assert [verdict(o)[1] for o in outcomes[1:]] == ["already_pending", "already_pending"]


def test_i11_a_confirmation_is_noted_and_not_repeated_beyond_the_cap_over_three_turns():
    dim, _, on = SPEC["gifts"]["confirm"]
    outcomes, state = _three_turns(
        "gifts", _implicit("gifts"), _user("gifts"), on=on, resolve_between=False
    )
    assert [verdict(o)[1] for o in outcomes] == ["confirm_implicit", "confirm_implicit", "clear"]
    assert state.asked(dim).attempts == POLICY.max_attempts_per_key


# --- degradarea (P6) -----------------------------------------------------------------------------


def _without_templates(name: str):
    return replace(fc.pack(name), clarify_templates={})


def test_no_template_on_a_read_answers_about_all():
    s = SPEC["furniture"]
    out = step(
        "furniture",
        "Spune-mi mai multe despre Oslo.",
        {
            "acts": [{"kind": "detail", "targets": ["r1"]}],
            "references": [ref("r1", "name", name="Oslo")],
        },
        on=s["five"],
        loaded=_without_templates("furniture"),
    ).outcome
    assert verdict(out) == ("act_both", "no_template")


def test_no_template_on_a_mutation_still_stops_the_cart():
    s = SPEC["electronics"]
    out = step(
        "electronics",
        "Adaugă Samsung în coș.",
        {
            "acts": [{"kind": "cart", "targets": ["r1"]}],
            "references": [ref("r1", "name", name="Samsung")],
        },
        on=s["pair"],
        loaded=_without_templates("electronics"),
    ).outcome
    assert verdict(out) == ("must_ask", "no_template")
    assert out.decision.question is None and out.asked_key is None


def test_a_generic_template_covers_a_missing_kind():
    pack = fc.pack("electronics")
    table = {"ro": {"generic": "Mai exact? {options}"}}
    out = step(
        "electronics",
        "Adaugă Samsung în coș.",
        {
            "acts": [{"kind": "cart", "targets": ["r1"]}],
            "references": [ref("r1", "name", name="Samsung")],
        },
        on=SPEC["electronics"]["pair"],
        loaded=replace(pack, clarify_templates=table),
    ).outcome
    assert out.decision.question.startswith("Mai exact? ")


def test_without_a_vocabulary_the_rules_that_need_it_pass_and_say_so():
    out = step("gifts", "Vreau ceva.", {"acts": [find()]}, vocab=None).outcome
    assert verdict(out) == ("act", "vocabulary_unavailable")
    out = step(
        "fashion",
        "Vreau ceva roșu.",
        {
            "acts": [find("rosu")],
            "ambiguities": [{"about": "value", "readings": ["roșu", "rosie"]}],
        },
        vocab=None,
    ).outcome
    assert verdict(out) == ("act", "vocabulary_unavailable")


# --- vocabularul închis, proprietăți -------------------------------------------------------------

_PROPERTY = settings(
    max_examples=150,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)

_OUTCOMES = ("exact", "ambiguous", "not_found", "stale")
_REASONS = (
    None,
    "unavailable",
    "price_changed",
    "ordinal_in_set",
    "named",
    "name_shared",
    "no_set",
)
_ACT_KINDS = ("find", "detail", "link", "compare", "cart", "show_more", "chitchat")
_IDS = tuple(f"p{i}" for i in range(1, 7))


def _synthetic_facts() -> ReferenceFacts:
    return ReferenceFacts(
        products={
            pid: ProductFacts(
                product_id=pid, name=f"Produs {pid}", price=10.0 * i, available=i % 3 != 0
            )
            for i, pid in enumerate(_IDS, 1)
        }
    )


@st.composite
def _turns(draw, name: str):
    """O interpretare cu 1-3 acte (ținte pe r1/r2), verdictele resolverului pentru ele, ecranul și
    schimbările validate (inclusiv limite încrucișate)."""
    n_acts = draw(st.integers(1, 3))
    acts = []
    for _ in range(n_acts):
        kind = draw(st.sampled_from(_ACT_KINDS))
        targets = draw(st.lists(st.sampled_from(["r1", "r2"]), max_size=2, unique=True))
        acts.append(Act(kind=kind, targets=targets, query=draw(st.sampled_from([None, "ceva"]))))
    if len(acts) < 3 and draw(st.booleans()):
        # Un coș pe o poziție OARECARE (nu doar ultima): I10 cere verificarea pe orice act, iar
        # §C.12 are coșul primul și `find` principal.
        cart = Act(kind="cart", targets=[draw(st.sampled_from(["r1", "r2"]))], query=None)
        acts.insert(draw(st.integers(0, len(acts))), cart)
    resolved = []
    for rid in ("r1", "r2"):
        outcome = draw(st.sampled_from(_OUTCOMES))
        if outcome == "exact":
            ids = [draw(st.sampled_from(_IDS))]
        elif outcome == "ambiguous":
            ids = draw(st.lists(st.sampled_from(_IDS), min_size=2, max_size=6, unique=True))
        else:
            ids = []
        reason = draw(st.sampled_from(_REASONS))
        resolved.append(
            ResolvedRef(
                ref_id=rid,
                kind="name",
                outcome=outcome,
                product_ids=ids,
                source="shown_now",
                reason=reason,
            )
        )
    checked = []
    if draw(st.booleans()):
        for relation, number in (("lte", 100.0), ("gte", 150.0)):
            change = StateChange(
                op="set",
                target=None,
                dimension="price",
                relation=relation,
                value=None,
                number=number,
                unit="lei",
                relative_to=None,
                quote="q",
            )
            checked.append(
                CheckedChange(
                    change=change,
                    dimension="price",
                    canonical_value=number,
                    provenance="explicit",
                    strength="ranking",
                    rejected="hard_conflict",
                )
            )
    shown = draw(st.lists(st.sampled_from(_IDS), max_size=4, unique=True))
    asked = draw(
        st.lists(
            st.sampled_from([target_question_key(_IDS[:2]), "subject", "conflict:price"]),
            max_size=2,
            unique=True,
        )
    )
    attempts = draw(st.integers(0, 3))
    return acts, resolved, checked, shown, asked, attempts


def _state(shown, asked, attempts) -> ConversationStateV2:
    facts = _synthetic_facts()
    state = reduce(
        ConversationStateV2(revision=1),
        StateUpdateProposal(
            "set_references",
            source="catalog",
            payload={
                "displayed_products": [
                    {"product_id": p, "name": facts.products[p].name} for p in shown
                ]
            },
        ),
        ReducerPolicy(),
    )
    assert isinstance(state, ConversationStateV2)
    if attempts:
        state = replace(state, asked_questions=tuple(AskedQuestion(k, 1, attempts) for k in asked))
    return state


def _decide(name: str, acts, resolved, checked, state) -> GateOutcome:
    loaded = fc.pack(name)
    return decide_ambiguity(
        TurnInterpretation(
            thread="continue",
            acts=acts,
            changes=[c.change for c in checked],
            references=[],
            ambiguities=[],
            corrects_previous_turn=False,
        ),
        checked,
        resolved,
        state,
        _synthetic_facts(),
        vocab=fc.vocabulary(name) if name != "sole-ro" else None,
        pack=loaded,
        locale="ro",
        policy=POLICY,
    )


@pytest.mark.parametrize("name", ALL_PACKS)
@_PROPERTY
@given(data=st.data())
def test_i10_any_mutating_act_on_a_non_exact_or_sold_out_target_must_ask(name, data):
    acts, resolved, checked, shown, asked, attempts = data.draw(_turns(name))
    out = _decide(name, acts, resolved, checked, _state(shown, asked, attempts))
    by_id = {r.ref_id: r for r in resolved}
    # Un coș FĂRĂ țintă e tot „fără țintă `exact`" (recenzia NX-332: `any` pe o listă goală îl
    # scutea, deci proprietatea nu vedea exact cazul pe care poarta îl rata).
    blocked = any(
        act.kind == "cart"
        and (
            not act.targets
            or any(
                by_id[t].outcome != "exact" or by_id[t].reason == "unavailable" for t in act.targets
            )
        )
        for act in acts
    )
    if blocked:
        assert out.decision.verdict == "must_ask", out
    assert out.decision.reason in GATE_REASONS


@pytest.mark.parametrize("name", ALL_PACKS)
@_PROPERTY
@given(data=st.data())
def test_i11_no_verdict_asks_a_key_at_or_over_the_cap_and_at_most_one_question(name, data):
    acts, resolved, checked, shown, asked, attempts = data.draw(_turns(name))
    state = _state(shown, asked, attempts)
    out = _decide(name, acts, resolved, checked, state)
    if out.asked_key is not None:
        previous = state.asked(out.asked_key)
        assert previous is None or previous.attempts < POLICY.max_attempts_per_key
        assert out.decision.question is not None
    else:
        assert out.asked_kind is None
    assert out.decision.question is None or isinstance(out.decision.question, str)


@pytest.mark.parametrize("name", ALL_PACKS)
@_PROPERTY
@given(data=st.data())
def test_determinism_same_inputs_same_decision(name, data):
    acts, resolved, checked, shown, asked, attempts = data.draw(_turns(name))
    state = _state(shown, asked, attempts)
    first = _decide(name, acts, resolved, checked, state)
    again = _decide(name, list(acts), list(resolved), list(checked), state)
    assert first == again


def test_every_reason_emitted_by_the_suite_is_in_the_closed_vocabulary():
    """`GATE_REASONS` e închis: fiecare motiv pe care poarta îl poate emite e acolo, iar fiecare
    motiv declarat e emis de cel puțin un scenariu (altfel lista ar fi minciună)."""
    seen: set[str] = set()
    scenarios = [
        lambda: step(
            "electronics",
            "x",
            {
                "acts": [{"kind": "cart", "targets": ["r1"]}],
                "references": [ref("r1", "ordinal", ordinal=1)],
            },
            on=SPEC["electronics"]["three"],
        ),
        lambda: step(
            "electronics",
            "x",
            {
                "acts": [{"kind": "cart", "targets": ["r1"]}],
                "references": [ref("r1", "name", name="Samsung")],
            },
            on=SPEC["electronics"]["pair"],
        ),
        lambda: step(
            "electronics",
            "x",
            {
                "acts": [{"kind": "cart", "targets": ["r1"]}],
                "references": [ref("r1", "ordinal", ordinal=1)],
            },
            on=SPEC["electronics"]["sold_out"],
        ),
        lambda: step(
            "electronics",
            "x",
            {"acts": [{"kind": "cart", "targets": ["r1"]}], "references": [ref("r1", "deictic")]},
        ),
        lambda: step(
            "gifts",
            "x",
            {
                "acts": [{"kind": "link", "targets": ["r2"]}],
                "references": [ref("r1", "ordinal", ordinal=1)],
            },
        ),
        lambda: step("gifts", "Vreau ceva.", {"acts": [find()]}),
        lambda: step("gifts", "Vreau ceva.", {"acts": [find()]}, vocab=None),
        lambda: step(
            "fashion",
            "x",
            {
                "acts": [find("frumos")],
                "ambiguities": [{"about": "value", "readings": ["elegant"]}],
            },
        ),
        lambda: step(
            "fashion",
            "x",
            {
                "acts": [find("rosu")],
                "ambiguities": [{"about": "value", "readings": ["roșu", "rosie"]}],
            },
        ),
        lambda: step(
            "fashion",
            "x",
            {
                "acts": [find("x")],
                "ambiguities": [{"about": "value", "readings": ["negru", "albastru"]}],
            },
            on=SPEC["fashion"]["differ"][2],
        ),
        lambda: step(
            "fashion",
            "x",
            {
                "acts": [find("x")],
                "ambiguities": [{"about": "value", "readings": ["negru", "rosu"]}],
            },
            on=("fa-01", "fa-02", "fa-04", "fa-05", "fa-06"),
        ),
        lambda: step(
            "electronics",
            "x",
            {
                "acts": [{"kind": "link", "targets": ["r1"]}],
                "references": [ref("r1", "name", name="Samsung")],
            },
            on=SPEC["electronics"]["pair"],
        ),
        lambda: step(
            "furniture",
            "x",
            {
                "acts": [{"kind": "detail", "targets": ["r1"]}],
                "references": [ref("r1", "name", name="Oslo")],
            },
            on=SPEC["furniture"]["five"],
        ),
        lambda: step("gifts", _user("gifts"), _implicit("gifts"), on=SPEC["gifts"]["confirm"][2]),
        lambda: step(
            "electronics",
            "Ceva sub 200 lei, dar minim 300 lei.",
            {
                "acts": [find("ceva")],
                "changes": [
                    {
                        "op": "set",
                        "dimension": "price",
                        "relation": "lte",
                        "number": 200,
                        "unit": "lei",
                        "quote": "sub 200 lei",
                    },
                    {
                        "op": "set",
                        "dimension": "price",
                        "relation": "gte",
                        "number": 300,
                        "unit": "lei",
                        "quote": "minim 300 lei",
                    },
                ],
            },
        ),
        lambda: step(
            "electronics",
            "x",
            {
                "acts": [{"kind": "cart", "targets": ["r1"]}],
                "references": [ref("r1", "name", name="Samsung")],
            },
            on=SPEC["electronics"]["pair"],
            loaded=_without_templates("electronics"),
        ),
        lambda: step(
            "electronics",
            "x",
            {
                "acts": [{"kind": "cart", "targets": ["r1"]}],
                "references": [ref("r1", "name", name="Samsung")],
            },
            state=_asked(
                screen("electronics", *SPEC["electronics"]["pair"]),
                target_question_key(SPEC["electronics"]["pair"]),
                2,
            ),
        ),
        lambda: step(
            "gifts",
            "Este pentru sora mea.",
            {"acts": [find()]},
            vocab=CatalogVocabulary(
                business_id="b", dimensions={CATEGORY_DIMENSION: (VocabEntry("casa", "Casa", 3),)}
            ),
        ),
    ]
    for build in scenarios:
        seen.add(build().outcome.decision.reason)
    # Aceeași ambiguitate ca turul anterior, cu întrebarea încă în așteptare.
    outcomes, _ = _three_turns(
        "electronics",
        {
            "acts": [{"kind": "cart", "targets": ["r1"]}],
            "references": [ref("r1", "name", name="Samsung")],
        },
        "Adaugă Samsung în coș.",
        on=SPEC["electronics"]["pair"],
        resolve_between=False,
    )
    seen |= {o.decision.reason for o in outcomes}
    # Lecturi pe o dimensiune pe care clientul a spus-o deja explicit.
    seen.add(
        step(
            "fashion",
            "Vreau negru.",
            {
                "acts": [find("negru")],
                "changes": [
                    {
                        "op": "set",
                        "dimension": "color",
                        "relation": "eq",
                        "value": "negru",
                        "quote": "negru",
                    }
                ],
                "ambiguities": [{"about": "value", "readings": ["negru", "albastru"]}],
            },
            on=SPEC["fashion"]["differ"][2],
        ).outcome.decision.reason
    )
    assert seen <= set(GATE_REASONS), seen - set(GATE_REASONS)
    assert set(GATE_REASONS) <= seen, set(GATE_REASONS) - seen


# --- regresiile din recenzie ---------------------------------------------------------------------


@pytest.mark.parametrize("name", PACKS)
@pytest.mark.parametrize(
    "acts", [[{"kind": "cart"}], [{"kind": "cart"}, find("o husa")]], ids=["alone", "then-find"]
)
def test_a_cart_without_a_target_asks(name, acts):
    """Recenzia NX-332 (I10): un coș fără țintă nu producea nicio verificare în `gate_act_targets`,
    deci ieșea `act clear`. Nu există o țintă `exact`, deci coșul nu se execută."""
    out = step(name, "pune-l in cos", {"acts": acts}, on=SPEC[name]["three"]).outcome
    assert verdict(out) == ("must_ask", "mutation_not_exact")
    for product in names(name, *SPEC[name]["three"]):
        assert out.decision.question is not None
        assert product in out.decision.question


def test_a_cart_without_a_target_on_an_empty_screen_asks_nothing():
    out = step("electronics", "pune-l in cos", {"acts": [{"kind": "cart"}]}).outcome
    assert (verdict(out), out.decision.question) == (("must_ask", "no_options"), None)


def test_a_scope_reading_on_shelves_is_unmeasurable_on_screen_so_it_asks():
    """Recenzia NX-332: raftul nu e în `ProductFacts`, deci partiția număra 0 pe toate valorile și
    ieșea «câștig mic» exact când ecranul avea ambele rafturi. Nemăsurabil ⇒ întrebare."""
    compact = {
        "acts": [find("ceva cu ecran mare")],
        "ambiguities": [{"about": "scope", "readings": ["telefoane", "tablete"]}],
    }
    for on in ((), ("el-01", "el-02", "el-11", "el-12")):
        out = step("electronics", "ceva cu ecran mare", compact, on=on).outcome
        assert verdict(out) == ("must_ask", "readings_differ"), on


def test_an_attribute_the_lookup_did_not_read_is_not_counted_as_zero():
    """Recenzia NX-332: în producție `reference_facts` aduce doar atributele cerute. Un atribut
    necerut lipsea din fapte ca și cum n-ar fi fost pe niciun produs: partiția ieșea zero, iar
    întrebarea cădea pe «câștig mic». Nemăsurabil ⇒ `total=None` ⇒ întrebare."""
    field_, values, on = SPEC["electronics"]["differ"]
    compact = {
        "acts": [find("un telefon")],
        "ambiguities": [{"about": "value", "readings": list(values)}],
    }
    base = screen("electronics", *on)
    known = fc.facts("electronics")
    unread = replace(known, attributes_read=("brand",))
    out = decide_ambiguity(
        interp(**compact),
        [],
        [],
        base,
        unread,
        vocab=fc.vocabulary("electronics"),
        pack=fc.pack("electronics"),
        locale="ro",
        policy=POLICY,
    )
    assert verdict(out) == ("must_ask", "readings_differ")


def test_the_gate_declares_the_attributes_it_counts():
    """`lookup_attributes`: ce numără poarta intră în ACEEAȘI citire (`plan_lookup`)."""
    from src.conversation.ambiguity_gate import lookup_attributes

    field_, values, _ = SPEC["electronics"]["differ"]
    compact = {
        "acts": [find("arata-mi")],
        "ambiguities": [{"about": "value", "readings": list(values)}],
        "changes": [
            {"op": "add", "dimension": "color", "relation": "eq", "value": "negru", "quote": "q"}
        ],
    }
    attrs = lookup_attributes(
        interp(**compact), vocab=fc.vocabulary("electronics"), pack=fc.pack("electronics")
    )
    assert field_ in attrs
    assert CATEGORY_DIMENSION not in attrs


def test_duplicate_names_are_not_offered_as_two_options():
    """Recenzia NX-332: pe SOLE există produse cu nume identic. „La care te referi: X și X?" nu e o
    întrebare; sub două nume distincte, poarta nu întreabă (coșul tot nu se execută)."""
    pair = SPEC["electronics"]["pair"]
    same = {pid: {"name": "Samsung Phone 1 128 GB"} for pid in pair}
    out = step(
        "electronics",
        "adauga Samsung-ul in cos",
        {
            "acts": [{"kind": "cart", "targets": ["r1"]}],
            "references": [ref("r1", "name", name="Samsung")],
        },
        on=pair,
        override=same,
    ).outcome
    assert out.decision.verdict == "must_ask"
    assert out.decision.question is None or out.decision.question.count("Samsung Phone 1") == 1


def test_conflict_bounds_are_written_in_the_locale():
    """Recenzia NX-332: `:g` dădea „1.23457e+06" și punct zecimal. Pe `ro`: virgulă, fără notație
    științifică, fără zecimale pe o sumă întreagă."""
    out = step(
        "electronics",
        "sub 99,99 lei dar minim 1234567 lei",
        {
            "acts": [find("ceva")],
            "changes": [
                {
                    "op": "set",
                    "dimension": "price",
                    "relation": "lte",
                    "number": 99.99,
                    "unit": "lei",
                    "quote": "sub 99,99 lei",
                },
                {
                    "op": "set",
                    "dimension": "price",
                    "relation": "gte",
                    "number": 1234567,
                    "unit": "lei",
                    "quote": "minim 1234567 lei",
                },
            ],
        },
    ).outcome
    question = out.decision.question or ""
    assert verdict(out) == ("must_ask", "hard_conflict")
    assert "99,99 lei" in question and "1.234.567 lei" in question
    assert "e+" not in question


def test_a_conflict_is_asked_even_when_no_act_is_left():
    """Recenzia NX-332: `_rules` rula doar cu acte, deci două limite contradictorii într-un tur
    fără act ieșeau `act clear`, fără întrebare."""
    out = step(
        "electronics",
        "sub 200 de lei, dar minim 300 lei",
        {
            "acts": [],
            "changes": [
                {
                    "op": "set",
                    "dimension": "price",
                    "relation": "lte",
                    "number": 200,
                    "unit": "lei",
                    "quote": "sub 200 de lei",
                },
                {
                    "op": "set",
                    "dimension": "price",
                    "relation": "gte",
                    "number": 300,
                    "unit": "lei",
                    "quote": "minim 300 lei",
                },
            ],
        },
    ).outcome
    assert verdict(out) == ("must_ask", "hard_conflict")


def test_a_numeric_bound_counts_as_a_facet_need_for_the_subject_rule():
    """NX-332 × NX-334: o limită numerică se memorează ca `storage_min`, nu `storage`. Regula
    „`find` fără subiect" compara cheia nevoii cu cheile fațetelor, deci «minim 256 GB» fără raft
    primea întrebarea de subiect deși clientul a spus ce vrea. O nevoie de culoare (cheie = fațetă)
    trecea deja; limita trebuie să treacă la fel."""
    for change, said in (
        (
            {
                "op": "add",
                "dimension": "storage",
                "relation": "gte",
                "number": 256,
                "unit": "gb",
                "quote": "minim 256 GB",
            },
            "minim 256 GB",
        ),
        (
            {
                "op": "add",
                "dimension": "color",
                "relation": "eq",
                "value": "negru",
                "quote": "negru",
            },
            "negru",
        ),
    ):
        out = step("electronics", said, {"acts": [find()], "changes": [change]}).outcome
        assert verdict(out) == ("act", "clear"), said


@pytest.mark.parametrize("name", PACKS)
def test_a_passive_reading_verdict_does_not_hide_an_ambiguous_target(name):
    """Găsit la NX-333: regulile lecturilor (4/5) se evaluau înaintea țintelor ambigue (6/7), iar
    un verdict care NU întreabă (`same_reading`) închidea evaluarea. Un `detail` pe două produse
    lângă o lectură rezolvată ieșea `resolve_from_context`, deci plannerul n-avea țintă
    (`reply_only`), în loc de răspunsul despre ambele (`act_both`)."""
    pair = SPEC[name]["pair"]
    _field, first, second = SPEC[name]["same"]
    out = step(
        name,
        f"spune-mi de {SPEC[name]['pair_name']}",
        {
            "acts": [{"kind": "detail", "targets": ["r1"]}],
            "references": [ref("r1", "name", name=SPEC[name]["pair_name"])],
            "ambiguities": [{"about": "value", "readings": [first, second]}],
        },
        on=pair,
    ).outcome
    assert verdict(out) == ("act_both", "ambiguous_read")
