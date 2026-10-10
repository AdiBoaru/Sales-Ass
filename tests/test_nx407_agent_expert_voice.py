"""NX-407 — agentul vorbește ca un specialist: recenziile reale ajung la el, meniul de nevoi are
doar fațetele pachetului, iar sugestiile oferite se țin minte. Promptul `assistant.v6` poartă
regulile care lipseau (fără catalog în text, fără repetiții, primul card = alegerea lui).
Zero apeluri de model și zero DB (catalogul fals din NX-396)."""

from __future__ import annotations

import json
from types import SimpleNamespace as NS

import src.db.queries.catalog as cat
from src.agent import detail_answer as da
from src.assistant import menus as amenus
from src.assistant import prompt as aprompt
from src.assistant.memory import OFFERED_CHARS, OFFERED_MAX, Memory
from src.assistant.tools import Tools
from src.domain.facets import FacetSource, FacetType, TypedFacet
from tests.test_nx396_assistant import (  # noqa: F401 — `_catalog` e fixture autouse
    ROWS,
    ScriptedLLM,
    _ans,
    _call,
    _catalog,
    _ctx,
    _events,
    _run,
    _search,
)

LONG = (
    "Sincer, nu credeam ca un gel poate hidrata asa bine. De obicei folosesc creme mai dense, dar "
    "acesta m-a convins. Perfect pentru zilele de vara si pentru tenul gras. Efectul calmant se "
    "simte imediat dupa aplicare, iar dimineata nu mai am luciu pe frunte."
)
SHORT = "Se absoarbe in cateva secunde si nu lipeste."
#: Funcția reală, capturată înainte ca fixture-ul comun (`_catalog`) s-o înlocuiască cu un fals.
REAL_REVIEW_EXCERPTS = cat.review_excerpts


# --- meniul de nevoi -----------------------------------------------------------------------------


class _Vocab:
    def __init__(self, dims):
        self.dims = dims
        self.facet_names = list(dims)

    def entries(self, dim):
        return [NS(key=k, count=c) for k, c in self.dims[dim]]


def _facet(key):
    return TypedFacet(
        key=key,
        value_type=FacetType.ENUM,
        source=FacetSource.ATTRIBUTE,
        source_key=key,
        operators=("eq",),
    )


VOCAB = _Vocab(
    {
        "skin_type": [("oily", 90), ("dry", 80)],
        "concerns": [("hydration", 300)],
        "price_per_unit_source": [("170 lei/100ml", 40)],
        "shade_group": [("254d9a22fa84", 12)],
        "sku": [("ABC-1", 1)],
        "category": [("ten", 900)],
    }
)


def test_the_needs_menu_has_only_the_facets_the_pack_declares():
    pack = NS(facets=(_facet("skin_type"), _facet("concerns")))
    assert amenus.need_values(VOCAB, pack) == (
        "skin_type:oily",
        "skin_type:dry",
        "concerns:hydration",
    )


def test_without_declared_facets_the_menu_is_the_old_one():
    old = amenus.need_values(VOCAB)
    assert "price_per_unit_source:170 lei/100ml" in old and "category:ten" not in old
    assert amenus.need_values(VOCAB, NS(facets=())) == old
    assert amenus.declared_dimensions(None) is None


# --- recenziile pe fișă ---------------------------------------------------------------------------

PRODUCT = {
    "name": "COSRX Hydrium Gel Cream",
    "price": 131.0,
    "rating": 4.9,
    "review_count": 12,
    "top_pros": ["se absoarbe rapid"],
    "reviews_list": [
        {"author": "Ana Pop", "rating": 4, "body": LONG},
        {"author": "Ion", "rating": 5, "body": SHORT},
        {"author": "X", "rating": 5, "body": "a treia"},
    ],
}


