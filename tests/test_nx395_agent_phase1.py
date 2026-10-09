"""NX-395 — agentul complet (Faza 1 a NX-394): handle-uri stricte, nume distincte, reîncercarea la
poarta de adevăr, notele, sugestiile, comparația și rutina. Fără model real și fără DB: modelul e
scriptat, uneltele care citesc din catalog sunt înlocuite."""

from __future__ import annotations

import json
from types import SimpleNamespace as NS

import pytest

from scripts.sim import agent_a_prototype as ap

MENUS = ap.Menus(
    categories=("ten",),
    product_types=("crema de fata", "cushion"),
    brands=("COSRX",),
    needs=("skin_type:dry", "concerns:acne"),
    families=("fata", "par"),
    moments=("am", "pm"),
    steps=("curatare", "hidratare", "spalare"),
)
ROWS = [
    {"id": "u1", "name": "COSRX Crema A - descriere", "price": 100.0, "availability": "in_stock"},
    {"id": "u2", "name": "COSRX Crema B - descriere", "price": 50.0, "availability": "in_stock"},
]
SHADES = [
    {"id": "s1", "name": "Cushion Glow - nuanta 10C, 15 g", "price": 90.0},
    {"id": "s2", "name": "Cushion Glow - nuanta 13C, 15 g", "price": 90.0},
    {"id": "s3", "name": "Cushion Glow - nuanta 18, 15 g", "price": 90.0},
]


class Item:
    def __init__(self, **kw):
        self.__dict__.update(kw)

    def model_dump(self, exclude_none: bool = True):
        return {k: v for k, v in self.__dict__.items() if v is not None}


def _call(name, args, cid):
    return Item(type="function_call", name=name, arguments=json.dumps(args), call_id=cid)


def _ans(text, show=(), **kw):
    return {
        "text": text,
        "cards": [{"handle": h, "reason": kw.get("reason", "")} for h in show],
        "suggestions": kw.get("suggestions", []),
        "comparison": kw.get("comparison"),
        "notes": kw.get("notes", ""),
    }


class ScriptedLLM:
    model_agent = "gpt-6-luna"

    def __init__(self, *rounds):
        self.rounds = list(rounds)
        self.inputs: list[list] = []
        self.tools: list[list] = []
        self.instructions: list[str] = []

    async def _respond(self, *, effort, **kw):
        self.inputs.append(list(kw["input"]))
        self.tools.append(kw["tools"])
        self.instructions.append(kw["instructions"])
        return NS(output=self.rounds.pop(0), usage=None)


class FakeTools(ap.Tools):
    rows = ROWS

    async def _search_catalog(self, a):
        return {"found": len(self.rows), "products": self._remember(self.rows)}

    async def comparison(self, handles, texts):
        from src.models import Comparison, ComparisonColumn, ComparisonRow

        cols = [
            ComparisonColumn(product_id=self.conv.handles[h], name=h, price=1.0) for h in handles
        ]
        return Comparison(
            columns=cols,
            rows=[ComparisonRow(label="Preț", values=["100", "50"])],
            intro=texts.get("intro"),
            closing=[texts["closing"]] if texts.get("closing") else [],
        )


def _tools(rows=ROWS):
    conv = ap.Conversation()
    business = NS(id="b", default_locale="ro", domain_pack=None, name="SOLE")
    tools = FakeTools(NS(), business, MENUS, conv)
    tools.rows = rows
    return tools, conv


async def _turn(llm, tools, conv, message="vreau o crema"):
    return await ap.run_turn(llm, tools, conv, message, store="SOLE", effort="low")


# --- R1: handle-urile ----------------------------------------------------------------------------


def _walk(schema):
    if isinstance(schema, dict):
        yield schema
        for v in schema.values():
            yield from _walk(v)
    elif isinstance(schema, list):
        for v in schema:
            yield from _walk(v)


def test_every_schema_is_strict_and_every_object_closed():
    for tool in ap.tool_schemas(MENUS):
        assert tool["strict"] is True, tool["name"]
        for node in _walk(tool["parameters"]):
            if node.get("type") == "object":
                assert node["additionalProperties"] is False, tool["name"]
                assert sorted(node["required"]) == sorted(node["properties"]), tool["name"]


