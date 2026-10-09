"""NX-393 — prototipul agentului unic (varianta A): bucla, uneltele și porțile, fără model real și
fără DB. Rularea pe catalogul real și cu modelul o pornește Adi (`scripts/sim/agent_a_prototype.py
--yes`); aici se verifică doar că scriptul nu pierde creditele pe o eroare de cod."""

from __future__ import annotations

import json
from types import SimpleNamespace as NS

import pytest

from scripts.sim import agent_a_prototype as ap

MENUS = ap.Menus(
    categories=("ten", "par"),
    product_types=("crema de fata", "sampon"),
    brands=("COSRX",),
    needs=("skin_type:oily", "concerns:acne"),
)
ROWS = [
    {"id": "u1", "name": "COSRX Crema A - descriere", "price": 100.0, "availability": "in_stock"},
    {"id": "u2", "name": "COSRX Crema B - descriere", "price": 50.0, "availability": "in_stock"},
]


class Item:
    """Un element de ieșire `/v1/responses`, cu `model_dump` ca în SDK."""

    def __init__(self, **kw):
        self.__dict__.update(kw)

    def model_dump(self, exclude_none: bool = True):
        return {k: v for k, v in self.__dict__.items() if v is not None}


def _call(name, args, cid):
    return Item(type="function_call", name=name, arguments=json.dumps(args), call_id=cid)


class ScriptedLLM:
    """Rundele modelului, în ordine; reține ce a primit la fiecare rundă."""

    model_agent = "gpt-6-luna"

    def __init__(self, *rounds):
        self.rounds = list(rounds)
        self.inputs: list[list] = []

    async def _respond(self, *, effort, **kw):
        self.inputs.append(list(kw["input"]))
        return NS(output=self.rounds.pop(0), usage=None)


class FakeTools(ap.Tools):
    async def _search_catalog(self, a):
        return {"found": len(ROWS), "products": self._remember(ROWS)}


def _ans(text, show=(), **kw):
    """Argumentele lui `answer` în forma strictă (NX-395)."""
    return {
        "text": text,
        "cards": [{"handle": h, "reason": ""} for h in show],
        "suggestions": kw.get("suggestions", []),
        "comparison": kw.get("comparison"),
        "notes": kw.get("notes", ""),
    }


def _tools(conv=None):
    conv = conv or ap.Conversation()
    business = NS(id="b", default_locale="ro", domain_pack=None, name="SOLE")
    return FakeTools(NS(), business, MENUS, conv), conv


# --- uneltele și meniurile -----------------------------------------------------------------------


def test_the_search_filters_are_closed_menus_from_the_catalog():
    schemas = {t["name"]: t for t in ap.tool_schemas(MENUS)}
    props = schemas["search_catalog"]["parameters"]["properties"]
    assert props["product_types"]["items"]["enum"] == ["crema de fata", "sampon"]
    assert props["needs"]["items"]["enum"] == ["skin_type:oily", "concerns:acne"]
    assert set(schemas) == {
        "search_catalog",
        "product_details",
        "store_rules",
        "add_to_cart",
        "answer",
    }


def test_a_value_outside_the_menu_is_refused_with_a_hint():
    with pytest.raises(ap._Refused, match="not in the menu"):
        ap._check("fata", MENUS.categories, "category")
    ap._check(None, MENUS.categories, "category")


async def test_only_a_shown_product_goes_to_the_cart():
    tools, conv = _tools()
    conv.handle_of("u1")
    out = json.loads(await tools.run("add_to_cart", {"handle": "P1"}))
    assert out == {
        "ok": False,
        "error": "only a product you showed to the customer can go to the cart",
    }
    conv.handle_of("u1")
    conv.shown.append("P1")
    out = json.loads(await tools.run("add_to_cart", {"handle": "P1", "quantity": 2}))
    assert out["ok"] and conv.cart == {"P1": 2}


# --- porțile de adevăr ---------------------------------------------------------------------------