def test_the_sheet_carries_real_reviews_only_when_asked():
    plain = da.product_facts(PRODUCT, None, "ro")
    assert "customer review" not in plain, "compozitorul și NX-381 rămân neatinse"
    sheet = da.product_facts(PRODUCT, None, "ro", reviews=2)
    lines = [x for x in sheet.splitlines() if x.startswith("customer review")]
    assert len(lines) == 2
    assert lines[0].startswith("customer review (4/5): Sincer")
    assert lines[1] == f"customer review (5/5): {SHORT}"
    assert "Ana" not in sheet and "Ion" not in sheet, "fără autor"
    assert all(len(x.split(": ", 1)[1]) <= da.REVIEW_EXCERPT_CHARS for x in lines)
    assert "rating: 4.9/5 from 12 reviews" in sheet


# --- recenziile pe rândul de căutare --------------------------------------------------------------


class _Db:
    def __init__(self):
        self.ops: list[str] = []

    def __call__(self, op):
        db = self

        class _Cm:
            async def __aenter__(self):
                db.ops.append(op)
                return "conn"

            async def __aexit__(self, *exc):
                return False

        return _Cm()


async def test_a_search_row_carries_what_customers_said(monkeypatch):
    seen = {}

    async def fake_excerpts(conn, business_id, ids, *, per_product=1):
        seen.update(business_id=business_id, ids=list(ids), per_product=per_product)
        return {ROWS[0]["id"]: [{"rating": 5, "body": LONG}]}

    monkeypatch.setattr(cat, "review_excerpts", fake_excerpts)
    llm = ScriptedLLM(
        [_call("search_catalog", _search(), "c1")],
        [_call("answer", _ans("Uite.", ["P1"], reason="hidratează lejer"), "c2")],
    )
    served, ctx = await _run(llm)
    assert served
    assert seen["business_id"] == "b", "tenantul vine de la server"
    assert seen["ids"] == [ROWS[0]["id"], ROWS[1]["id"]]
    out = next(
        i
        for i in llm.inputs[1]
        if isinstance(i, dict) and i.get("type") == "function_call_output" and i["call_id"] == "c1"
    )
    products = json.loads(out["output"])["products"]
    assert products[0]["customers_say"] == [da.review_excerpt(LONG)]
    assert "customers_say" not in products[1], "fără recenzii, fără câmp"
    assert not _events(ctx, "assistant_reviews")


async def test_a_failed_review_read_leaves_the_rows_and_is_visible(monkeypatch):
    async def broken(conn, business_id, ids, *, per_product=1):
        raise OSError("pooler")

    monkeypatch.setattr(cat, "review_excerpts", broken)
    llm = ScriptedLLM(
        [_call("search_catalog", _search(), "c1")],
        [_call("answer", _ans("Uite.", ["P1"], reason="hidratează lejer"), "c2")],
    )
    served, ctx = await _run(llm)
    assert served, "fragmentele sunt un plus, nu o condiție a turului"
    assert _events(ctx, "assistant_reviews") == [
        {"outcome": "error", "error": "OSError", "turn_id": "t"}
    ]


async def test_reviews_are_read_once_and_only_for_rows_without_them(monkeypatch):
    calls = []

    async def fake_excerpts(conn, business_id, ids, *, per_product=1):
        calls.append(list(ids))
        return {}

    monkeypatch.setattr(cat, "review_excerpts", fake_excerpts)
    db = _Db()
    ctx = _ctx()
    tools = Tools(ctx, NS(db=db), None, Memory(), shown_ids=set())
    tools.facts.rows = {"a": {"id": "a", "reviews_list": []}, "b": {"id": "b"}}
    await tools.attach_reviews(["a", "b"])
    await tools.attach_reviews(["a", "b"])
    assert calls == [["b"]] and db.ops == ["assistant_reviews"]
    assert tools.facts.rows["b"]["reviews_list"] == []