def test_every_product_reference_is_a_handle_pattern():
    """Unde modelul numește un produs, schema cere forma `P<n>`: un slug nu mai trece."""
    names = {t["name"]: t["parameters"]["properties"] for t in ap.tool_schemas(MENUS)}
    refs = [
        names["search_catalog"]["exclude"]["items"],
        names["product_details"]["handles"]["items"],
        names["add_to_cart"]["handle"],
        names["answer"]["cards"]["items"]["properties"]["handle"],
        names["answer"]["comparison"]["anyOf"][0]["properties"]["handles"]["items"],
        names["routine_plan"]["anchor"]["anyOf"][0],
    ]
    assert all(r == {"type": "string", "pattern": ap.HANDLE_PATTERN} for r in refs)


def test_the_schemas_do_not_change_when_products_become_known():
    """Prefixul cache-uit: schemele depind doar de meniuri, nu de produsele conversației."""
    before = json.dumps(ap.tool_schemas(MENUS))
    conv = ap.Conversation()
    for r in ROWS:
        conv.facts[r["id"]] = r
        conv.handle_of(r["id"])
    assert json.dumps(ap.tool_schemas(MENUS)) == before


async def test_a_slug_or_an_unknown_handle_is_refused_with_the_valid_ones():
    tools, conv = _tools()
    await tools.run("search_catalog", {"query": "crema"})
    out = json.loads(await tools.run("product_details", {"handles": ["black-rouge-balm"]}))
    assert out["ok"] is False and "is not a handle" in out["error"]
    out = json.loads(await tools.run("product_details", {"handles": ["P9"]}))
    assert out["ok"] is False and "valid handles: P1, P2" in out["error"]
    assert [c.get("refused") is not None for c in tools.calls] == [False, True, True]


async def test_the_view_lists_every_known_product_not_only_the_shown_ones():
    tools, conv = _tools()
    await tools.run("search_catalog", {"query": "crema"})
    conv.shown.append("P1")
    view = conv.view()
    assert "- P1: COSRX Crema A, 100.0 lei, in_stock [shown]" in view
    assert "- P2: COSRX Crema B, 50.0 lei, in_stock" in view


# --- R4: numele distincte ------------------------------------------------------------------------


def test_shades_with_the_same_short_name_get_what_tells_them_apart():
    names = ap.distinct_names({r["id"]: r["name"] for r in SHADES + ROWS})
    assert names["s1"] == "Cushion Glow (10C)"
    assert names["s2"] == "Cushion Glow (13C)"
    assert names["s3"] == "Cushion Glow (18)"
    assert names["u1"] == "COSRX Crema A", "un nume unic rămâne scurt"


def test_identical_full_names_get_an_order_number():
    names = ap.distinct_names({"a": "KUNDAL Sampon", "b": "KUNDAL Sampon"})
    assert names == {"a": "KUNDAL Sampon (1)", "b": "KUNDAL Sampon (2)"}


async def test_search_results_carry_the_distinct_name():
    tools, conv = _tools(rows=SHADES)
    out = json.loads(await tools.run("search_catalog", {"query": "cushion"}))
    assert [p["name"] for p in out["products"]] == [
        "Cushion Glow (10C)",
        "Cushion Glow (13C)",
        "Cushion Glow (18)",
    ]


# --- R5: reîncercarea la poarta de adevăr --------------------------------------------------------


async def test_an_invented_price_is_rejected_then_fixed_on_the_retry():
    llm = ScriptedLLM(
        [_call("search_catalog", {"query": "crema"}, "c1")],
        [_call("answer", _ans("Prima costă 77 lei.", ["P1"]), "c2")],
        [_call("answer", _ans("Prima costă 100 lei.", ["P1"]), "c3")],
    )
    tools, conv = _tools()
    out = await _turn(llm, tools, conv)
    assert out["served"] and out["text"] == "Prima costă 100 lei."
    assert out["retries"] == 1 and out["first_rejected"] == ["ungrounded_price"]
    rejection = [
        json.loads(i["output"])
        for i in llm.inputs[2]
        if isinstance(i, dict) and i.get("type") == "function_call_output" and i["call_id"] == "c2"
    ][0]
    assert rejection["ok"] is False and rejection["rejected"] == ["ungrounded_price"]


