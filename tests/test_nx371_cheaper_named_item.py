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
from src.domain.routine_steps import RoutineSpec
from src.models import Author, Direction, Message, ProductRef


@pytest.mark.parametrize(
    "message, type_key, named",
    [
        ("pot sa inlocuiesc tonerul cu ceva mai ieftin?", "toner de fata", "inflected"),
        ("ai un toner mai ieftin?", "toner de fata", "bare"),
        ("ceva mai ieftin decat serul?", "ser de fata", "inflected"),
        ("serurile astea sunt scumpe", "ser de fata", "inflected"),
        ("ceva mai ieftin?", "toner de fata", None),
        ("nu vreau ser, ceva mai ieftin", "ser de fata", None),  # H1: negat
        ("in fond as vrea ceva mai ieftin", "fond de ten", None),  # H2: locutiune
        ("doua seri la rand", "ser de fata", None),  # H2: seri != ser + i
        ("tonifiant mai ieftin", "toner de fata", None),  # alt cuvânt, nu flexiune
        ("serios, ceva mai ieftin", "ser de fata", None),  # „serios" nu e „ser" + sufix
    ],
)
def test_type_mention_uses_locale_inflection_not_free_prefix(message, type_key, named):
    assert planner.type_mention(message, type_key, "ro") == named


def test_shown_screens_are_most_recent_first():
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
    assert planner._shown_screens(ctx) == [[("s1", 30.0)], [("s1", 30.0)], [("t1", 110.0)]]


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


# ── Recenzia adversarială (H1-H3, M1-M2, L1) ────────────────────────────────────────────────────


def _bot(*shown):
    return Message(
        direction=Direction.OUTBOUND,
        author=Author.BOT,
        body="ecran",
        payload={"shown": [{"product_id": i, "name": i, "price": p} for i, p in shown]},
    )


@pytest.mark.parametrize(
    "message",
    [
        "nu vreau ser, ceva mai ieftin",  # H1: „nu" cădea din `content_terms`
        "nu ser, ceva mai ieftin",
    ],
)
async def test_h1_a_negated_type_is_not_the_anchor(message):
    ctx = _ctx(message, [ProductRef("m1", "Masca", 20.0)], [_bot(("s1", 90.0))])
    assert await planner._named_anchor(ctx, _deps({"s1": "ser de fata", "m1": "masca"})) is None


async def test_h1_fara_is_a_negation_too():
    ctx = _ctx("fara luciu, ceva mai ieftin", [ProductRef("r1", "Ruj", 20.0)], [_bot(("l1", 60.0))])
    deps = _deps({"l1": "luciu de buze", "r1": "ruj"})
    assert await planner._named_anchor(ctx, deps) is None


async def test_h1_a_negation_in_another_clause_does_not_cancel_the_type():
    screen, history = [ProductRef("s1", "Ser", 30.0)], [_bot(("t1", 110.0))]
    ctx = _ctx("nu stiu, tonerul mai ieftin?", screen, history)
    got = await planner._named_anchor(ctx, _deps({"s1": "ser de fata", "t1": "toner de fata"}))
    assert got == ([("t1", 110.0)], "toner de fata")


@pytest.mark.parametrize(
    "message, type_key",
    [
        ("in fond as vrea ceva mai ieftin", "fond de ten"),  # H2: locuțiune, nu produsul
        ("in esenta, ceva mai ieftin", "esenta de fata"),
    ],
)
async def test_h2_a_bare_head_after_a_preposition_is_an_idiom(message, type_key):
    ctx = _ctx(message, [ProductRef("x1", "X", 20.0)], [_bot(("f1", 150.0))])
    assert await planner._named_anchor(ctx, _deps({"f1": type_key, "x1": "rimel"})) is None