def test_review_text_is_never_a_fact_for_the_gate():
    """Recenzia adversarială: «SPF 50 într-un tub de 100 ml» scris de un client despre crema lui
    de dinainte trecea pe cardul unui produs cu SPF 30. Poarta nu vede textul recenziilor."""
    ctx = _ctx()
    tools = Tools(ctx, NS(db=_Db()), None, Memory(), shown_ids=set())
    tools.facts.rows = {
        "a": {"id": "a", "name": "Gel", "reviews_list": [{"rating": 5, "body": LONG}]}
    }
    assert "tenul gras" not in tools.product_text("a")
    from src.assistant.tools import _without_reviews

    out = {
        "products": [{"handle": "P1", "customers_say": ["14 zile"]}],
        "sheets": {"P1": "name: Gel\ncustomer review (5/5): dupa 14 zile\nrating: 4.9/5"},
    }
    clean = _without_reviews(out)
    assert clean["products"] == [{"handle": "P1"}]
    assert clean["sheets"]["P1"] == "name: Gel\nrating: 4.9/5"
    assert out["products"][0]["customers_say"] == ["14 zile"], "ce vede agentul rămâne neatins"


async def test_a_number_copied_from_a_review_is_rejected_by_the_gate(monkeypatch):
    async def fake_excerpts(conn, business_id, ids, *, per_product=1):
        body = "Folosesc crema de 14 zile si pielea e mult mai calma dimineata."
        return {ROWS[0]["id"]: [{"rating": 5, "body": body}]}

    monkeypatch.setattr(cat, "review_excerpts", fake_excerpts)
    llm = ScriptedLLM(
        [_call("search_catalog", _search(), "c1")],
        [_call("answer", _ans("Uite.", ["P1"], reason="Vezi rezultate in 14 zile."), "c2")],
        [_call("answer", _ans("Uite.", ["P1"], reason="Calmeaza pielea peste noapte."), "c3")],
    )
    served, ctx = await _run(llm)
    assert served
    [ev] = _events(ctx, "assistant_turn")
    assert ev["retries"] == 1 and ev["gate_first"], "prima variantă respinsă"
    assert [i.reason for i in ctx.reply.rich.items] == ["Calmeaza pielea peste noapte."]


def test_a_review_about_pregnancy_or_a_cure_never_reaches_the_agent():
    """Recenzia adversarială: «a folosit-o în sarcină fără probleme», redat, e sfat medical."""
    pregnant = "Am folosit-o toata sarcina si nu am avut nicio problema, textura e superba."
    product = {
        "reviews_list": [
            {"rating": 5, "body": pregnant},
            {"rating": 5, "body": SHORT},
        ]
    }
    assert not da.relayable(pregnant)
    assert da.relayable(SHORT)
    assert da.review_lines(product, 2) == [f"customer review (5/5): {SHORT}"]
    from src.assistant.tools import _excerpts

    assert _excerpts(product, 1) == [SHORT], "se sare la următoarea recenzie"


class _Conn:
    def __init__(self, rows):
        self.rows, self.sql, self.args = rows, None, None

    async def fetch(self, sql, *args):
        self.sql, self.args = sql, args
        return self.rows


async def test_the_review_query_is_tenant_scoped_and_ordered_in_code():
    conn = _Conn(
        [
            {"product_id": "p", "rating": 5, "body": SHORT},
            {"product_id": "p", "rating": 4, "body": LONG},
        ]
    )
    out = await REAL_REVIEW_EXCERPTS(conn, "biz", ["p"], per_product=2)
    assert "business_id = $1" in conn.sql and conn.args[0] == "biz"
    assert [x["body"] for x in out["p"]] == [LONG, SHORT], "cea lungă întâi, ca pe fișă"
    assert "author" not in conn.sql
    assert await REAL_REVIEW_EXCERPTS(conn, "biz", []) == {}


# --- sugestiile ținute minte ----------------------------------------------------------------------