async def test_a_wrong_card_reason_is_rejected_like_the_text():
    a = ap.check_answer(_known(), _ans("Uite.", ["P1"], reason="Costă doar 20 lei."), "")
    assert a.rejected == ["ungrounded_price"]


async def test_failing_twice_serves_nothing():
    llm = ScriptedLLM(
        [_call("search_catalog", {"query": "crema"}, "c1")],
        [_call("answer", _ans("Costă 77 lei.", ["P1"]), "c2")],
        [_call("answer", _ans("Costă 78 lei.", ["P1"]), "c3")],
    )
    tools, conv = _tools()
    out = await _turn(llm, tools, conv)
    assert out["served"] is False and out["error"] == "gate_failed"
    assert out["rejected_text"] == "Costă 78 lei." and out["gate"] == ["ungrounded_price"]
    assert conv.shown == [] and [m["role"] for m in conv.history] == ["client"]


async def test_an_unknown_card_handle_is_a_rejection_not_a_silent_drop():
    a = ap.check_answer(_known(), _ans("Uite.", ["P1", "P9"]), "")
    assert a.rejected == ["unknown_card"] and [c["handle"] for c in a.cards] == ["P1"]


def _known():
    conv = ap.Conversation()
    for r in ROWS:
        conv.facts[r["id"]] = r
        conv.handle_of(r["id"])
    return conv


# --- notele --------------------------------------------------------------------------------------


async def test_notes_persist_and_open_the_next_turns_view():
    llm = ScriptedLLM(
        [_call("answer", _ans("Ce tip de ten ai?", notes="buget sub 100 lei"), "c1")],
        [_call("answer", _ans("Bine."), "c2")],
    )
    tools, conv = _tools()
    await _turn(llm, tools, conv, "ceva sub 100 lei")
    await _turn(llm, tools, conv, "uscat")
    second = llm.inputs[1][0]["content"]
    assert second.startswith("NOTES (what the customer said, written by you earlier)\nbuget sub")
    assert conv.notes == "buget sub 100 lei", "un `notes` gol nu șterge notele"


def test_notes_are_capped():
    a = ap.check_answer(_known(), _ans("Ok.", notes="x" * 900), "")
    assert len(a.notes) == ap.NOTES_MAX


# --- sugestiile ----------------------------------------------------------------------------------


def test_suggestions_are_capped_and_a_wrong_one_is_dropped_not_the_answer():
    a = ap.check_answer(
        _known(),
        _ans(
            "Uite.",
            ["P1"],
            suggestions=[
                "Ai ceva pentru ten uscat?",
                "Vreau una de 33 lei",
                "x" * 80,
                "Ai ceva pentru ten uscat?",
                "Și o cremă de noapte?",
                "Cum se aplică?",
                "Ce rutină îmi faci?",
                "Ai și în alt gramaj?",
            ],
        ),
        "",
    )
    assert a.rejected == []
    assert a.dropped_suggestions == ["Vreau una de 33 lei", "x" * 80]
    assert a.suggestions == [
        "Ai ceva pentru ten uscat?",
        "Și o cremă de noapte?",
        "Cum se aplică?",
        "Ce rutină îmi faci?",
        "Ai și în alt gramaj?",
    ]


# --- comparația ----------------------------------------------------------------------------------


async def test_a_comparison_is_a_table_from_facts_with_the_agents_texts_around_it():
    comp = {
        "handles": ["P1", "P2"],
        "intro": "Diferă la preț.",
        "subtitle": None,
        "closing": "Ia-o pe a doua dacă vrei să economisești.",
    }
    llm = ScriptedLLM(
        [_call("search_catalog", {"query": "crema"}, "c1")],
        [_call("answer", _ans("Uite comparația.", comparison=comp), "c2")],
    )
    tools, conv = _tools()
    out = await _turn(llm, tools, conv, "compara-le")
    assert out["comparison"]["rows"] == [{"label": "Preț", "values": ["100", "50"]}]
    assert out["comparison"]["intro"] == "Diferă la preț."
    assert out["comparison"]["closing"] == ["Ia-o pe a doua dacă vrei să economisești."]


def test_a_comparison_text_with_an_invented_price_rejects_the_answer():
    comp = {"handles": ["P1", "P2"], "intro": "Prima e 120 lei.", "subtitle": None, "closing": ""}
    a = ap.check_answer(_known(), _ans("Uite.", comparison=comp), "")
    assert a.rejected == ["ungrounded_price"]


