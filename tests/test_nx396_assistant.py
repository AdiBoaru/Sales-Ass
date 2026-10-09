"""NX-396 — agentul unic în producție: modelul scriptat, catalogul fals, `ctx`/`deps` reale.

Se verifică turul servit (carduri, comparație, text, stare, evenimente), memoria între ture,
poarta de adevăr cu reîncercarea, siguranța pe fiecare set, mutațiile doar pe produse arătate și
căderea pe calea de azi cu contextul restaurat. Zero apeluri de model și zero DB."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace as NS

import pytest

from src.assistant import gate as agate
from src.assistant import menus as amenus
from src.assistant import turn as aturn
from src.assistant.memory import MAX_HANDLES, Memory, Refused, distinct_names
from src.assistant.menus import Menus
from src.assistant.schemas import HANDLE, tool_schemas
from src.config import get_settings
from src.domain.pack import DomainPack
from src.models import (
    BusinessConfig,
    Contact,
    ConversationState,
    Direction,
    InboundMessage,
    Message,
    TurnContext,
)
from src.worker.runner import PipelineDeps

MENUS = Menus(
    categories=("ten",),
    product_types=("crema de fata", "cushion"),
    brands=("COSRX",),
    needs=("skin_type:dry",),
    families=("fata",),
    moments=("am", "pm"),
    steps=("curatare", "hidratare"),
)


def _row(pid, name, price, **kw):
    return {
        "id": pid,
        "name": name,
        "price": price,
        "url": f"https://shop.test/{pid}",
        "availability": "in_stock",
        **kw,
    }


ROWS = [
    _row("11111111-1111-1111-1111-111111111111", "COSRX Crema A - descriere", 100.0),
    _row("22222222-2222-2222-2222-222222222222", "COSRX Crema B - descriere", 50.0),
]
SHADES = [
    _row("33333333-3333-3333-3333-333333333333", "Cushion Glow - nuanta 10C, 15 g", 90.0),
    _row("44444444-4444-4444-4444-444444444444", "Cushion Glow - nuanta 13C, 15 g", 90.0),
]
CATALOG = {r["id"]: r for r in ROWS + SHADES}


class Item:
    def __init__(self, **kw):
        self.__dict__.update(kw)

    def model_dump(self, exclude_none: bool = True):
        return {k: v for k, v in self.__dict__.items() if v is not None}


def _call(name, args, cid="c"):
    return Item(type="function_call", name=name, arguments=json.dumps(args), call_id=cid)


def _ans(text, cards=(), **kw):
    return {
        "text": text,
        "cards": [{"handle": h, "reason": kw.get("reason", "")} for h in cards],
        "suggestions": kw.get("suggestions", []),
        "comparison": kw.get("comparison"),
        "notes": kw.get("notes", ""),
    }


def _search(**kw):
    return {
        "query": "crema",
        "category": None,
        "product_types": [],
        "brand": None,
        "needs": [],
        "price_max": None,
        "price_min": None,
        "in_stock_only": False,
        "sort": "relevance",
        "exclude": [],
        **kw,
    }


class ScriptedLLM:
    model_agent = "gpt-6-luna"

    def __init__(self, *rounds):
        self.rounds = list(rounds)
        self.inputs: list[list] = []
        self.calls: list[dict] = []

    async def respond_round(self, *, instructions, input, tools, effort, **kw):  # noqa: A002
        self.inputs.append(list(input))
        self.calls.append({"instructions": instructions, "tools": tools, "effort": effort})
        nxt = self.rounds.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return NS(output=nxt, usage=None)


@pytest.fixture(autouse=True)
def _catalog(monkeypatch):
    """Catalogul fals: căutarea întoarce `state["search"]`, citirile după id iau din `CATALOG`."""
    state = {"search": list(ROWS), "blocked": set()}

    async def fake_menus(deps, business):
        return MENUS

    async def fake_search(conn, business_id, query, **kw):
        state["last_search"] = {"business_id": business_id, "query": query, **kw}
        return [dict(r) for r in state["search"]]

    async def fake_by_ids(conn, business_id, ids, *, limit=6, respect_content_status=False):
        return [dict(CATALOG[i]) for i in ids[:limit] if i in CATALOG]

    async def fake_pool(conn, business_id, ids, *, pool=30, respect_content_status=True):
        return [dict(CATALOG[i]) for i in ids[:pool] if i in CATALOG]

    import src.db.queries.catalog as cat
    from src.safety.policy import SafetyPolicy

    real_gate = SafetyPolicy.gate
    real_evaluate = SafetyPolicy.evaluate

    def fake_gate(self, ctx, products, *, purpose, count_kept=True):
        kept, decision = real_gate(self, ctx, products, purpose=purpose, count_kept=count_kept)
        return [p for p in kept if str(p.get("id")) not in state["blocked"]], decision

    def fake_evaluate(self, products, *, purpose="expose"):
        import dataclasses

        d = real_evaluate(self, products, purpose=purpose)
        kept = [p for p in d.kept if str(p.get("id")) not in state["blocked"]]
        return dataclasses.replace(d, kept=kept)

    monkeypatch.setattr(aturn, "load_menus", fake_menus)
    monkeypatch.setattr(cat, "search_products_lexical", fake_search)
    monkeypatch.setattr(cat, "get_products_by_ids", fake_by_ids)
    monkeypatch.setattr(cat, "get_products_pool_by_ids", fake_pool)
    monkeypatch.setattr(SafetyPolicy, "gate", fake_gate)
    monkeypatch.setattr(SafetyPolicy, "evaluate", fake_evaluate)
    s = get_settings()
    monkeypatch.setattr(s, "assistant_max_rounds", 6)
    monkeypatch.setattr(s, "assistant_turn_timeout_s", 30.0)
    return state


def _ctx(body="vreau o crema", *, state=None, history=()):
    business = BusinessConfig(id="b", slug="sole-ro", name="SOLE", vertical="ecommerce")
    business.domain_pack = DomainPack(vertical="ecommerce")
    business.default_locale = "ro"
    msg = Message(direction=Direction.INBOUND, author="contact", body=body)
    return TurnContext(
        turn_id="t",
        business=business,
        contact=Contact(id="c", business_id="b"),
        message=InboundMessage(provider_msg_id="m", body=body),
        conversation_id="conv",
        history=[*history, msg],
        state=state or ConversationState(),
        language="ro",
    )


def _deps(llm):
    return PipelineDeps(llm=llm)


def _events(ctx, type_):
    return [e.properties for e in ctx.events if e.type == type_]


async def _run(llm, ctx=None):
    ctx = ctx or _ctx()
    served = await aturn.run_assistant_turn(ctx, _deps(llm))
    return served, ctx


# --- turul servit --------------------------------------------------------------------------------


async def test_a_search_turn_is_served_with_cards_reasons_and_chips():
    llm = ScriptedLLM(
        [_call("search_catalog", _search(), "c1")],
        [
            _call(
                "answer",
                _ans(
                    "Uite două creme hidratante.",
                    ["P1", "P2"],
                    reason="hidratează bine",
                    suggestions=["Ai și ceva mai ieftin?", "Care e mai bună pentru ten uscat?"],
                    notes="vrea o cremă hidratantă",
                ),
                "c2",
            )
        ],
    )
    served, ctx = await _run(llm)
    assert served is True
    rich = ctx.reply.rich
    assert rich.intro == "Uite două creme hidratante."
    assert [i.name for i in rich.items] == ["COSRX Crema A", "COSRX Crema B"]
    assert [i.reason for i in rich.items] == ["hidratează bine", "hidratează bine"]
    assert [c.label for c in rich.chips] == [
        "Ai și ceva mai ieftin?",
        "Care e mai bună pentru ten uscat?",
    ]
    assert [p["product_id"] for p in ctx.reply.products] == [ROWS[0]["id"], ROWS[1]["id"]]
    assert ctx.reply.cacheable is False
    assert ctx.retrieval.source == "assistant" and ctx.retrieval.catalog_read is True
    memory = ctx.state_patch["assistant"]
    assert memory["h"] == {"P1": ROWS[0]["id"], "P2": ROWS[1]["id"]}
    assert memory["notes"] == "vrea o cremă hidratantă"
    assert ctx.state_patch["active_search"] is None
    [ev] = _events(ctx, "assistant_turn")
    assert ev["served"] and ev["rounds"] == 2 and ev["tools"] == ["search_catalog"]
    assert ev["cards"] == 2 and ev["n_suggestions"] == 2


async def test_the_model_never_sees_ids_and_the_tenant_comes_from_the_server(_catalog):
    llm = ScriptedLLM(
        [_call("search_catalog", _search(), "c1")],
        [_call("answer", _ans("Uite.", ["P1"]), "c2")],
    )
    await _run(llm)
    assert _catalog["last_search"]["business_id"] == "b"
    output = [i for i in llm.inputs[1] if isinstance(i, dict) and i.get("call_id") == "c1"]
    tool_out = json.loads(output[-1]["output"])
    assert [p["handle"] for p in tool_out["products"]] == ["P1", "P2"]
    assert ROWS[0]["id"] not in json.dumps(tool_out)


async def test_a_text_answer_carries_the_suggestions():
    llm = ScriptedLLM([_call("answer", _ans("Salut! Cu ce te ajut?", suggestions=["Caut un ser"]))])
    served, ctx = await _run(llm, _ctx("salut"))
    assert served and ctx.reply.rich is None and ctx.reply.text == "Salut! Cu ce te ajut?"
    assert ctx.reply.suggestions == ["Caut un ser"]
    assert "active_search" not in ctx.state_patch


async def test_the_same_schemas_and_instructions_on_every_round():
    llm = ScriptedLLM(
        [_call("search_catalog", _search(), "c1")],
        [_call("answer", _ans("Uite.", ["P1"]), "c2")],
    )
    await _run(llm)
    assert llm.calls[0]["tools"] == llm.calls[1]["tools"]
    assert llm.calls[0]["instructions"] == llm.calls[1]["instructions"]
    assert llm.calls[0]["effort"] == get_settings().llm_reasoning_effort_assistant


# --- memoria între ture --------------------------------------------------------------------------


async def test_the_next_turn_sees_the_shown_products_the_notes_and_the_history():
    llm = ScriptedLLM(
        [_call("search_catalog", _search(), "c1")],
        [_call("answer", _ans("Uite două.", ["P1", "P2"], notes="buget sub 120 lei"), "c2")],
    )
    _served, first = await _run(llm)
    shown = [
        {"product_id": p["product_id"], "name": p["name"], "price": p["price"]}
        for p in first.reply.products
    ]
    state = ConversationState.from_jsonb(
        {"assistant": first.state_patch["assistant"], "displayed_products": shown}
    )
    history = [
        Message(direction=Direction.INBOUND, author="contact", body="vreau o crema"),
        Message(
            direction=Direction.OUTBOUND, author="bot", body="Uite două.", payload={"shown": shown}
        ),
    ]
    llm2 = ScriptedLLM([_call("answer", _ans("A doua costă 50 lei."), "c3")])
    served, ctx = await _run(llm2, _ctx("cat costa a doua?", state=state, history=history))
    assert served
    view = llm2.inputs[0][0]["content"]
    assert view.startswith("NOTES (what the customer said, written by you earlier)\nbuget sub 120")
    assert "- P2: COSRX Crema B, 50.0" in view and "in_stock [shown]" in view
    assert "assistant: Uite două. [showed: P1 COSRX Crema A; P2 COSRX Crema B]" in view
    assert view.endswith("CUSTOMER MESSAGE\ncat costa a doua?")
    assert ctx.state_patch["assistant"]["h"]["P2"] == ROWS[1]["id"], "handle-ul e stabil"


async def test_the_total_of_an_earlier_set_is_grounded():
    shown = [{"product_id": r["id"], "name": r["name"], "price": r["price"]} for r in ROWS]
    state = ConversationState.from_jsonb({"displayed_products": shown})
    llm = ScriptedLLM([_call("answer", _ans("Amândouă fac 150 lei."), "c1")])
    served, ctx = await _run(llm, _ctx("cat fac amandoua?", state=state))
    assert served and ctx.reply.text == "Amândouă fac 150 lei."


# --- poarta de adevăr ----------------------------------------------------------------------------


async def test_an_invented_price_is_rejected_then_fixed_on_the_retry():
    llm = ScriptedLLM(
        [_call("search_catalog", _search(), "c1")],
        [_call("answer", _ans("Prima costă 77 lei.", ["P1"]), "c2")],
        [_call("answer", _ans("Prima costă 100 lei.", ["P1"]), "c3")],
    )
    served, ctx = await _run(llm)
    assert served and ctx.reply.rich.intro == "Prima costă 100 lei."
    rejection = [
        json.loads(i["output"])
        for i in llm.inputs[2]
        if isinstance(i, dict) and i.get("type") == "function_call_output" and i["call_id"] == "c2"
    ][0]
    assert rejection["rejected"] == ["ungrounded_price"] and rejection["fix"]
    [ev] = _events(ctx, "assistant_turn")
    assert ev["retries"] == 1 and ev["gate_first"] == ["ungrounded_price"]


async def test_a_store_rule_quoted_word_for_word_passes(monkeypatch):
    async def rules(ctx, deps):
        return [
            {"question": "Livrare", "answer": "Livrarea este gratuită la comenzi peste 199 lei."}
        ]

    import src.tools.faq_tools as faq

    monkeypatch.setattr(faq, "load_rules", rules)
    llm = ScriptedLLM(
        [_call("store_rules", {}, "c1")],
        [_call("answer", _ans("Livrarea este gratuită la comenzi peste 199 lei."), "c2")],
    )
    served, ctx = await _run(llm, _ctx("cat e livrarea?"))
    assert served and ctx.retrieval.read_beyond_catalog is True


async def test_failing_the_gate_twice_falls_back_with_the_context_restored():
    llm = ScriptedLLM(
        [_call("search_catalog", _search(), "c1")],
        [_call("answer", _ans("Costă 77 lei.", ["P1"]), "c2")],
        [_call("answer", _ans("Costă 78 lei.", ["P1"]), "c3")],
    )
    ctx = _ctx()
    ctx.state_patch["safety"] = {"x": 1}
    served, ctx = await _run(llm, ctx)
    assert served is False and ctx.reply is None and ctx.retrieval is None
    assert ctx.state_patch == {"safety": {"x": 1}}, "starea turului e cea de dinainte"
    assert [e.type for e in ctx.events] == ["assistant_fallback"]
    assert ctx.events[0].properties["reason"] == "gate_failed"
    assert "assistant" not in ctx.trace


async def test_an_unknown_card_is_a_rejection():
    llm = ScriptedLLM(
        [_call("search_catalog", _search(), "c1")],
        [_call("answer", _ans("Uite.", ["P9"]), "c2")],
        [_call("answer", _ans("Uite.", ["P1"]), "c3")],
    )
    served, ctx = await _run(llm)
    assert served and [i.product_id for i in ctx.reply.rich.items] == [ROWS[0]["id"]]


@pytest.mark.parametrize(
    ("rounds", "reason"),
    [
        ([RuntimeError("provider down")], "model_error"),
        ([[_call("search_catalog", _search(), f"c{i}")] for i in range(6)], "no_answer"),
    ],
)
async def test_a_failed_turn_falls_back(rounds, reason):
    served, ctx = await _run(ScriptedLLM(*rounds))
    assert served is False and ctx.reply is None
    assert ctx.events[-1].type == "assistant_fallback"
    assert ctx.events[-1].properties["reason"] == reason


async def test_a_slow_turn_falls_back_on_timeout(monkeypatch):
    monkeypatch.setattr(get_settings(), "assistant_turn_timeout_s", 0.05)

    class Slow(ScriptedLLM):
        async def respond_round(self, **kw):
            await asyncio.sleep(1)

    served, ctx = await _run(Slow())
    assert served is False and ctx.events[-1].properties["reason"] == "timeout"


async def test_without_a_model_the_turn_falls_back():
    served, ctx = await _run(None)
    assert served is False and ctx.events[-1].properties["reason"] == "model_error"


# --- siguranța -----------------------------------------------------------------------------------


async def test_a_blocked_product_never_reaches_the_model_nor_a_card(_catalog):
    _catalog["blocked"] = {ROWS[1]["id"]}
    llm = ScriptedLLM(
        [_call("search_catalog", _search(), "c1")],
        [_call("answer", _ans("Uite.", ["P1"]), "c2")],
    )
    served, ctx = await _run(llm)
    out = [i for i in llm.inputs[1] if isinstance(i, dict) and i.get("call_id") == "c1"][-1]
    assert [p["name"] for p in json.loads(out["output"])["products"]] == ["COSRX Crema A"]
    assert served and [i.product_id for i in ctx.reply.rich.items] == [ROWS[0]["id"]]


async def test_a_known_product_blocked_later_cannot_become_a_card(_catalog):
    state = ConversationState.from_jsonb(
        {"assistant": {"h": {"P1": ROWS[0]["id"], "P2": ROWS[1]["id"]}, "n": 3}}
    )
    _catalog["blocked"] = {ROWS[1]["id"]}
    llm = ScriptedLLM(
        [_call("answer", _ans("Uite.", ["P2"]), "c1")],
        [_call("answer", _ans("Uite.", ["P1"]), "c2")],
    )
    served, ctx = await _run(llm, _ctx("si a doua?", state=state))
    rejection = json.loads(llm.inputs[1][-1]["output"])
    assert rejection["rejected"] == ["unavailable_card"]
    assert served and [i.product_id for i in ctx.reply.rich.items] == [ROWS[0]["id"]]
    assert "P2:" not in llm.inputs[0][0]["content"]


# --- comparația și numele distincte --------------------------------------------------------------


async def test_a_comparison_is_the_widget_table_with_distinct_names(_catalog):
    _catalog["search"] = list(SHADES)
    comp = {
        "handles": ["P1", "P2"],
        "intro": "Diferă doar la nuanță.",
        "subtitle": None,
        "closing": "Ia 10C dacă ai tenul deschis.",
    }
    llm = ScriptedLLM(
        [_call("search_catalog", _search(query="cushion"), "c1")],
        [_call("answer", _ans("Le-am pus una lângă alta.", comparison=comp), "c2")],
    )
    served, ctx = await _run(llm)
    table = ctx.reply.comparison
    assert served and [c.name for c in table.columns] == [
        "Cushion Glow (10C)",
        "Cushion Glow (13C)",
    ]
    assert table.intro == "Le-am pus una lângă alta.\n\nDiferă doar la nuanță."
    assert table.closing == ["Ia 10C dacă ai tenul deschis."]
    assert [p["product_id"] for p in ctx.reply.products] == [SHADES[0]["id"], SHADES[1]["id"]]


# --- mutațiile -----------------------------------------------------------------------------------


async def test_the_cart_takes_only_a_shown_product(monkeypatch):
    seen = []

    async def fake_run_tool(ctx, deps, name, args):
        seen.append((name, args))
        return NS(ok=True, llm_view="adăugat", error=None, state_patch={"cart": [1]}, prices=[])

    import src.tools.base as base

    monkeypatch.setattr(base, "run_tool", fake_run_tool)
    shown = [{"product_id": ROWS[0]["id"], "name": ROWS[0]["name"], "price": 100.0}]
    state = ConversationState.from_jsonb(
        {
            "displayed_products": shown,
            "assistant": {"h": {"P1": ROWS[0]["id"], "P2": ROWS[1]["id"]}, "n": 3},
        }
    )
    llm = ScriptedLLM(
        [_call("add_to_cart", {"handle": "P2", "quantity": 1}, "c1")],
        [_call("add_to_cart", {"handle": "P1", "quantity": 2}, "c2")],
        [_call("answer", _ans("Am pus-o în coș."), "c3")],
    )
    served, ctx = await _run(llm, _ctx("o iau pe prima", state=state))
    refused = json.loads(llm.inputs[1][-1]["output"])
    assert refused["ok"] is False and "showed" in refused["error"]
    assert seen == [("cart_add", {"product_id": ROWS[0]["id"], "quantity": 2})]
    assert served and ctx.state_patch["cart"] == [1]
    assert _events(ctx, "assistant_turn")[0]["mutated"] is True


# --- rutina --------------------------------------------------------------------------------------


async def test_routine_plan_runs_the_planned_routine_with_handles(monkeypatch):
    seen = {}

    async def fake_planned(ctx, deps, a, *, prefer=None):
        seen["args"] = a
        return NS(
            ok=True,
            error=None,
            products=[dict(r) for r in ROWS],
            llm_view=f"1. curatare — [{ROWS[0]['id']}] A\n2. hidratare — [{ROWS[1]['id']}] B",
        )

    import src.tools.routine_tools as rt

    monkeypatch.setattr(rt, "run_planned_routine", fake_planned)
    args = {
        "family": "fata",
        "moment": "pm",
        "needs": ["skin_type:dry"],
        "budget_max": 200,
        "steps": [],
        "anchor": None,
    }
    llm = ScriptedLLM(
        [_call("routine_plan", args, "c1")],
        [_call("answer", _ans("Rutina ta de seară, 150 lei în total.", ["P1", "P2"]), "c2")],
    )
    served, ctx = await _run(llm, _ctx("fa-mi o rutina de seara pentru ten uscat sub 200 lei"))
    assert seen["args"].family == "fata" and seen["args"].concerns == ["dry"]
    assert seen["args"].budget_max == 200 and seen["args"].moment == "pm"
    out = json.loads(llm.inputs[1][-1]["output"])
    assert "[P1]" in out["routine"] and ROWS[0]["id"] not in out["routine"]
    assert served and _events(ctx, "assistant_turn")[0]["routine"] is True


# --- memoria, schemele, modul --------------------------------------------------------------------


def test_memory_is_defensive_and_never_reuses_a_number():
    assert Memory.from_state("stricat").handles == {}
    m = Memory.from_state({"h": {"P3": "a", "bad": "x"}, "n": 1})
    assert m.handles == {"P3": "a"} and m.next == 4
    for i in range(MAX_HANDLES + 5):
        m.handle_of(f"id{i}")
    assert len(m.handles) == MAX_HANDLES + 6, "în tur nu se scoate nimic"
    saved = Memory.from_state(m.to_state())
    assert len(saved.handles) == MAX_HANDLES and "P3" not in saved.handles
    assert saved.handle_of("a") == f"P{MAX_HANDLES + 9}", "un id scos primește un număr nou"
    first = next(iter(saved.handles))
    saved.handle_of(saved.handles[first], touch=False)
    assert next(iter(saved.handles)) == first, "fără `touch`, locul rămâne"
    with pytest.raises(Refused, match="is not a handle"):
        m.check("black-rouge-balm")
    with pytest.raises(Refused, match="unknown handle"):
        m.check("P1")


def test_distinct_names_tell_the_shades_apart():
    names = distinct_names({r["id"]: r["name"] for r in SHADES + ROWS})
    assert names[SHADES[0]["id"]] == "Cushion Glow (10C)"
    assert names[ROWS[0]["id"]] == "COSRX Crema A"


def _walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def test_every_schema_is_strict_and_names_products_only_by_handle():
    schemas = tool_schemas(MENUS)
    for tool in schemas:
        assert tool["strict"] is True
        for node in _walk(tool["parameters"]):
            if node.get("type") == "object":
                assert node["additionalProperties"] is False
                assert sorted(node["required"]) == sorted(node["properties"])
    text = json.dumps(schemas)
    assert text.count(json.dumps(HANDLE)[1:-1]) >= 7
    assert "business" not in text and "product_id" not in text


def test_the_mode_is_sticky_salted_and_scoped(monkeypatch):
    s = NS(assistant_agent_enabled=True, assistant_tenants="", assistant_canary_percent=100)
    business = NS(id="b", slug="sole-ro")
    assert aturn.assistant_mode(s, business, "conv") == "serve"
    assert (
        aturn.assistant_mode(NS(**{**vars(s), "assistant_canary_percent": 0}), business, "x")
        == "off"
    )
    assert (
        aturn.assistant_mode(NS(**{**vars(s), "assistant_tenants": "alt"}), business, "x") == "off"
    )
    assert (
        aturn.assistant_mode(NS(**{**vars(s), "assistant_agent_enabled": False}), business, "x")
        == "off"
    )
    buckets = {aturn.canary_bucket("b", f"c{i}") for i in range(300)}
    assert len(buckets) > 90, "bucket-urile acoperă procentul"
    from src.worker.stages.agent import canary_bucket as kernel_bucket

    same = sum(aturn.canary_bucket("b", f"c{i}") == kernel_bucket("b", f"c{i}") for i in range(300))
    assert same < 15, "saltul propriu: alt bucket decât canary-ul kernelului"


def test_the_flag_excludes_the_single_brain_and_the_turn_budget():
    from src.config import Settings

    with pytest.raises(ValueError, match="SINGLE_BRAIN_ENABLED"):
        Settings(ASSISTANT_AGENT_ENABLED=True, SINGLE_BRAIN_ENABLED=True)
    with pytest.raises(ValueError, match="TURN_BUDGET_ENFORCED"):
        Settings(
            ASSISTANT_AGENT_ENABLED=True, TURN_BUDGET_ENFORCED=True, TURN_DEADLINE_ENABLED=True
        )
    with pytest.raises(ValueError):
        Settings(LLM_REASONING_EFFORT_ASSISTANT="turbo")


async def test_a_figure_the_customer_wrote_is_not_an_allowed_price():
    """Recenzia NX-396: «am văzut-o la 10 lei pe alt site» făcea din 10 un preț permis (NX-121)."""
    llm = ScriptedLLM(
        [_call("search_catalog", _search(), "c1")],
        [_call("answer", _ans("COSRX Crema A costă 10 lei.", ["P1"]), "c2")],
        [_call("answer", _ans("COSRX Crema A costă 10 lei.", ["P1"]), "c3")],
    )
    served, ctx = await _run(llm, _ctx("am vazut-o la 10 lei pe alt site"))
    assert served is False and ctx.events[-1].properties["reason"] == "gate_failed"


async def test_a_handle_in_the_text_becomes_the_product_name():
    llm = ScriptedLLM(
        [_call("search_catalog", _search(), "c1")],
        [_call("answer", _ans("P1 hidratează mai bine decât P2.", ["P1"], reason="ca P2"), "c2")],
    )
    served, ctx = await _run(llm)
    assert served
    assert ctx.reply.rich.intro == "COSRX Crema A hidratează mai bine decât COSRX Crema B."
    assert ctx.reply.rich.items[0].reason == "ca COSRX Crema B"


def test_an_unknown_handle_in_the_text_is_rejected():
    a = agate.check_answer(
        _ans("Ia P7."),
        handles={},
        names={},
        short_names={},
        rows={},
        shown_sets=[],
        grounded=set(),
        sources=[],
        order_found=False,
        max_cards=6,
        max_suggestions=5,
        notes_max=500,
    )
    assert "handle_in_text" in a.rejected


async def test_a_known_blocked_product_does_not_turn_a_thank_you_into_an_exclusion(_catalog):
    """Recenzia NX-396 (P1): re-citirea produselor de mai devreme nu intră în decizia turului."""
    state = ConversationState.from_jsonb(
        {"assistant": {"h": {"P1": ROWS[0]["id"], "P2": ROWS[1]["id"]}, "n": 3}}
    )
    _catalog["blocked"] = {ROWS[1]["id"]}
    llm = ScriptedLLM([_call("answer", _ans("Cu plăcere!"), "c1")])
    served, ctx = await _run(llm, _ctx("mersi", state=state))
    assert served and ctx.reply.text == "Cu plăcere!"
    assert ctx.safety_decision is None or not ctx.safety_decision.blocked


async def test_a_failing_tool_is_a_result_not_the_end_of_the_turn(monkeypatch):
    async def boom(self, a):
        raise ConnectionError("db")

    from src.assistant.tools import Tools

    monkeypatch.setattr(Tools, "_search_catalog", boom)
    llm = ScriptedLLM(
        [_call("search_catalog", _search(), "c1")],
        [_call("answer", _ans("Nu pot căuta acum, revin."), "c2")],
    )
    served, _ctx_ = await _run(llm)
    assert served and json.loads(llm.inputs[1][-1]["output"])["ok"] is False


async def test_the_last_round_forces_the_answer(monkeypatch):
    monkeypatch.setattr(get_settings(), "assistant_max_rounds", 2)
    seen = []

    class Rec(ScriptedLLM):
        async def respond_round(self, **kw):
            seen.append(kw.get("tool_choice"))
            return await super().respond_round(**kw)

    llm = Rec(
        [_call("search_catalog", _search(), "c1")],
        [_call("answer", _ans("Uite.", ["P1"]), "c2")],
    )
    served, _ctx_ = await _run(llm)
    assert served and seen == ["required", {"type": "function", "name": "answer"}]


async def test_calls_after_an_accepted_answer_are_not_executed(monkeypatch):
    ran = []

    async def fake_run_tool(ctx, deps, name, args):
        ran.append(name)
        return NS(ok=True, llm_view="", error=None, state_patch={}, prices=[])

    import src.tools.base as base

    monkeypatch.setattr(base, "run_tool", fake_run_tool)
    shown = [{"product_id": ROWS[0]["id"], "name": ROWS[0]["name"], "price": 100.0}]
    state = ConversationState.from_jsonb(
        {"displayed_products": shown, "assistant": {"h": {"P1": ROWS[0]["id"]}, "n": 2}}
    )
    llm = ScriptedLLM(
        [
            _call("answer", _ans("Gata."), "c1"),
            _call("add_to_cart", {"handle": "P1", "quantity": 1}, "c2"),
        ]
    )
    served, _ctx_ = await _run(llm, _ctx("ok", state=state))
    assert served and ran == []


async def test_a_mutation_survives_a_failed_turn_and_is_told(monkeypatch):
    async def fake_run_tool(ctx, deps, name, args):
        ctx.emit("cart_updated", n=1)
        return NS(ok=True, llm_view="adăugat", error=None, state_patch={"cart": [1]}, prices=[])

    import src.tools.base as base

    monkeypatch.setattr(base, "run_tool", fake_run_tool)
    shown = [{"product_id": ROWS[0]["id"], "name": ROWS[0]["name"], "price": 100.0}]
    state = ConversationState.from_jsonb(
        {"displayed_products": shown, "assistant": {"h": {"P1": ROWS[0]["id"]}, "n": 2}}
    )
    ctx = _ctx("pune-l in cos", state=state)
    ctx.business.domain_pack = DomainPack(
        vertical="ecommerce", kernel_sentences={"ro": {"cart_added": "Am pus {product} în coș."}}
    )
    llm = ScriptedLLM(
        [_call("add_to_cart", {"handle": "P1", "quantity": 1}, "c1")],
        RuntimeError("provider down"),
    )
    served, ctx = await _run(llm, ctx)
    assert served is True and ctx.reply.text == "Am pus COSRX Crema A în coș."
    assert ctx.state_patch["cart"] == [1]
    assert [e.type for e in ctx.events] == ["assistant_fallback", "cart_updated"]


def test_an_empty_menu_never_becomes_an_empty_enum():
    empty = Menus(categories=(), product_types=(), brands=(), needs=(), families=("fata",))
    for node in _walk(tool_schemas(empty)):
        assert node.get("enum", ["x"]) != [], "modul strict refuză un enum gol"


def test_the_enums_fit_the_strict_mode_limits():
    """Mărimile SOLE (45 de rafturi, 54 de tipuri, 100 de mărci, 196 de nevoi): sub 1.000 de
    valori pe schemă și cel mult 250 pe proprietate."""
    big = Menus(
        categories=tuple(f"raft-{i}" for i in range(45)),
        product_types=tuple(f"tip {i}" for i in range(54)),
        brands=tuple(f"MARCA {i}" for i in range(100)),
        needs=tuple(f"fateta:valoare-{i}" for i in range(196)),
        families=("corp", "fata", "machiaj", "par"),
        moments=("am", "pm"),
        steps=tuple(f"pas{i}" for i in range(20)),
    )
    for tool in tool_schemas(big):
        enums = [n["enum"] for n in _walk(tool["parameters"]) if "enum" in n]
        assert sum(len(e) for e in enums) < 1000, tool["name"]
        assert all(len(e) <= 250 for e in enums), tool["name"]


def test_brands_menu_is_cached_per_tenant(monkeypatch):
    calls = []

    async def brands(conn, business_id):
        calls.append(business_id)
        return ["COSRX"]

    import src.db.queries.catalog as cat

    monkeypatch.setattr(cat, "list_brand_names", brands)
    amenus._brands_cache.clear()
    deps = PipelineDeps()
    assert asyncio.run(amenus._brands(deps, "b")) == ("COSRX",)
    assert asyncio.run(amenus._brands(deps, "b")) == ("COSRX",)
    assert calls == ["b"]


def test_the_state_carries_the_memory_on_both_shapes():
    from src.conversation.state_v2 import PASSTHROUGH_KEYS

    assert "assistant" in PASSTHROUGH_KEYS
    state = ConversationState.from_jsonb({"assistant": {"h": {"P1": "x"}, "n": 2, "notes": "n"}})
    assert state.assistant["notes"] == "n"
    assert ConversationState.from_jsonb({"assistant": "stricat"}).assistant == {}


# --- legarea în `agent_stage` --------------------------------------------------------------------


async def test_agent_stage_serves_the_assistant_turn_before_the_old_path(monkeypatch):
    import src.worker.stages.agent as stage

    s = get_settings()
    monkeypatch.setattr(s, "assistant_agent_enabled", True)
    monkeypatch.setattr(s, "assistant_canary_percent", 100)
    monkeypatch.setattr(s, "assistant_tenants", "")

    async def served(ctx, deps):
        ctx.set_reply("de la agent", cacheable=False)
        return True

    async def old_path(*a, **kw):
        raise AssertionError("calea de azi n-are voie să ruleze pe un tur servit")

    monkeypatch.setattr(aturn, "run_assistant_turn", served)
    monkeypatch.setattr(stage, "try_pre_intents", old_path)
    ctx = _ctx()
    await stage.agent_stage(ctx, PipelineDeps(llm=ScriptedLLM()))
    assert ctx.reply.text == "de la agent"


async def test_agent_stage_falls_through_when_the_assistant_does_not_serve(monkeypatch):
    import src.worker.stages.agent as stage

    s = get_settings()
    monkeypatch.setattr(s, "assistant_agent_enabled", True)
    monkeypatch.setattr(s, "assistant_canary_percent", 100)
    monkeypatch.setattr(s, "assistant_tenants", "")

    async def not_served(ctx, deps):
        return False

    async def old_path(ctx, deps, **kw):
        ctx.set_reply("calea de azi", cacheable=False)
        return True

    monkeypatch.setattr(aturn, "run_assistant_turn", not_served)
    monkeypatch.setattr(stage, "try_pre_intents", old_path)
    ctx = _ctx()
    await stage.agent_stage(ctx, PipelineDeps(llm=ScriptedLLM()))
    assert ctx.reply.text == "calea de azi"


async def test_with_the_flag_off_the_assistant_is_never_called(monkeypatch):
    import src.worker.stages.agent as stage

    monkeypatch.setattr(get_settings(), "assistant_agent_enabled", False)

    async def never(ctx, deps):
        raise AssertionError("flag stins: agentul nu rulează")

    async def old_path(ctx, deps, **kw):
        ctx.set_reply("calea de azi", cacheable=False)
        return True

    monkeypatch.setattr(aturn, "run_assistant_turn", never)
    monkeypatch.setattr(stage, "try_pre_intents", old_path)
    ctx = _ctx()
    await stage.agent_stage(ctx, PipelineDeps(llm=ScriptedLLM()))
    assert ctx.reply.text == "calea de azi"


def test_the_stage_imports_the_assistant_only_under_the_flag():
    """Flag stins = zero import: `src.assistant` apare în `agent.py` doar în ramura flagului."""
    import ast
    from pathlib import Path

    tree = ast.parse(Path("src/worker/stages/agent.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("src.assistant"):
            assert node.col_offset > 4, "importul e înăuntrul ramurii, nu la nivel de modul"
    top = [n for n in tree.body if isinstance(n, ast.ImportFrom)]
    assert not [n for n in top if (n.module or "").startswith("src.assistant")]