def test_offered_suggestions_round_trip_with_caps():
    m = Memory(offered=[f"sugestie {i} " + "x" * 200 for i in range(8)])
    saved = m.to_state()
    assert len(saved["s"]) == OFFERED_MAX and all(len(s) <= OFFERED_CHARS for s in saved["s"])
    assert Memory.from_state(saved).offered == saved["s"]
    assert "s" not in Memory().to_state(), "fără sugestii, forma de dinainte"
    assert Memory.from_state({"s": "stricat"}).offered == []
    assert Memory.from_state({"s": ["ok", 3, " "]}).offered == ["ok"]


async def test_the_served_suggestions_reach_the_next_turn():
    llm = ScriptedLLM(
        [
            _call(
                "answer",
                _ans("Salut!", suggestions=["Compară primele două", "Ai ceva mai ieftin?"]),
                "c1",
            )
        ]
    )
    served, ctx = await _run(llm, _ctx("salut"))
    assert served
    assert ctx.state_patch["assistant"]["s"] == ["Compară primele două", "Ai ceva mai ieftin?"]
    assert ctx.state_patch["assistant"]["st"] == "t", "turul care le-a oferit"


def _bot(turn_id):
    from src.models import Direction, Message

    return Message(
        direction=Direction.OUTBOUND, author="bot", body="Salut!", payload={"turn_id": turn_id}
    )


async def test_suggestions_are_shown_only_after_the_reply_that_offered_them():
    """Recenzia adversarială: după un tur servit de plasă sau de un strat gratuit, sugestiile
    ținute minte NU sunt ale răspunsului anterior."""
    from src.assistant import turn as aturn
    from src.models import ConversationState, Direction, Message

    m = Memory(offered=["Compară primele două"], offered_turn="t-old")
    assert m.offered_for("t-old") == ["Compară primele două"]
    assert m.offered_for("t-other") == [] and m.offered_for(None) == []
    assert Memory(offered=["x"]).offered_for("") == []
    client = Message(direction=Direction.INBOUND, author="contact", body="salut")
    state = ConversationState()
    state.assistant = {"h": {}, "n": 1, "notes": "", "s": ["Compară primele două"], "st": "t-old"}
    for last, shown in (("t-old", True), ("t-fallback", False)):
        llm = ScriptedLLM([_call("answer", _ans("Bine."), "c1")])
        await _run(llm, _ctx("mersi", state=state, history=[client, _bot(last)]))
        view = llm.inputs[0][0]["content"]
        assert ("YOUR LAST SUGGESTIONS" in view) is shown, last
    assert aturn._last_reply_turn(_ctx("x")) is None


def test_the_view_shows_the_last_suggestions_after_history():
    memory = Memory(offered=["Compară PSA cu COSRX"])
    kw = dict(
        rows={},
        names={},
        shown=set(),
        cart=[],
        cart_total=None,
        currency="lei",
        safety=[],
        message="mersi",
    )
    view = aprompt.render_view(memory=memory, history=[("client", "salut", [])], **kw)
    head, _, tail = view.partition("YOUR LAST SUGGESTIONS")
    assert "HISTORY" in head and "- Compară PSA cu COSRX" in tail
    assert tail.index("Compară") < tail.index("CUSTOMER MESSAGE")
    assert "YOUR LAST SUGGESTIONS" not in aprompt.render_view(memory=memory, history=[], **kw)


# --- promptul ------------------------------------------------------------------------------------


def test_prompt_v6_carries_the_missing_rules():
    assert aprompt.PROMPT_VERSION == "assistant.v6"
    text = aprompt.instructions(
        store="SOLE", locale="ro", families=("fata",), max_shown=6, chip_count=5
    )
    for anchor in (
        "Never mention the catalog",
        "Do not repeat what an earlier reply already said",
        "the first card is the one you would take first",
        "not with its name",
        "`customers_say`",
        "Never offer again one of YOUR LAST SUGGESTIONS",
        "do not tell the customer that something is not confirmed",
    ):
        assert anchor in text, anchor
    assert "{" not in text.replace("{}", ""), "niciun marcator nesubstituit"
