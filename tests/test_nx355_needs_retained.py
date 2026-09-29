"""NX-355 — pe calea v1, ce a spus clientul rămâne în căutare de la un tur la altul.

Conversația `1748f988` (`sole-ro`, 2026-09-29): «vreau o crema de hidratare» → «am ten uscat» →
«sub 100 de lei sa vad». Trei goluri, fiecare cu testul lui mai jos:

1. «ten uscat» (scris de model în `concerns`) e `skin_type=dry`; la încărcare `adapt_v1` îl căuta
   doar printre valorile `concerns`, deci ieșea `unknown` și dispărea. Iar fuziunea stivei v1 nu
   căra nicio cheie în afara celor trei fixe.
2. Nevoile stivei nu intrau în căutare dacă modelul trimitea ALTE nevoi.
3. Setul de măști (căutarea ratase) muta subiectul pe `masca de fata`, deci eroarea se
   autoîntărea la turul următor.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from src.config import get_settings
from src.conversation.needs import (
    SOFT,
    NeedKind,
    NeedSpec,
    NeedVocabulary,
    positive_facet_keys,
    rehome_list_value,
)
from src.conversation.state_v2 import adapt_v1, project_v1
from src.conversation.subject import SUBJECT_KEY, ConversationSubject, search_named_type
from src.models import BusinessConfig, Contact, ConversationState, InboundMessage, TurnContext
from src.tools.catalog_tools import SearchArgs, _carry_retained
from src.worker.stages.agent import _filters_hint, _learn_subject, merge_constraints

VOCAB = NeedVocabulary(
    specs={
        "concerns": NeedSpec(
            "concerns", NeedKind.LIST, SOFT, values=frozenset({"hydration", "acne", "barrier"})
        ),
        "skin_type": NeedSpec(
            "skin_type",
            NeedKind.SCALAR,
            SOFT,
            values=frozenset({"dry", "oily", "combination", "sensitive"}),
        ),
        "budget_max": NeedSpec("budget_max", NeedKind.NUMERIC_MAX, "hard", scoped=False),
        "restriction": NeedSpec("restriction", NeedKind.EXCLUSION, "hard", scoped=False),
    },
    concern_map={"ten uscat": "dry", "ten gras": "oily", "hidratare": "hydration"},
)

SHELF = "ten-ingrijirea-tenului"
CREAM = "crema de fata"
MASK = "masca de fata"


# --- 1a. mutarea valorii în fațeta ei ---------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("ten uscat", ("skin_type", "dry")),  # prin harta tenantului
        ("ten gras", ("skin_type", "oily")),
        ("dry", ("skin_type", "dry")),  # deja canonic, dar al altei fațete
        ("hydration", None),  # valoare corectă de `concerns`: nu se atinge
        ("hidratare", None),  # harta o duce în `concerns`
        ("ceva la intamplare", None),  # niciun proprietar: comportamentul de dinainte
    ],
)
def test_rehome_list_value(value: str, expected: tuple[str, str] | None) -> None:
    assert rehome_list_value("concerns", value, VOCAB) == expected


def test_rehome_needs_exactly_one_owner() -> None:
    """Două fațete care ar primi aceeași valoare = ghicire. Nu mutăm."""
    two = NeedVocabulary(
        specs={
            **VOCAB.specs,
            "hair_type": NeedSpec("hair_type", NeedKind.SCALAR, SOFT, values=frozenset({"dry"})),
        },
        concern_map=dict(VOCAB.concern_map),
    )
    assert rehome_list_value("concerns", "ten uscat", two) is None


def test_scalar_key_is_not_rehomed() -> None:
    assert rehome_list_value("skin_type", "dry", VOCAB) is None


# --- 1b. memoria nu mai pierde tipul de ten ---------------------------------------------------


def test_adapt_v1_keeps_skin_type_written_as_concern() -> None:
    """Exact forma salvată după turul 2 din `1748f988`."""
    st = adapt_v1(
        {"search_constraints": {"concerns": ["hydration", "ten uscat"], "category_key": SHELF}},
        VOCAB,
    )
    needs = {(n.key, n.normalized_value, n.status) for n in st.needs}
    assert ("skin_type", "dry", "active") in needs
    assert ("concerns", "hydration", "active") in needs
    assert project_v1(st)["search_constraints"]["skin_type"] == "dry"


def test_adapt_v1_raw_constraint_answer_is_rehomed_too() -> None:
    st = adapt_v1({"constraints": {"concerns": "ten uscat"}}, VOCAB)
    assert [(n.key, n.normalized_value) for n in st.needs] == [("skin_type", "dry")]


def test_merge_carries_facet_keys_only_when_asked() -> None:
    stored = {"concerns": ["hydration"], "skin_type": "dry", "category_key": SHELF}
    carried, _ = merge_constraints(stored, {"budget_max": 100}, None, carry_facets=True)
    assert carried["skin_type"] == "dry" and carried["budget_max"] == 100
    legacy, _ = merge_constraints(stored, {"budget_max": 100}, None)
    assert "skin_type" not in legacy  # OFF = byte-identic


def test_merge_drops_carried_facets_on_topic_reset() -> None:
    stored = {"skin_type": "dry", "category_key": SHELF}
    merged, reset = merge_constraints(stored, {}, "par-ingrijirea-parului", carry_facets=True)
    assert reset and "skin_type" not in merged


def test_hint_shows_retained_facets_and_subject() -> None:
    stack = {
        "concerns": ["hydration"],
        "skin_type": "dry",
        "category_key": SHELF,
        SUBJECT_KEY: ConversationSubject(shelf_key=SHELF, product_type=CREAM).to_dict(),
    }
    positive = positive_facet_keys(VOCAB)
    hint = _filters_hint({**stack, "restriction": "alcool"}, retained=True, positive=positive)
    assert "skin_type: dry" in hint and f"tipul de produs discutat: {CREAM}" in hint
    assert "alcool" not in hint  # o excludere nu se afișează ca cerere
    assert "skin_type" not in _filters_hint(stack, positive=positive)  # OFF = byte-identic


# --- 2. nevoile stivei intră în căutare -------------------------------------------------------


def _ctx(stack: dict[str, Any]) -> TurnContext:
    ctx = TurnContext(
        turn_id="t",
        business=BusinessConfig(id="b", slug="d", name="D"),
        contact=Contact(id="c", business_id="b"),
        message=InboundMessage(provider_msg_id="m", body="sub 100 de lei sa vad"),
        conversation_id="conv",
        state=ConversationState(),
    )
    ctx.state.search_constraints = stack
    return ctx


@pytest.fixture
def vocab_from_pack(monkeypatch):
    monkeypatch.setattr(NeedVocabulary, "from_pack", classmethod(lambda cls, pack: VOCAB))


def _stack() -> dict[str, Any]:
    return {
        "concerns": ["hydration"],
        "skin_type": "dry",
        "category_key": SHELF,
        SUBJECT_KEY: ConversationSubject(shelf_key=SHELF, product_type=CREAM).to_dict(),
    }


def test_turn_3_search_carries_skin_type_and_prefers_the_subject(vocab_from_pack) -> None:
    ctx = _ctx(_stack())
    a = SearchArgs(
        query="cremă de hidratare pentru ten uscat, hidratare, sub 100 de lei",
        category=SHELF,
        concerns=["hydration"],
        price_max=100,
    )
    _carry_retained(ctx, a)
    assert a.concerns == ["hydration", "dry"]
    assert a.prefer == {"product_type": [CREAM]}


def test_nothing_carried_on_another_shelf(vocab_from_pack) -> None:
    ctx = _ctx(_stack())
    a = SearchArgs(query="sampon", category="par-ingrijirea-parului", concerns=["dandruff"])
    _carry_retained(ctx, a)
    assert a.concerns == ["dandruff"] and not a.prefer


def test_a_changed_facet_is_not_carried(vocab_from_pack) -> None:
    """«ten gras» după «ten uscat»: clientul s-a răzgândit, nu cere un ten și uscat, și gras."""
    ctx = _ctx(_stack())
    a = SearchArgs(query="crema pentru ten gras", category=SHELF, concerns=["ten gras"])
    _carry_retained(ctx, a)
    assert "dry" not in a.concerns and "hydration" in a.concerns


def test_no_type_preference_when_the_search_asks_for_another_type(vocab_from_pack) -> None:
    ctx = _ctx(_stack())
    a = SearchArgs(query="măști de față hidratante", category=SHELF)
    _carry_retained(ctx, a)
    assert not a.prefer


# --- 3. o căutare ratată nu mută subiectul ----------------------------------------------------


def test_search_named_type() -> None:
    assert search_named_type(["cremă de hidratare, sub 100 lei"], CREAM)
    assert not search_named_type(["măști de față"], CREAM)
    assert not search_named_type([None], CREAM)


def _run(query: str, types: list[str]) -> SimpleNamespace:
    return SimpleNamespace(
        search_args=[{"query": query, "category": SHELF}],
        retrieved=[{"id": f"p{i}", "attributes": {"product_type": t}} for i, t in enumerate(types)],
    )


def _subject_after(query: str, types: list[str]) -> tuple[ConversationSubject | None, TurnContext]:
    previous = ConversationSubject(shelf_key=SHELF, product_type=CREAM)
    ctx = _ctx({SUBJECT_KEY: previous.to_dict(), "category_key": SHELF})
    _learn_subject(ctx, _run(query, types), None, SHELF, {}, previous)
    return ConversationSubject.from_dict(ctx.state.search_constraints.get(SUBJECT_KEY)), ctx


def test_masks_from_a_cream_search_do_not_move_the_subject() -> None:
    subject, ctx = _subject_after("cremă de hidratare, sub 100 de lei", [MASK] * 6)
    assert subject is not None and subject.product_type == CREAM
    kept = [e.properties for e in ctx.events if e.type == "subject_kept"]
    assert kept and kept[0]["dropped"] == 6


def test_a_search_for_masks_still_moves_the_subject() -> None:
    """Clientul a cerut măști: căutarea nu numește crema, deci setul e dovadă, ca înainte."""
    subject, _ = _subject_after("măști de față hidratante", [MASK] * 6)
    assert subject is not None and subject.product_type == MASK


def test_flag_off_is_the_old_subject_behaviour(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "needs_retained_enabled", False)
    subject, _ = _subject_after("cremă de hidratare, sub 100 de lei", [MASK] * 6)
    assert subject is not None and subject.product_type == MASK


def test_carried_needs_count_as_the_clients(vocab_from_pack) -> None:
    """Garda NX-313 judecă o nevoie pe cuvintele clientului; «dry» (codul canonic) nu apare literal
    în «am ten uscat», deci fără asta nevoia cărată era scoasă ca ghicită."""
    ctx = _ctx(_stack())
    a = SearchArgs(query="crema de hidratare", category=SHELF, concerns=["hydration"])
    assert _carry_retained(ctx, a) is True


def test_a_new_model_need_is_still_judged(vocab_from_pack) -> None:
    """Modelul a adăugat o nevoie pe care stiva n-o are: garda NX-313 o judecă în continuare."""
    ctx = _ctx(_stack())
    a = SearchArgs(query="crema de hidratare", category=SHELF, concerns=["acne"])
    assert _carry_retained(ctx, a) is False
    assert "dry" in a.concerns  # cărarea s-a făcut, doar proveniența rămâne a gărzii


def test_an_exclusion_is_never_carried_as_a_need(vocab_from_pack) -> None:
    """Stiva v1 pierde operatorul: `restriction: alcool` (clientul a EXCLUS alcoolul) ar fi plecat
    în căutare ca «alcool» dorit. Doar fațetele scalare cu valori închise se cară."""
    ctx = _ctx({**_stack(), "restriction": "alcool", "budget_max": 100.0})
    a = SearchArgs(query="crema de hidratare", category=SHELF, concerns=["hydration"])
    _carry_retained(ctx, a)
    assert a.concerns == ["hydration", "dry"]