async def test_h2_a_one_letter_suffix_does_not_stretch_a_three_letter_head():
    # „seri" (plural de la „seară") ≠ „ser" + „i".
    ctx = _ctx(
        "doua seri la rand, ceva mai ieftin", [ProductRef("m1", "M", 20.0)], [_bot(("s1", 90.0))]
    )
    assert await planner._named_anchor(ctx, _deps({"s1": "ser de fata", "m1": "masca"})) is None


async def test_h2_a_bare_mention_never_raises_the_threshold_of_the_current_screen():
    # Tipul e pe ecranul curent, numit doar prin cuvântul de bază: pragul nu urcă peste minimul
    # ecranului (ancora de azi). Forma articulată („tonerul") rămâne ancoră (cazul rutinei).
    screen = [ProductRef("s1", "Ser", 30.0), ProductRef("t1", "Toner", 110.0)]
    types = {"s1": "ser de fata", "t1": "toner de fata"}
    bare = _ctx("ai un toner mai ieftin?", screen)
    assert await planner._named_anchor(bare, _deps(types)) is None
    got = await planner._named_anchor(_ctx("tonerul mai ieftin?", screen), _deps(types))
    assert got == ([("t1", 110.0)], "toner de fata")


async def test_h2_a_bare_mention_of_a_type_from_an_earlier_screen_still_anchors():
    ctx = _ctx("ai un toner mai ieftin?", [ProductRef("s1", "Ser", 30.0)], [_bot(("t1", 110.0))])
    got = await planner._named_anchor(ctx, _deps({"s1": "ser de fata", "t1": "toner de fata"}))
    assert got == ([("t1", 110.0)], "toner de fata")


async def test_h3_the_threshold_is_the_most_recent_screen_with_the_type():
    # Tonerul de 60 (ecran vechi) nu coboară pragul tonerului de 110 arătat după el.
    history = [_bot(("t_old", 60.0)), _bot(("t_new", 110.0), ("s1", 30.0))]
    ctx = _ctx("tonerul mai ieftin?", [ProductRef("s1", "Ser", 30.0)], history)
    types = {"t_old": "toner de fata", "t_new": "toner de fata", "s1": "ser de fata"}
    assert await planner._named_anchor(ctx, _deps(types)) == ([("t_new", 110.0)], "toner de fata")


# ── resolve_cheaper_followup: subiectul, kill-switch-ul și excluderea ─────────────────────────


class _RecConn:
    """Răspunde la citirea tipurilor (ancora), apoi înregistrează căutarea."""

    def __init__(self, types):
        self.types = types
        self.calls = []

    async def fetch(self, sql, *args):
        if "->> 'product_type' as product_type" in sql:
            ids = [i for i in args[1] if i in self.types]
            return [{"id": i, "product_type": self.types[i]} for i in ids]
        self.calls.append((sql, args))
        return []


def _rec_deps(conn):
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def db(op="x"):
        yield conn

    return NS(db=db)


class _Policy:
    def gate(self, ctx, products, purpose):  # noqa: ARG002
        return (products,)


def _turn(body, stored=None):
    from src.conversation.subject import SUBJECT_KEY
    from src.models import BusinessConfig, Contact, InboundMessage, TurnContext

    ctx = TurnContext(
        turn_id="t2",
        business=BusinessConfig(id="biz", slug="demo", name="Demo"),
        contact=Contact(id="c1", business_id="biz"),
        message=InboundMessage(provider_msg_id="m2", body=body),
        conversation_id="conv1",
    )
    ctx.language = "ro"
    ctx.state.displayed_products = [ProductRef("s1", "Ser", 30.0), ProductRef("m1", "Masca", 15.0)]
    ctx.history = [_bot(("t1", 110.0))]
    if stored is not None:
        ctx.state.search_constraints = {SUBJECT_KEY: stored}
    return ctx


_TYPES = {"s1": "ser de fata", "m1": "masca", "t1": "toner de fata"}


@pytest.fixture
def _flags(monkeypatch):
    from src.config import get_settings

    def _set(subject: bool, anchor: bool = True) -> None:
        monkeypatch.setattr(get_settings(), "conversation_subject_enabled", subject)
        monkeypatch.setattr(get_settings(), "cheaper_named_anchor_enabled", anchor)

    return _set


