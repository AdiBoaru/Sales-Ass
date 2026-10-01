"""NX-371 — «mai ieftin» pe produsul NUMIT, iar momentul rutinei din cuvintele pachetului.

Conversația c9 din 2026-10-01: rutina de seară a venit cu protecția solară de dimineață
(`moment="seara"` nu era o cheie), iar «pot să înlocuiesc tonerul cu ceva mai ieftin?» a primit o
bandă de nas de 3 lei: pragul era cel mai ieftin card de pe ecranul curent (30 de lei), nu tonerul
numit (110 lei, de pe ecranul de dinainte).
"""

from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from src.agent import planner
from src.catalog.query_terms import content_terms, inflection_suffixes
from src.domain.routine_steps import RoutineSpec
from src.models import Author, Direction, Message, ProductRef

SUFFIXES = inflection_suffixes("ro")


def _tokens(text: str) -> list[str]:
    return content_terms(text, "ro")


@pytest.mark.parametrize(
    "message, type_key, named",
    [
        ("pot sa inlocuiesc tonerul cu ceva mai ieftin?", "toner de fata", True),
        ("ai un toner mai ieftin?", "toner de fata", True),
        ("ceva mai ieftin decat serul?", "ser de fata", True),
        ("serurile astea sunt scumpe", "ser de fata", True),
        ("ceva mai ieftin?", "toner de fata", False),
        ("tonifiant mai ieftin", "toner de fata", False),  # alt cuvânt, nu flexiune
        ("serios, ceva mai ieftin", "ser de fata", False),  # „serios" nu e „ser" + sufix
    ],
)
def test_type_named_uses_locale_inflection_not_free_prefix(message, type_key, named):
    assert planner.type_named(_tokens(message), type_key, SUFFIXES) is named


def test_shown_refs_include_earlier_screens_most_recent_first():
    ctx = NS(
        state=NS(displayed_products=[ProductRef("s1", "Ser", 30.0)]),
        history=[
            Message(
                direction=Direction.OUTBOUND,
                author=Author.BOT,
                body="rutina",
                payload={"shown": [{"product_id": "t1", "name": "Toner", "price": 110.0}]},
            ),
            Message(direction=Direction.INBOUND, author=Author.CONTACT, body="max 200"),
            Message(
                direction=Direction.OUTBOUND,
                author=Author.BOT,
                body="doua produse",
                payload={"shown": [{"product_id": "s1", "name": "Ser", "price": 30.0}]},
            ),
        ],
    )
    assert planner._shown_refs(ctx) == [("s1", 30.0), ("t1", 110.0)]


class _Conn:
    def __init__(self, types):
        self.types = types

    async def fetch(self, sql, business_id, ids):
        assert business_id == "biz"  # izolare: query-ul poartă tenantul
        return [{"id": i, "product_type": self.types.get(i)} for i in ids if i in self.types]


def _deps(types):
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def db(op="x"):
        yield _Conn(types)

    return NS(db=db)


def _ctx(body, displayed, history=()):
    return NS(
        message=NS(body=body),
        language="ro",
        business=NS(id="biz"),
        state=NS(displayed_products=displayed),
        history=list(history),
    )


async def test_named_anchor_finds_the_toner_on_an_earlier_screen():
    history = [
        Message(
            direction=Direction.OUTBOUND,
            author=Author.BOT,
            body="rutina",
            payload={"shown": [{"product_id": "t1", "name": "ANUA Heartleaf", "price": 110.0}]},
        )
    ]
    ctx = _ctx(
        "pot sa inlocuiesc tonerul cu ceva mai ieftin?",
        [ProductRef("s1", "SOME BY MI Retinol", 30.0)],
        history,
    )
    anchor = await planner._named_anchor(ctx, _deps({"t1": "toner de fata", "s1": "ser de fata"}))
    assert anchor == ([("t1", 110.0)], "toner de fata")


async def test_no_type_named_keeps_the_screen_anchor():
    ctx = _ctx("ceva mai ieftin?", [ProductRef("s1", "Ser", 30.0)])
    assert await planner._named_anchor(ctx, _deps({"s1": "ser de fata"})) is None


async def test_two_types_named_is_ambiguous():
    ctx = _ctx(
        "si tonerul si serul mai ieftine",
        [ProductRef("s1", "Ser", 30.0), ProductRef("t1", "Toner", 110.0)],
    )
    deps = _deps({"s1": "ser de fata", "t1": "toner de fata"})
    assert await planner._named_anchor(ctx, deps) is None


def test_routine_moment_from_declared_words():
    spec = RoutineSpec(
        families={},
        by_product_type={},
        time_markers={"am": ("dimineata", "zi"), "pm": ("seara", "noapte")},
    )
    assert spec.moment_key("pm") == "pm"
    assert spec.moment_key("seara") == "pm"
    assert spec.moment_key("Seară") == "pm"
    assert spec.moment_key("dimineata") == "am"
    assert spec.moment_key("pranz") is None
    assert spec.moment_key(None) is None


async def test_anchor_lookup_failure_falls_back_to_the_screen():
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def broken(op="x"):
        raise RuntimeError("pool plin")
        yield  # pragma: no cover

    events = []
    ctx = _ctx("tonerul mai ieftin", [ProductRef("s1", "Ser", 30.0)])
    ctx.emit = lambda kind, **p: events.append((kind, p))
    assert await planner._named_anchor(ctx, NS(db=broken)) is None
    assert events == [("cheaper_anchor_unavailable", {"cause": "RuntimeError"})]
