"""NX-354 — prețul devenit filtru nu se mai cere o dată ca text și nu mai decide ordinea.

Conversația `1748f988` (`sole-ro`, 2026-09-29): la «sub 100 de lei sa vad», după o cremă pentru ten
uscat, modelul a căutat corect `"cremă de hidratare pentru ten uscat, hidratare, sub 100 de lei"`
cu `price_max=100` și `price_asc`. `100` și `lei` au devenit termeni obligatorii, strictul a dat
zero, `relaxed` a adus 96 de produse (54 de măști), iar sortarea pe preț a pus măștile de 10 lei
primele. Măsurat: 4 din 6 căutări salvate cu plafon aveau prețul și în text.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from src.catalog.query_terms import content_terms
from src.catalog.vocabulary import CatalogVocabulary
from src.config import get_settings
from src.domain.constraints import EMPTY_UNITS, build_units, strip_price_mentions
from src.domain.loader import load_domain_pack
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
from src.tools import catalog_tools as ct
from src.tools.catalog_tools import SearchArgs, _price_as_filter_only, search_products_tool
from src.worker.runner import PipelineDeps

UNITS = build_units(
    {
        "price": {"factors": {"lei": 1, "ron": 1, "bani": 0.01}, "canonical": "lei"},
        "volume": {"factors": {"ml": 1, "l": 1000}, "canonical": "ml"},
        "spf": {"factors": {"spf": 1, "ip": 1}, "canonical": "spf"},
    }
)


# --- funcția pură ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("query", "terms"),
    [
        # conversația 1748f988, turul 3: exact textul salvat în `active_search`
        (
            "cremă de hidratare pentru ten uscat, hidratare, sub 100 de lei",
            ["crema", "hidratare", "ten", "uscat"],
        ),
        # celelalte trei din măsurătoare
        ("cadou sub 100 lei pentru ea sau el", ["cadou"]),
        (
            "ser pentru piele uscată, hidratare și calmare, mai ieftin decât 70 lei",
            ["ser", "piele", "uscata", "hidratare", "calmare"],
        ),
        ("crema spf 50 sub 80 lei", ["crema", "spf", "50"]),  # SPF-ul e cerință de produs
        ("crema 50 ml sub 100", ["crema", "50", "ml"]),  # cantitatea rămâne, suma iese
        ("crema de fata", ["crema", "fata"]),  # fără preț: neatins
    ],
)
def test_price_words_leave_the_text(query: str, terms: list[str]) -> None:
    assert content_terms(strip_price_mentions(query, units=UNITS, locale="ro"), "ro") == terms


def test_without_units_the_text_is_untouched() -> None:
    """Fără registru de unități nu putem deosebi un preț de o cantitate: nu ghicim."""
    q = "crema sub 100 lei"
    assert strip_price_mentions(q, units=EMPTY_UNITS, locale="ro") == q


def test_unknown_locale_keeps_comparison_words() -> None:
    """P11: frazele de comparație sunt ale limbii; pe o locale necunoscută nu aplicăm româna.
    Suma și unitatea de bani (ale tenantului) ies oricum."""
    out = strip_price_mentions("crema mai ieftin 100 lei", units=UNITS, locale="xx")
    assert "100" not in out and "lei" not in out and "ieftin" in out


# --- legătura din unealtă -------------------------------------------------------------------------


def _pack():
    base = load_domain_pack(BusinessConfig(id="b", slug="d", name="D", vertical="ecommerce"))
    return dataclasses.replace(base, units=UNITS)


def _ctx(body: str, *, history: tuple[str, ...] = (), units: bool = True) -> TurnContext:
    return TurnContext(
        turn_id="t",
        business=BusinessConfig(id="b", slug="d", name="D", domain_pack=_pack() if units else None),
        contact=Contact(id="c", business_id="b"),
        message=InboundMessage(provider_msg_id="m", body=body),
        conversation_id="conv",
        history=[
            Message(direction=Direction.INBOUND, author=Author.CONTACT, body=h) for h in history
        ],
        state=ConversationState(),
    )


def _events(ctx: TurnContext, name: str) -> list[dict[str, Any]]:
    return [
        {k: v for k, v in e.properties.items() if k != "turn_id"}
        for e in ctx.events
        if e.type == name
    ]


def test_turn_3_of_1748f988_query_and_sort() -> None:
    ctx = _ctx("sub 100 de lei sa vad", history=("vreau o crema de hidratare", "am ten uscat"))
    a = SearchArgs(
        query="cremă de hidratare pentru ten uscat, hidratare, sub 100 de lei",
        price_max=100,
        sort_mode="price_asc",
    )
    _price_as_filter_only(ctx, a, planned=False)
    assert content_terms(a.query, "ro") == ["crema", "hidratare", "ten", "uscat"]
    assert a.sort_mode == "relevance"
    assert _events(ctx, "query_price_words") == [{"outcome": "stripped", "removed": 2}]
    assert _events(ctx, "price_sort_dropped") == [{"reason": "budget_is_not_cheapest"}]


@pytest.mark.parametrize("message", ["si ceva mai ieftin ?", "care e cea mai ieftina sub 100?"])
def test_a_real_cheaper_request_keeps_price_sort(message: str) -> None:
    ctx = _ctx(message)
    a = SearchArgs(query="crema de fata", price_max=100, sort_mode="price_asc")
    _price_as_filter_only(ctx, a, planned=False)
    assert a.sort_mode == "price_asc"
    assert not _events(ctx, "price_sort_dropped")


def test_planned_sort_is_the_planners() -> None:
    """NX-333: pe calea planificată sortarea e a plannerului, nu a modelului."""
    ctx = _ctx("sub 100 lei")
    a = SearchArgs(query="crema", price_max=100, sort_mode="price_asc")
    _price_as_filter_only(ctx, a, planned=True)
    assert a.sort_mode == "price_asc"


def test_nothing_left_keeps_the_original_text() -> None:
    ctx = _ctx("sub 100 lei")
    a = SearchArgs(query="sub 100 lei", price_max=100)
    _price_as_filter_only(ctx, a, planned=False)
    assert a.query == "sub 100 lei"
    assert _events(ctx, "query_price_words") == [{"outcome": "kept_nothing_left", "removed": 0}]


# --- prin unealta reală: ce ajunge în SQL ---------------------------------------------------------


class _LLM:
    async def embed(self, texts, *, model=None):
        return [[0.0] * 8 for _ in texts]


@pytest.fixture
def lexical_calls(monkeypatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def fake_lexical(conn, business_id, *, query_text, **kwargs):
        calls.append({"query": query_text, **kwargs})
        return [
            {"id": f"cr{i}", "name": f"Crema {i}", "price": 60.0, "lexical_step": "strict"}
            for i in range(8)
        ]

    async def no_embeddings(conn, business_id):
        return False

    async def empty_vocab(deps, business_id):
        return CatalogVocabulary(business_id="b", dimensions={})

    monkeypatch.setattr(ct, "search_products_lexical", fake_lexical)
    monkeypatch.setattr(ct, "has_embeddings", no_embeddings)
    monkeypatch.setattr(ct, "fuse_candidates", lambda lex, vec, **k: list(lex))
    monkeypatch.setattr(ct, "get_vocabulary", empty_vocab)
    return calls


async def _search(ctx: TurnContext) -> None:
    await search_products_tool(
        ctx,
        PipelineDeps(conn=object(), redis=None, llm=_LLM()),
        {
            "query": "cremă de hidratare pentru ten uscat, hidratare, sub 100 de lei",
            "price_max": 100,
            "sort_mode": "price_asc",
        },
    )


async def test_sql_gets_the_clean_text_and_relevance(lexical_calls) -> None:
    ctx = _ctx("sub 100 de lei sa vad", history=("vreau o crema de hidratare", "am ten uscat"))
    await _search(ctx)
    assert lexical_calls
    first = lexical_calls[0]
    assert content_terms(first["query"], "ro") == ["crema", "hidratare", "ten", "uscat"]
    assert first["price_max"] == 100
    assert first["sort_mode"] == "relevance"


async def test_flag_off_is_the_old_behaviour(lexical_calls, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "search_price_as_filter_only_enabled", False)
    ctx = _ctx("sub 100 de lei sa vad", history=("vreau o crema de hidratare", "am ten uscat"))
    await _search(ctx)
    first = lexical_calls[0]
    assert first["query"] == "cremă de hidratare pentru ten uscat, hidratare, sub 100 de lei"
    assert first["sort_mode"] == "price_asc"
    assert not _events(ctx, "query_price_words") and not _events(ctx, "price_sort_dropped")