def _capture_subject(monkeypatch):
    seen = {}

    async def fake(conn, business_id, ref_ids, baseline, **kw):  # noqa: ARG001
        seen.update(kw, ref_ids=ref_ids, baseline=baseline)
        return []

    monkeypatch.setattr(planner, "search_cheaper_than", fake)
    return seen


async def test_m1_the_stored_needs_survive_and_the_old_shelf_goes_with_the_old_type(
    _flags, monkeypatch
):
    from src.conversation.subject import ConversationSubject

    _flags(True)
    seen = _capture_subject(monkeypatch)
    stored = {"shelf": "ten", "type": "ser de fata", "needs": [["skin_type", "dry"]]}
    await planner.resolve_cheaper_followup(
        _turn("tonerul mai ieftin?", stored), _rec_deps(_RecConn(_TYPES)), policy=_Policy()
    )
    assert seen["subject"] == ConversationSubject(
        product_type="toner de fata", needs=(("skin_type", "dry"),)
    )


async def test_m1_the_shelf_stays_when_the_named_type_is_the_stored_one(_flags, monkeypatch):
    from src.conversation.subject import ConversationSubject

    _flags(True)
    seen = _capture_subject(monkeypatch)
    stored = {"shelf": "ten", "type": "toner de fata", "needs": [["skin_type", "dry"]]}
    await planner.resolve_cheaper_followup(
        _turn("tonerul mai ieftin?", stored), _rec_deps(_RecConn(_TYPES)), policy=_Policy()
    )
    assert seen["subject"] == ConversationSubject(
        shelf_key="ten", product_type="toner de fata", needs=(("skin_type", "dry"),)
    )


async def test_m2_subject_kill_switch_sends_no_subject_even_with_a_named_anchor(_flags):
    from src.db.queries.catalog import search_cheaper_than

    _flags(False)
    conn = _RecConn(_TYPES)
    ctx = _turn("tonerul mai ieftin?", {"type": "ser de fata"})
    await planner.resolve_cheaper_followup(ctx, _rec_deps(conn), policy=_Policy())
    sql, args = conn.calls[0]
    ref = _RecConn({})
    await search_cheaper_than(ref, "biz", ["t1"], 110.0, exclude_ids=["s1", "m1"])
    assert (sql, args) == ref.calls[0]  # fără subiect, doar ancora + excluderea ecranului
    assert "is not distinct from" not in sql
    ev = next(e for e in ctx.events if e.type == "cheaper_followup")
    assert set(ev.properties) - {"turn_id"} == {"baseline", "found"}


async def test_m2_without_a_named_anchor_the_sql_is_the_old_one(_flags):
    from src.db.queries.catalog import search_cheaper_than

    _flags(False)
    conn = _RecConn(_TYPES)
    await planner.resolve_cheaper_followup(
        _turn("ceva mai ieftin?"), _rec_deps(conn), policy=_Policy()
    )
    ref = _RecConn({})
    await search_cheaper_than(ref, "biz", ["s1", "m1"], 15.0)
    assert conn.calls[0] == ref.calls[0]


@pytest.mark.parametrize("subject_on", [True, False])
async def test_l1_every_card_on_the_current_screen_is_excluded(_flags, subject_on):
    import re

    _flags(subject_on)
    conn = _RecConn(_TYPES)
    await planner.resolve_cheaper_followup(
        _turn("tonerul mai ieftin?"), _rec_deps(conn), policy=_Policy()
    )
    sql, args = conn.calls[0]
    assert args[1] == ["t1"] and args[2] == 110.0  # categoria și pragul tonerului
    assert ["s1", "m1"] in args  # ecranul curent nu se re-servește
    used = sorted({int(x) for x in re.findall(r"\$(\d+)", sql)})
    assert used == list(range(1, len(args) + 1))  # fiecare parametru legat apare în SQL
