"""NX-355 — pe calea v1, ce a spus clientul rămâne în căutare de la un tur la altul.

Conversația `1748f988` (`sole-ro`, 2026-09-29): «vreau o crema de hidratare» → «am ten uscat» →
«sub 100 de lei sa vad». Trei goluri, fiecare cu testul lui mai jos:

1. «ten uscat» (scris de model în `concerns`) e `skin_type=dry`; la încărcare `adapt_v1` îl căuta
   doar printre valorile `concerns`, deci ieșea `unknown` și dispărea. Iar fuziunea stivei v1 nu
   căra nicio cheie în afara celor trei fixe.
2. Tipul de ten nu intra în căutare dacă modelul îl omitea.
3. Setul de măști (căutarea ratase) muta subiectul pe `masca de fata`, deci eroarea se
   autoîntărea la turul următor.

Testele marcate „recenzia” pinuiesc defectele găsite de recenzia adversarială a primei variante.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from src.catalog.vocabulary import CatalogVocabulary, VocabEntry
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
from src.conversation.subject import (
    SUBJECT_KEY,
    ConversationSubject,
    search_named_type,
    type_fits_query,
)
from src.models import BusinessConfig, Contact, ConversationState, InboundMessage, TurnContext
from src.tools.catalog_tools import SearchArgs, _carry_retained, _prefer_subject_type
from src.worker.stages.agent import _filters_hint, _learn_subject, merge_constraints

VOCAB = NeedVocabulary(
    specs={
        "concerns": NeedSpec(
            "concerns",
            NeedKind.LIST,
            SOFT,
            values=frozenset({"hydration", "acne", "barrier", "hyperpigmentation"}),
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
POSITIVE = positive_facet_keys(VOCAB)

SHELF = "ten-ingrijirea-tenului"
CREAM = "crema de fata"
MASK = "masca de fata"
EYE = "crema contur ochi"
BODY = "crema de corp"
CATALOG = CatalogVocabulary(
    business_id="b",
    dimensions={
        "product_type": tuple(
            VocabEntry(key=t, label=t, count=10) for t in (CREAM, EYE, BODY, "crema de maini", MASK)
        ),
        "skin_type": tuple(VocabEntry(key=v, label=v, count=10) for v in ("dry", "oily")),
    },
)
PACK = SimpleNamespace(concern_map=dict(VOCAB.concern_map), facets=())


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


def test_only_positive_closed_facets_are_positive() -> None:
    assert POSITIVE == frozenset({"skin_type"})


# --- 1b. memoria nu mai pierde tipul de ten ---------------------------------------------------


def test_adapt_v1_keeps_skin_type_written_as_concern() -> None:
    """Exact forma salvată după turul 2 din `1748f988`."""
    st = adapt_v1(
        {"search_constraints": {"concerns": ["hydration", "ten uscat"], "category_key": SHELF}},
        VOCAB,
        rehome=True,
    )
    needs = {(n.key, n.normalized_value, n.status) for n in st.needs}
    assert ("skin_type", "dry", "active") in needs
    assert ("concerns", "hydration", "active") in needs
    assert project_v1(st)["search_constraints"]["skin_type"] == "dry"


def test_adapt_v1_raw_constraint_answer_is_rehomed_too() -> None:
    st = adapt_v1({"constraints": {"concerns": "ten uscat"}}, VOCAB, rehome=True)
    assert [(n.key, n.normalized_value) for n in st.needs] == [("skin_type", "dry")]


def test_adapt_v1_without_the_flag_is_the_old_behaviour() -> None:
    """Recenzia: mutarea rula și cu flagul stins, deci „OFF = byte-identic” era fals."""
    doc = {"search_constraints": {"concerns": ["hydration", "ten uscat"]}}
    st = adapt_v1(doc, VOCAB)
    assert [(n.key, n.normalized_value) for n in st.needs] == [("concerns", "hydration")]


def test_merge_carries_only_positive_facet_keys() -> None:
    stored = {"concerns": ["hydration"], "skin_type": "dry", "category_key": SHELF}
    carried, _ = merge_constraints(stored, {"budget_max": 100}, None, carry_keys=POSITIVE)
    assert carried["skin_type"] == "dry" and carried["budget_max"] == 100
    legacy, _ = merge_constraints(stored, {"budget_max": 100}, None)
    assert "skin_type" not in legacy  # OFF = byte-identic


def test_merge_never_carries_exclusions_or_bounds_and_keeps_the_skin_type() -> None:
    """Recenzia: cu excluderi și limite numerice în stivă, plafonul de 4 se umplea cu ele și
    arunca exact tipul de ten."""
    stored = {
        "restriction": "alcool",
        "budget_min": 50.0,
        "fragrance_free": True,
        "spf_min": 30.0,
        "skin_type": "dry",
        "category_key": SHELF,
    }
    merged, _ = merge_constraints(stored, {}, None, carry_keys=POSITIVE)
    assert merged.get("skin_type") == "dry"
    assert not {"restriction", "budget_min", "fragrance_free", "spf_min"} & set(merged)


def test_merge_drops_carried_facets_on_topic_reset() -> None:
    stored = {"skin_type": "dry", "category_key": SHELF}
    merged, reset = merge_constraints(stored, {}, "par-ingrijirea-parului", carry_keys=POSITIVE)
    assert reset and "skin_type" not in merged


def test_hint_shows_retained_facets_and_subject() -> None:
    stack = {
        "concerns": ["hydration"],
        "skin_type": "dry",
        "category_key": SHELF,
        SUBJECT_KEY: ConversationSubject(shelf_key=SHELF, product_type=CREAM).to_dict(),
    }
    hint = _filters_hint({**stack, "restriction": "alcool"}, retained=True, positive=POSITIVE)
    assert "skin_type: dry" in hint and f"tipul de produs discutat: {CREAM}" in hint
    assert "alcool" not in hint  # o excludere nu se afișează ca cerere
    assert "skin_type" not in _filters_hint(stack, positive=POSITIVE)  # OFF = byte-identic


# --- 2. tipul de ten rămâne în căutare --------------------------------------------------------


def _ctx(stack: dict[str, Any], body: str = "sub 100 de lei sa vad") -> TurnContext:
    ctx = TurnContext(
        turn_id="t",
        business=BusinessConfig(id="b", slug="d", name="D", domain_pack=PACK),
        contact=Contact(id="c", business_id="b"),
        message=InboundMessage(provider_msg_id="m", body=body),
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


def test_turn_3_search_carries_the_skin_type(vocab_from_pack) -> None:
    ctx = _ctx(_stack())
    a = SearchArgs(
        query="cremă de hidratare pentru ten uscat, hidratare, sub 100 de lei",
        category=SHELF,
        concerns=["hydration"],
        price_max=100,
    )
    assert _carry_retained(ctx, a, CATALOG) is True
    assert a.concerns == ["hydration", "dry"]


def test_old_concerns_are_not_added_to_a_new_need(vocab_from_pack) -> None:
    """Recenzia: valorile unei dimensiuni se leagă cu SAU în SQL, deci «acnee» cărată într-o
    căutare de «pete» o LĂRGEA. Lista `concerns` nu se mai cară, doar fațetele pozitive."""
    ctx = _ctx({**_stack(), "concerns": ["acne"]})
    a = SearchArgs(query="ser pentru pete", category=SHELF, concerns=["hyperpigmentation"])
    _carry_retained(ctx, a, CATALOG)
    assert a.concerns == ["hyperpigmentation", "dry"]


def test_nothing_carried_on_another_shelf_or_without_a_shelf(vocab_from_pack) -> None:
    ctx = _ctx(_stack())
    other = SearchArgs(query="sampon", category="par-ingrijirea-parului", concerns=["dandruff"])
    assert _carry_retained(ctx, other, CATALOG) is False and other.concerns == ["dandruff"]
    no_shelf = SearchArgs(query="ceva", concerns=["hydration"])
    assert _carry_retained(ctx, no_shelf, CATALOG) is False and no_shelf.concerns == ["hydration"]


def test_a_changed_facet_is_not_carried(vocab_from_pack) -> None:
    """«ten gras» după «ten uscat»: clientul s-a răzgândit, nu cere un ten și uscat, și gras."""
    ctx = _ctx(_stack())
    a = SearchArgs(query="crema pentru ten gras", category=SHELF, concerns=["ten gras"])
    _carry_retained(ctx, a, CATALOG)
    assert a.concerns == ["ten gras"]


def test_an_exclusion_is_never_carried_as_a_need(vocab_from_pack) -> None:
    """Stiva v1 pierde operatorul: `restriction: alcool` (clientul a EXCLUS alcoolul) ar fi plecat
    în căutare ca «alcool» dorit."""
    ctx = _ctx({**_stack(), "restriction": "alcool", "budget_max": 100.0})
    a = SearchArgs(query="crema de hidratare", category=SHELF, concerns=["hydration"])
    _carry_retained(ctx, a, CATALOG)
    assert a.concerns == ["hydration", "dry"]


@pytest.mark.parametrize(
    "sent", [["hydration"], ["hydration", "dry"], ["dry"], ["hidratare", "ten uscat"]]
)
def test_needs_from_the_stack_count_as_the_clients(vocab_from_pack, sent) -> None:
    """Garda NX-313 judecă o nevoie pe cuvintele clientului; «dry» (codul canonic) nu apare literal
    în «am ten uscat». Recenzia: dacă modelul copia codurile din indiciu («dry»), nimic nu mai
    lipsea și proveniența cădea iar pe gardă."""
    ctx = _ctx(_stack())
    a = SearchArgs(query="crema de hidratare", category=SHELF, concerns=list(sent))
    assert _carry_retained(ctx, a, CATALOG) is True


def test_a_new_model_need_is_still_judged(vocab_from_pack) -> None:
    ctx = _ctx(_stack())
    a = SearchArgs(query="crema de hidratare", category=SHELF, concerns=["acne"])
    assert _carry_retained(ctx, a, CATALOG) is False
    assert "dry" in a.concerns  # cărarea s-a făcut, doar proveniența rămâne a gărzii


# --- 3. tipul subiectului: preferință și dovadă -----------------------------------------------


def test_type_fits_query() -> None:
    types = [e.key for e in CATALOG.entries("product_type")]
    stop = {"de", "pentru"}
    assert type_fits_query(["cremă de hidratare pentru ten uscat"], CREAM, types, stop)
    assert not type_fits_query(["crema contur ochi"], CREAM, types, stop)
    assert not type_fits_query(["crema de corp"], CREAM, types, stop)
    assert not type_fits_query(["măști de față"], CREAM, types, stop)


def test_subject_type_is_preferred_when_the_search_asks_for_it() -> None:
    ctx = _ctx(_stack())
    a = SearchArgs(query="cremă de hidratare pentru ten uscat", category=SHELF)
    _prefer_subject_type(ctx, a, CATALOG)
    assert a.prefer == {"product_type": [CREAM]}


@pytest.mark.parametrize("query", ["crema contur ochi", "măști de față hidratante"])
def test_no_subject_preference_for_another_type(query: str) -> None:
    """Recenzia: pe capul „crema” stau 8 tipuri; căutarea de creme de ochi primea preferința
    pentru crema de față."""
    ctx = _ctx(_stack())
    a = SearchArgs(query=query, category=SHELF)
    _prefer_subject_type(ctx, a, CATALOG)
    assert not a.prefer


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


def test_an_eye_cream_search_moves_the_subject() -> None:
    """Recenzia: comparat doar pe capul „crema”, subiectul rămânea blocat pe crema de față."""
    subject, _ = _subject_after("crema contur ochi", [EYE] * 6)
    assert subject is not None and subject.product_type == EYE


def test_a_search_for_masks_still_moves_the_subject() -> None:
    subject, _ = _subject_after("măști de față hidratante", [MASK] * 6)
    assert subject is not None and subject.product_type == MASK


def test_flag_off_is_the_old_subject_behaviour(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "needs_retained_enabled", False)
    subject, _ = _subject_after("cremă de hidratare, sub 100 de lei", [MASK] * 6)
    assert subject is not None and subject.product_type == MASK


# --- a doua recenzie ---------------------------------------------------------------------------


@pytest.mark.parametrize("body", ["am ten foarte gras", "de fapt am tenul gras"])
def test_a_spoken_correction_beats_the_carried_skin_type(vocab_from_pack, body) -> None:
    """«ten foarte gras» nu e în harta tenantului: fără citirea mesajului curent, `dry` se căra
    peste corecția clientului."""
    ctx = _ctx(_stack(), body=body)
    a = SearchArgs(query="crema de hidratare", category=SHELF, concerns=["hydration"])
    _carry_retained(ctx, a, CATALOG)
    assert "dry" not in a.concerns


def test_a_model_phrase_on_the_same_facet_blocks_the_carry(vocab_from_pack) -> None:
    ctx = _ctx(_stack(), body="si pentru ten foarte gras?")
    a = SearchArgs(query="crema", category=SHELF, concerns=["ten foarte gras"])
    _carry_retained(ctx, a, CATALOG)
    assert a.concerns == ["ten foarte gras"]


def test_a_spoken_correction_is_not_kept_in_the_stack(vocab_from_pack) -> None:
    """Valoarea veche nu se mai păstrează în stivă pe turul în care clientul numește fațeta."""
    from src.worker.stages.agent import _retained_keys

    ctx = _ctx(_stack(), body="de fapt am tenul gras")
    assert "skin_type" not in _retained_keys(ctx, CATALOG)
    quiet = _ctx(_stack(), body="sub 100 de lei sa vad")
    assert "skin_type" in _retained_keys(quiet, CATALOG)


def test_need_proposals_rehome_at_the_source(vocab_from_pack) -> None:
    """Cu scrierea stării v2 aprinsă, `concerns: ten uscat` ieșea inactivă în reducer."""
    from src.worker.stages.agent import _need_proposals

    ctx = _ctx({})
    props = _need_proposals(ctx, {"concerns": ["hydration", "ten uscat"]})
    assert [(p.key, p.value) for p in props] == [("concerns", "hydration"), ("skin_type", "dry")]


def test_need_proposals_without_the_flag_are_unchanged(vocab_from_pack, monkeypatch) -> None:
    from src.worker.stages.agent import _need_proposals

    monkeypatch.setattr(get_settings(), "needs_retained_enabled", False)
    props = _need_proposals(_ctx({}), {"concerns": ["ten uscat"]})
    assert [(p.key, p.value) for p in props] == [("concerns", "ten uscat")]


def test_body_creams_from_a_face_cream_search_do_not_move_the_subject() -> None:
    """A doua recenzie: pe capul „crema”, «cremă de hidratare» care întoarce creme de corp e tot o
    ratare. Dovadă e doar tipul subiectului sau un tip numit COMPLET în căutare."""
    subject, _ = _subject_after("cremă de hidratare", [BODY] * 6)
    assert subject is not None and subject.product_type == CREAM
    body, _ = _subject_after("crema de corp hidratanta", [BODY] * 6)
    assert body is not None and body.product_type == BODY
