"""NX-315 felia 2 — o întrebare de îngustare ALĂTURI de produse, pe o fațetă aleasă de server.

Turul real «vreau o crema de hidratare» (2026-09-23): iZi a răspuns cu o frază, o întrebare („te
interesează mai mult pentru ten uscat, mixt/gras sau sensibil?") și cardurile. Nativx: cardurile,
nicio întrebare. Modelul nu știa CE merită întrebat, iar pe calea v1 nu avea nici unde s-o pună.

Acum serverul alege fațeta (partiționantă, nerostită, neîntrebată, cu ≥2 valori în setul servit și
câștig informațional peste pragul NX-235), iar modelul poate doar s-o formuleze. Poarta cere ca
întrebarea să numească opțiunile oferite. Turul are cel mult o întrebare.
"""

from __future__ import annotations

import pytest

from src.agent import answer_shape
from src.agent import finalize as finalize_mod
from src.agent.finalize import render
from src.agent.planner import ResponsePlan
from src.agent.prompt_builder import PromptInputs
from src.catalog.vocabulary import CatalogVocabulary, VocabEntry
from src.config import get_settings
from src.conversation.clarification_policy import (
    NARROWING_REASONS,
    narrowing_candidate,
)
from src.domain.facets import build_facets
from src.domain.pack import DomainPack, FacetSpec
from src.models import (
    Author,
    BusinessConfig,
    Contact,
    ConversationState,
    Direction,
    InboundMessage,
    Message,
    TurnContext,
)
from src.worker import compose
from src.worker.runner import PipelineDeps

INP = PromptInputs.build("S", "ecommerce", "ro", ["Ten"], [])

_FACETS = build_facets(
    [
        {
            "key": "skin_type",
            "value_type": "enum",
            "source": "attribute",
            "source_key": "skin_type",
            "operators": ["eq"],
            "binding": "partitioning",
            "values": ["oily", "dry", "combination", "sensitive", "normal"],
            "labels": {"ro": "Tip de ten"},
        },
        {
            "key": "concerns",
            "value_type": "list",
            "source": "attribute",
            "source_key": "concerns",
            "operators": ["contains"],
            "binding": "additive",
            "labels": {"ro": "Potrivit pentru"},
        },
    ]
)
PACK = DomainPack(
    vertical="ecommerce",
    concern_map={"ten uscat": "dry", "ten gras": "oily", "ten sensibil": "sensitive"},
    facets=_FACETS,
    comparison_facets=(FacetSpec(key="skin_type", labels={"ro": "Tip de ten"}),),
)
VOCAB = CatalogVocabulary(
    business_id="b",
    dimensions={
        "skin_type": (
            VocabEntry(key="dry", label="dry", count=429),
            VocabEntry(key="oily", label="oily", count=318),
            VocabEntry(key="sensitive", label="sensitive", count=200),
        )
    },
)


def _cream(i: int, skin: str, price: float = 50.0) -> dict:
    return {
        "id": f"p{i}",
        "name": f"Crema {i}",
        "price": price,
        "url": f"https://shop/p{i}",
        "availability": "in_stock",
        "attributes": {"skin_type": skin, "product_type": "crema de fata"},
    }


# Setul turului 1: șase creme, trei tipuri de ten, două pe fiecare.
SIX_CREAMS = [_cream(i, skin) for i, skin in enumerate(["dry", "oily", "sensitive"] * 2, 1)]


# --- alegerea fațetei (PUR) ---------------------------------------------------------------------


def test_turn_one_set_offers_skin_type():
    """DoD: pe setul turului 1 (6 creme, `skin_type` nerostit, 3 valori în set) oferta e
    `skin_type`, cu câștigul calculat pe setul SERVIT."""
    verdict = narrowing_candidate(_FACETS, SIX_CREAMS)
    assert verdict.reason == "offered"
    assert verdict.offer is not None
    assert verdict.offer.facet == "skin_type"
    assert set(verdict.offer.values) == {"dry", "oily", "sensitive"}
    assert verdict.offer.partition == (2, 2, 2)
    assert verdict.offer.gain > 0.30