def test_the_difference_and_the_total_of_the_compared_pair_are_grounded():
    comp = {
        "handles": ["P1", "P2"],
        "intro": "Amândouă fac 150 lei.",
        "subtitle": None,
        "closing": "",
    }
    assert ap.check_answer(_known(), _ans("Uite.", comparison=comp), "").rejected == []


def test_a_comparison_with_one_known_product_is_rejected():
    comp = {"handles": ["P1", "P7"], "intro": "x", "subtitle": None, "closing": ""}
    a = ap.check_answer(_known(), _ans("Uite.", comparison=comp), "")
    assert a.rejected == ["comparison_handles"] and a.comparison is None


# --- rutina --------------------------------------------------------------------------------------


async def test_routine_plan_passes_the_agents_args_and_returns_handles(monkeypatch):
    seen = {}

    async def fake_routine(ctx, deps, args, *, planned=False, prefer_planned=None):
        seen.update(args=args, planned=planned, business=ctx.business.id)
        return NS(
            ok=True,
            error=None,
            products=[ROWS[0], ROWS[1]],
            llm_view="Rutina fata:\n1. curatare — [u1] COSRX Crema A\n2. hidratare — [u2] B",
        )

    import src.tools.routine_tools as rt

    monkeypatch.setattr(rt, "_routine", fake_routine)
    tools, conv = _tools()
    out = json.loads(
        await tools.run(
            "routine_plan",
            {
                "family": "fata",
                "moment": "pm",
                "needs": ["skin_type:dry"],
                "budget_max": 200,
                "steps": [],
                "anchor": None,
            },
        )
    )
    assert seen["planned"] is True, "argumentele agentului nu se re-judecă pe text (NX-333)"
    assert seen["args"]["concerns"] == ["dry"] and seen["args"]["budget_max"] == 200
    assert seen["args"]["moment"] == "pm" and seen["args"]["steps"] is None
    assert "[P1]" in out["routine"] and "[u1]" not in out["routine"]
    assert [p["handle"] for p in out["products"]] == ["P1", "P2"]


async def test_routine_plan_refuses_a_family_outside_the_pack():
    tools, conv = _tools()
    out = json.loads(
        await tools.run(
            "routine_plan",
            {
                "family": "unghii",
                "moment": None,
                "needs": [],
                "budget_max": None,
                "steps": [],
                "anchor": None,
            },
        )
    )
    assert out["ok"] is False and "not in the menu" in out["error"]


def test_without_routine_families_there_is_no_routine_tool():
    menus = ap.Menus(categories=(), product_types=(), brands=(), needs=())
    assert "routine_plan" not in {t["name"] for t in ap.tool_schemas(menus)}


async def test_a_routine_turn_is_marked_and_its_total_is_grounded(monkeypatch):
    async def fake_routine(ctx, deps, args, *, planned=False, prefer_planned=None):
        return NS(ok=True, error=None, products=ROWS, llm_view="Rutina")

    import src.tools.routine_tools as rt

    monkeypatch.setattr(rt, "_routine", fake_routine)
    routine_args = {
        "family": "fata",
        "moment": None,
        "needs": [],
        "budget_max": None,
        "steps": [],
        "anchor": None,
    }
    llm = ScriptedLLM(
        [_call("routine_plan", routine_args, "c1")],
        [_call("answer", _ans("Rutina ta, 150 lei în total.", ["P1", "P2"]), "c2")],
    )
    tools, conv = _tools()
    out = await _turn(llm, tools, conv, "fa-mi o rutina de fata")
    assert out["served"] and out["routine"] is True and out["gate"] == []


def test_the_instructions_name_the_routine_areas_and_hold_no_romanian_examples():
    text = ap.instructions("SOLE", MENUS)
    assert "Areas with routines: fata, par." in text
    for word in ("ten", "rutina", "crema", "vrei"):
        assert f" {word} " not in text.split("\n\n")[0]


@pytest.mark.parametrize("handle", ["P0", "p1", "P", "P1a", "1"])
def test_the_handle_pattern_refuses_malformed_handles(handle):
    with pytest.raises(ap._Refused, match="is not a handle"):
        _known().check(handle)