def test_an_invented_price_is_flagged_and_the_shown_total_is_not():
    assert ap.truth_gate("Costă 77 lei.", ROWS, [ROWS], None) == ["ungrounded_price"]
    assert ap.truth_gate("Amândouă fac 150 lei.", ROWS, [ROWS], None) == []
    assert ap.truth_gate("Prima costă 100 lei.", ROWS, [ROWS[:1]], None) == []


def test_a_medical_claim_is_flagged():
    assert "medical_claim" in ap.truth_gate("Tratează acneea.", ROWS, [ROWS], None)


# --- bucla unui tur ------------------------------------------------------------------------------


async def test_a_turn_searches_then_answers_with_cards():
    llm = ScriptedLLM(
        [
            Item(type="reasoning", encrypted_content="x"),
            _call("search_catalog", {"query": "crema"}, "c1"),
        ],
        [_call("answer", _ans("Îți recomand prima, 100 lei.", ["P1"]), "c2")],
    )
    tools, conv = _tools()
    out = await ap.run_turn(llm, tools, conv, "vreau o crema", store="SOLE", effort="low")
    assert out["ok"] and out["rounds"] == 2
    assert out["text"] == "Îți recomand prima, 100 lei."
    assert [p["handle"] for p in out["products"]] == ["P1"]
    assert out["gate"] == []
    assert [c["tool"] for c in out["tools"]] == ["search_catalog", "answer"]
    # runda a doua primește raționamentul și ieșirea uneltei din runda întâi
    second = llm.inputs[1]
    assert {"type": "reasoning", "encrypted_content": "x"} in second
    outputs = [i for i in second if isinstance(i, dict) and i.get("type") == "function_call_output"]
    assert outputs and json.loads(outputs[0]["output"])["found"] == 2
    assert conv.shown == ["P1"]
    assert conv.history[-1] == {
        "role": "asistent",
        "text": "Îți recomand prima, 100 lei.",
        "shown": ["P1"],
    }


async def test_the_next_turn_sees_the_shown_products_and_the_history():
    llm = ScriptedLLM(
        [_call("search_catalog", {"query": "crema"}, "c1")],
        [_call("answer", _ans("Uite.", ["P2"]), "c2")],
        [_call("answer", _ans("Costă 50 lei."), "c3")],
    )
    tools, conv = _tools()
    await ap.run_turn(llm, tools, conv, "vreau o crema", store="SOLE", effort="low")
    out = await ap.run_turn(llm, tools, conv, "cat costa?", store="SOLE", effort="low")
    first_input = llm.inputs[2][0]["content"]
    assert "P2: COSRX Crema B, 50.0 lei" in first_input
    assert "client: vreau o crema" in first_input and first_input.endswith("cat costa?")
    assert out["gate"] == []


async def test_a_turn_without_an_answer_is_reported_not_invented():
    rounds = [[_call("search_catalog", {"query": "x"}, f"c{i}")] for i in range(ap.MAX_ROUNDS)]
    tools, conv = _tools()
    out = await ap.run_turn(ScriptedLLM(*rounds), tools, conv, "x", store="SOLE", effort="low")
    assert out["ok"] is False and out["rounds"] == ap.MAX_ROUNDS
    assert out["error"] == "no_answer" and out["served"] is False
    assert [m["role"] for m in conv.history] == ["client"], "nimic servit, nimic inventat"


def test_the_total_of_an_earlier_set_and_the_clients_budget_are_not_flagged():
    """Rularea din 2026-10-09: totalul corect al rutinei arătate la turul anterior (400 lei) și
    bugetul spus de client («sub 100 lei») ieșeau ca prețuri inventate."""
    earlier = [
        {"id": "a", "price": 100.0},
        {"id": "b", "price": 120.0},
        {"id": "c", "price": 90.0},
        {"id": "d", "price": 90.0},
    ]
    assert ap.truth_gate("În total, 400 de lei.", earlier, [earlier, []], None) == []
    assert (
        ap.truth_gate("Sub 100 lei am găsit una.", ROWS[1:], [ROWS[1:]], None, "sub 100 lei") == []
    )
    assert ap.truth_gate("Sub 100 lei am găsit una.", ROWS[1:], [ROWS[1:]], None, "") == [
        "ungrounded_price"
    ]