def test_single_value_set_offers_nothing():
    """DoD: o întrebare cu un singur răspuns posibil nu îngustează nimic."""
    same = [_cream(i, "dry") for i in range(1, 5)]
    verdict = narrowing_candidate(_FACETS, same)
    assert verdict.offer is None
    assert verdict.reason == "single_value"


def test_already_said_facet_is_not_offered():
    """Clientul a spus deja „am ten uscat" ⇒ nu-l punem să repete."""
    verdict = narrowing_candidate(_FACETS, SIX_CREAMS, known=frozenset({"skin_type"}))
    assert verdict.offer is None
    assert verdict.reason == "known"


def test_already_asked_facet_is_not_offered_again():
    verdict = narrowing_candidate(_FACETS, SIX_CREAMS, asked=frozenset({"skin_type"}))
    assert verdict.offer is None
    assert verdict.reason == "asked"


def test_additive_facet_is_never_a_narrowing_question():
    """„Hidratare și luminozitate" e un răspuns valid la o fațetă additive, deci întrebarea n-ar
    tăia nimic. Doar `partitioning` se întreabă."""
    additive_only = tuple(f for f in _FACETS if f.binding == "additive")
    verdict = narrowing_candidate(additive_only, SIX_CREAMS)
    assert verdict.offer is None
    assert verdict.reason == "no_partitioning_facet"


def test_pack_without_facets_changes_nothing():
    """Failure case din card: pachet fără fațete partiționante ⇒ zero oferte."""
    assert narrowing_candidate((), SIX_CREAMS).reason == "no_partitioning_facet"


def test_low_gain_is_not_asked():
    """Cinci din șase pe aceeași valoare: răspunsul aproape sigur nu schimbă nimic."""
    skewed = [_cream(i, "dry") for i in range(1, 6)] + [_cream(6, "oily")]
    verdict = narrowing_candidate(_FACETS, skewed)
    assert verdict.offer is None
    assert verdict.reason == "low_gain"


def test_too_many_values_is_a_form_not_a_question():
    many = [_cream(i, s) for i, s in enumerate(["dry", "oily", "sensitive", "normal", "x"], 1)]
    verdict = narrowing_candidate(_FACETS, many)
    assert verdict.offer is None
    assert verdict.reason == "too_many_values"


def test_reasons_come_from_a_closed_vocabulary():
    cases = [
        narrowing_candidate(_FACETS, SIX_CREAMS),
        narrowing_candidate(_FACETS, [_cream(1, "dry")]),
        narrowing_candidate((), SIX_CREAMS),
        narrowing_candidate(_FACETS, SIX_CREAMS, known=frozenset({"skin_type"})),
    ]
    assert all(v.reason in NARROWING_REASONS for v in cases)


# --- poarta pe întrebarea scrisă de model (PUR) ------------------------------------------------

_PHRASES = ("ten uscat", "ten gras", "ten sensibil")


def test_natural_question_passes_without_repeating_the_subject():
    """Un om întreabă „ai tenul uscat, gras sau sensibil?", nu „ten uscat, ten gras…". Poarta
    scade subiectul comun, deci fraza firească trece."""
    text, reason = answer_shape.judge_question("Ai tenul uscat, gras sau sensibil?", _PHRASES)
    assert reason is None and text == "Ai tenul uscat, gras sau sensibil?"


def test_inflected_options_still_count():
    text, reason = answer_shape.judge_question(
        "Pielea ta e mai degrabă uscată sau grasă?", _PHRASES
    )
    assert reason is None and text


def test_question_on_another_facet_is_rejected():
    """Failure case din card: modelul întreabă altceva decât s-a oferit ⇒ întrebarea se scoate."""
    assert answer_shape.judge_question("Vrei ceva cu SPF?", _PHRASES) == (None, "off_target")


def test_confirmation_question_is_not_a_narrowing_question():
    """„Ai tenul uscat?" numește o singură opțiune: confirmă, nu desparte setul."""
    assert answer_shape.judge_question("Ai tenul uscat?", _PHRASES)[1] == "off_target"


def test_two_questions_are_not_one():
    reason = answer_shape.judge_question("Ai tenul uscat? Sau gras?", _PHRASES)[1]
    assert reason == "not_a_question"


def test_empty_question_is_no_question():
    assert answer_shape.judge_question(None, _PHRASES) == (None, "no_question")
    assert answer_shape.judge_question("  ", _PHRASES) == (None, "no_question")


def test_rejections_come_from_a_closed_vocabulary():
    for q in (None, "Ai tenul uscat? Sau gras?", "Vrei SPF?"):
        assert answer_shape.judge_question(q, _PHRASES)[1] in answer_shape.QUESTION_REJECTIONS


def test_strip_questions_keeps_the_rest_of_the_paragraph():
    out = compose.strip_questions("Am ales creme hidratante. Vrei ceva anume? Toate sunt ușoare.")
    assert out == "Am ales creme hidratante. Toate sunt ușoare."
    assert compose.strip_questions("Vrei ceva anume?") is None


# --- cablajul în compunerea rich (v1, calea din producție) --------------------------------------


class _LLM:
    def __init__(self, rich: dict):
        self._rich = rich
        self.calls: list[tuple[str, str, dict]] = []

    async def embed(self, texts, *, model=None):
        return [[0.0] * 8 for _ in texts]

    async def complete(self, system, user, *, model=None):
        return "Uite câteva creme."

    async def complete_schema(self, system, user, schema, *, model=None):
        self.calls.append((system, user, schema))
        return self._rich


def _ctx(body: str = "vreau o crema de hidratare", history: list | None = None) -> TurnContext:
    ctx = TurnContext(
        turn_id="t",
        business=BusinessConfig(id="b", slug="s", name="S"),
        contact=Contact(id="c", business_id="b"),
        message=InboundMessage(provider_msg_id="m", body=body),
        conversation_id="conv",
        state=ConversationState(),
        history=history or [],
    )
    ctx.language = "ro"
    ctx.business.domain_pack = PACK
    return ctx


def _plan(products: list[dict]) -> ResponsePlan:
    return ResponsePlan(
        handled=False,
        products=[dict(p) for p in products],
        final="",
        is_order=False,
        query="vreau o crema de hidratare",
        history="",
        inp=INP,
        mode="rich",
    )


def _rich_json(question: str | None, intro: str = "Am ales creme hidratante.") -> dict:
    return {
        "intro": intro,
        "items": [
            {"product_id": p["id"], "pro_index": 0, "fit_clause": "ușoară"} for p in SIX_CREAMS
        ],
        "pick": None,
        "education": "Alege după cât de uscat e tenul. Vrei și ceva de noapte?",
        "suggestions": [],
        "question": question,
    }


@pytest.fixture
def narrowing_on(monkeypatch):
    monkeypatch.setattr(get_settings(), "narrowing_question_enabled", True)

    async def _vocab(deps, business_id):
        return VOCAB

    monkeypatch.setattr("src.catalog.vocabulary_cache.get_vocabulary", _vocab)


async def test_offer_reaches_the_model_and_the_question_leaves_as_the_only_one(narrowing_on):
    """Happy path din card: o întrebare pe `skin_type`, cardurile neschimbate, iar în textul
    servit iese UNA singură (DoD: cel mult o întrebare pe tur)."""
    llm = _LLM(_rich_json("Ai tenul mai degrabă uscat, gras sau sensibil?"))
    ctx = _ctx()
    await render(ctx, PipelineDeps(conn=object(), redis=None, llm=llm), _plan(SIX_CREAMS))

    _, user, schema = llm.calls[0]
    assert "Tip de ten" in user and "ten uscat" in user
    assert "question" in schema["schema"]["properties"]

    rich = ctx.reply.rich
    assert rich is not None and len(rich.items) == 6
    assert rich.intro.endswith("uscat, gras sau sensibil?")
    served = compose.flatten_framing(rich, "ro")
    assert served.count("?") == 1  # întrebarea din `education` a fost scoasă
    assert "skin_type" in ctx.state.asked_intents  # nu se mai întreabă a doua oară

    event = next(e for e in ctx.events if e.type == "narrowing_offer")
    assert event.properties["asked"] is True
    assert event.properties["facet"] == "skin_type"
    assert event.properties["rejected_reason"] is None


async def test_off_target_question_is_dropped_and_the_rest_stays(narrowing_on):
    llm = _LLM(_rich_json("Vrei ceva și cu SPF?"))
    ctx = _ctx()
    await render(ctx, PipelineDeps(conn=object(), redis=None, llm=llm), _plan(SIX_CREAMS))

    rich = ctx.reply.rich
    assert rich.intro == "Am ales creme hidratante."
    assert len(rich.items) == 6
    assert "skin_type" not in ctx.state.asked_intents
    event = next(e for e in ctx.events if e.type == "narrowing_offer")
    assert event.properties["asked"] is False
    assert event.properties["rejected_reason"] == "off_target"


async def test_client_who_said_the_value_is_not_asked_again(narrowing_on):
    """Edge case din card: „am ten uscat" ⇒ `skin_type` nu se oferă, iar schema rămâne cea
    obișnuită (fără câmpul întrebării)."""
    history = [Message(direction=Direction.INBOUND, author=Author.CONTACT, body="am ten uscat")]
    llm = _LLM(_rich_json(None))
    ctx = _ctx(history=history)
    await render(ctx, PipelineDeps(conn=object(), redis=None, llm=llm), _plan(SIX_CREAMS))

    _, user, schema = llm.calls[0]
    assert "question" not in schema["schema"]["properties"]
    assert "O singură întrebare" not in user  # oferta lipsește (axele NX-139 rămân, sunt altceva)
    event = next(e for e in ctx.events if e.type == "narrowing_offer")
    assert event.properties["rejected_reason"] == "known"


async def test_explain_turn_never_gets_a_narrowing_question(narrowing_on):
    """Întrebarea se aplică doar pe `recommend`, niciodată pe `explain`, `answer` sau `compare`."""
    llm = _LLM(_rich_json(None))
    ctx = _ctx(body="cum se foloseste prima")
    await render(ctx, PipelineDeps(conn=object(), redis=None, llm=llm), _plan(SIX_CREAMS))

    _, user, schema = llm.calls[0]
    assert "question" not in schema["schema"]["properties"]
    assert not any(e.type == "narrowing_offer" for e in ctx.events)


async def test_vocabulary_outage_fails_open_to_todays_turn(monkeypatch):
    monkeypatch.setattr(get_settings(), "narrowing_question_enabled", True)

    async def _down(deps, business_id):
        raise RuntimeError("db down")

    monkeypatch.setattr("src.catalog.vocabulary_cache.get_vocabulary", _down)
    llm = _LLM(_rich_json(None))
    ctx = _ctx()
    await render(ctx, PipelineDeps(conn=object(), redis=None, llm=llm), _plan(SIX_CREAMS))
    assert ctx.reply.rich is not None and len(ctx.reply.rich.items) == 6
    event = next(e for e in ctx.events if e.type == "narrowing_offer")
    assert event.properties["rejected_reason"] == "vocabulary_unavailable"


def test_with_question_schema_is_a_separate_object():
    """Schema obișnuită rămâne neatinsă: turele fără ofertă trimit exact `_RICH_SCHEMA`."""
    base = finalize_mod._RICH_SCHEMA["schema"]
    assert "question" not in base["properties"]
    assert "question" not in base["required"]
    extended = finalize_mod._RICH_SCHEMA_WITH_QUESTION["schema"]
    assert "question" in extended["properties"] and "question" in extended["required"]
