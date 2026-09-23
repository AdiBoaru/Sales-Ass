"""NX-316 felia 2 — mutări PESTE setul afișat: `choose_within` și `fit_question`.

Turul real «vreau o crema de hidratare» (2026-09-23): iZi a oferit „Am ten uscat, ce îmi recomanzi
dintre acestea" și „Este potrivită pentru ten sensibil". Nativx avea doar îngustări din meniu și un
raft oarecare, fiindcă niciun fel de mutare nu lucra pe setul afișat.

Pure (plan, construcție, selecție, recunoaștere) + handlerele cu stub-uri de DB: `fit_question` se
servește determinist din fișă, `choose_within` restrânge setul la cele afișate cu valoarea. ZERO
OpenAI, zero DB."""

from __future__ import annotations

from dataclasses import replace

import pytest

from src.agent import deterministic, planner
from src.agent import finalize as finalize_mod
from src.config import get_settings
from src.conversation import chip_moves
from src.conversation.chip_press import facet_of, product_ids, recognize
from src.conversation.subject import SUBJECT_KEY
from src.domain.facets import build_facets
from src.domain.loader import load_domain_pack
from src.models import (
    BusinessConfig,
    Contact,
    InboundMessage,
    ProductRef,
    Route,
    RouteDecision,
    TurnContext,
)
from src.worker.runner import PipelineDeps
from src.worker.stages import agent as agent_mod
from src.worker.stages.agent import agent_stage

_BIZ = BusinessConfig(id="b", slug="d", name="D")
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
# Pachetul REAL de ecommerce (șabloanele din JSON), cu fațetele de mai sus: testele judecă exact
# copy-ul care pleacă la client.
PACK = replace(load_domain_pack(_BIZ), facets=_FACETS)

PHRASES = {
    ("skin_type", "dry"): "ten uscat",
    ("skin_type", "oily"): "ten gras",
    ("skin_type", "sensitive"): "ten sensibil",
}

# Șase creme: 3 pentru ten uscat, 2 pentru ten gras, 1 pentru ten sensibil; ultima fără fațetă.
CARDS = [
    {"product_id": "p1", "name": "SOME BY MI Yuja Niacin Cream", "price": 110.0,
     "attributes": {"skin_type": ["dry"]}},
    {"product_id": "p2", "name": "BELIF Aqua Bomb Cream", "price": 159.0,
     "attributes": {"skin_type": ["oily", "dry"]}},
    {"product_id": "p3", "name": "Dear Klairs Rich Moist Cream", "price": 99.0,
     "attributes": {"skin_type": ["dry"]}},
    {"product_id": "p4", "name": "COSRX Oil Free Lotion", "price": 89.0,
     "attributes": {"skin_type": ["oily"]}},
    {"product_id": "p5", "name": "PURITO Centella Unscented Cream", "price": 79.0,
     "attributes": {"skin_type": ["sensitive"]}},
    {"product_id": "p6", "name": "IUNIK Beta Glucan Cream", "price": 85.0, "attributes": {}},
]  # fmt: skip


def _plan(cards=CARDS, **k):
    return chip_moves.plan_facets(cards, _FACETS, **k)


def _moves(cards=CARDS, plan=None, phrases=PHRASES, pack=PACK):
    plan = plan or _plan(cards)
    built = chip_moves.from_facets(cards, plan, _FACETS, phrases, locale="ro")
    return chip_moves.renderable(built, pack, "ro")


def _text(move_id: str, cards=CARDS) -> str:
    move = next(m for m in _moves(cards) if m.move_id == move_id)
    return chip_moves.render_move(move, PACK, "ro")


# --- planul și construcția, pure -------------------------------------------------------------


def test_choose_within_offers_the_values_the_shown_set_splits_on():
    plan = _plan()
    assert plan.facet == "skin_type"
    assert plan.values == (("dry", 3), ("oily", 2), ("sensitive", 1))
    chosen = [m for m in _moves() if m.kind == "choose_within"]
    assert [(m.move_id, m.evidence) for m in chosen] == [
        ("choose_within:skin_type:dry", 3),
        ("choose_within:skin_type:oily", 2),
        ("choose_within:skin_type:sensitive", 1),
    ]
    assert _text("choose_within:skin_type:dry") == "Pentru ten uscat, ce aleg dintre acestea?"


def test_selection_keeps_the_two_best_values_and_puts_them_before_menu_refinements():
    """Dovada unei îngustări din meniu e din CATALOG (sute), a lui `choose_within` din setul
    afișat: fără prioritate, «aleg dintre acestea» n-ar ieși niciodată pe un tur cu carduri."""
    menu = chip_moves.ChipMove(
        kind="refine_facet",
        move_id="refine_facet:concerns:hydration",
        anchor="hidratare",
        slots=(("slot", "hidratare"),),
        evidence=500,
    )
    picked = chip_moves.select(
        [menu, *_moves()], slots=5, role_order=chip_moves.roles_for(["recommend"])
    )
    forward = [m.move_id for m in picked if m.role == "forward"]
    # Primul chip e alegerea dintre cele afișate; în rol, felurile alternează (round-robin), deci
    # îngustarea din meniu rămâne a doua, nu dispare.
    assert picked[0].move_id == "choose_within:skin_type:dry"
    assert forward == [
        "choose_within:skin_type:dry",
        "refine_facet:concerns:hydration",
        "choose_within:skin_type:oily",
    ]


def test_a_known_facet_is_not_offered_as_a_choice():
    assert _plan(known=frozenset({"skin_type"})).facet is None


def test_a_single_valued_set_offers_no_choice():
    cards = [c for c in CARDS if c["product_id"] in ("p1", "p3")]
    assert _plan(cards).facet is None
    assert not [m for m in _moves(cards) if m.kind == "choose_within"]


def test_fit_question_is_asked_only_where_the_sheet_knows_the_facet():
    cards = [CARDS[5], CARDS[0]]  # p6 fără skin_type, apoi p1
    fits = [m for m in _moves(cards, plan=chip_moves.FacetPlan(fit_values=(("skin_type", "dry"),)))
            if m.kind == "fit_question"]  # fmt: skip
    assert [m.move_id for m in fits] == ["fit_question:p1:skin_type:dry"]


def test_fit_question_text_and_parts():
    text = _text("fit_question:p1:skin_type:dry")
    assert text == "SOME BY MI Yuja Niacin Cream merge pentru ten uscat?"
    move = next(m for m in _moves() if m.move_id == "fit_question:p1:skin_type:dry")
    assert product_ids(move) == ("p1",) and facet_of(move) == ("skin_type", "dry")


def test_spoken_value_is_asked_first_unless_every_card_carries_it():
    # «ten sensibil» rostit și purtat doar de p5 ⇒ se întreabă pe primele carduri.
    plan = _plan(spoken=[("skin_type", "sensitive")])
    assert plan.fit_values[0] == ("skin_type", "sensitive")
    # «ten uscat» rostit și purtat de TOATE ⇒ „da" garantat ⇒ nu se întreabă.
    dry_only = [c for c in CARDS if c["product_id"] in ("p1", "p3")]
    assert ("skin_type", "dry") not in _plan(dry_only, spoken=[("skin_type", "dry")]).fit_values


def test_value_without_phrase_is_not_offered():
    moves = _moves(phrases={("skin_type", "oily"): "ten gras"})
    assert {m.move_id for m in moves if m.kind == "choose_within"} == {
        "choose_within:skin_type:oily"
    }


def test_pack_without_the_template_does_not_offer_the_kind():
    bare = replace(PACK, chip_templates={})
    assert _moves(pack=bare) == []


def test_offered_before_is_not_repeated():
    plan = _plan()
    built = chip_moves.from_facets(
        CARDS, plan, _FACETS, PHRASES, offered_before=["choose_within:skin_type:dry"]
    )
    assert "choose_within:skin_type:dry" not in {m.move_id for m in built}


def test_choose_within_names_no_product():
    move = next(m for m in _moves() if m.kind == "choose_within")
    assert product_ids(move) == ()


def test_wanted_phrases_reads_only_facet_moves():
    offered = ["link:p1", "choose_within:skin_type:dry", "fit_question:p1:skin_type:oily"]
    assert chip_moves.wanted_phrases(offered) == {"skin_type": ("dry", "oily")}
    assert chip_moves.wanted_phrases(["link:p1", "compare:p1:p2"]) == {}


# --- recunoașterea, pură ---------------------------------------------------------------------


_SHOWN = [{k: c[k] for k in ("product_id", "name", "price")} for c in CARDS]  # starea: fără fațete


@pytest.mark.parametrize(
    "move_id", ["choose_within:skin_type:dry", "fit_question:p1:skin_type:dry"]
)
def test_facet_press_is_recognized_from_the_state_alone(move_id):
    """Starea ține doar `{id, nume, preț}` (P8): tot ce trebuie e în `move_id` + fraza valorii."""
    move = recognize(_text(move_id), _SHOWN, [move_id], PACK, "ro", phrases=PHRASES)
    assert move is not None and move.move_id == move_id


def test_different_diacritics_are_the_same_press():
    typed = "pentru ten uscat ce aleg dintre ACESTEA"
    move = recognize(typed, _SHOWN, ["choose_within:skin_type:dry"], PACK, "ro", phrases=PHRASES)
    assert move is not None


def test_without_phrases_facet_moves_are_not_recognized():
    text = _text("choose_within:skin_type:dry")
    assert recognize(text, _SHOWN, ["choose_within:skin_type:dry"], PACK, "ro") is None


def test_empty_state_recognizes_nothing():
    text = _text("fit_question:p1:skin_type:dry")
    assert (
        recognize(text, [], ["fit_question:p1:skin_type:dry"], PACK, "ro", phrases=PHRASES) is None
    )


# --- handlerele ------------------------------------------------------------------------------


def _ctx(body: str = "x", offered=()) -> TurnContext:
    ctx = TurnContext(
        turn_id="t",
        business=replace(_BIZ, domain_pack=PACK),
        contact=Contact(id="c", business_id="b"),
        message=InboundMessage(provider_msg_id="m", body=body),
        conversation_id="conv",
    )
    ctx.route = RouteDecision(route=Route.SALES)
    ctx.state.displayed_products = [
        ProductRef(c["product_id"], c["name"], c["price"]) for c in CARDS
    ]
    ctx.state.offered_chips = list(offered)
    return ctx


def _catalog(served: list | None = None):
    async def fake(conn, business_id, ids, **k):
        if served is not None:
            served.append(list(ids))
        by_id = {c["product_id"]: c for c in CARDS}
        return [{"id": i, **{k: v for k, v in by_id[i].items() if k != "product_id"}}
                for i in ids if i in by_id]  # fmt: skip

    return fake


class _Pass:
    def gate(self, ctx, products, purpose):
        return (products,)


class _NoModel:
    async def embed(self, texts, *, model=None):
        return [[0.0] * 8 for _ in texts]

    async def run_tool_loop(self, *a, **k):
        raise AssertionError("fit_question se servește fără model")

    async def complete(self, *a, **k):
        raise AssertionError("fit_question se servește fără model")


def _deps():
    return PipelineDeps(conn=object(), redis=None, llm=_NoModel())


def _move(move_id: str) -> chip_moves.ChipMove:
    return next(m for m in _moves() if m.move_id == move_id)


async def test_choose_within_restricts_to_shown_products_with_the_value(monkeypatch):
    served: list = []
    monkeypatch.setattr(planner, "get_products_by_ids", _catalog(served))
    ctx = _ctx()
    ctx.chip_move = _move("choose_within:skin_type:dry")

    got = await planner.resolve_choose_within(ctx, _deps(), policy=_Pass())

    assert served == [["p1", "p2", "p3", "p4", "p5", "p6"]]
    assert [p["id"] for p in got] == ["p1", "p2", "p3"]
    event = next(e.properties for e in ctx.events if e.type == "choose_within")
    assert event["served"] == 3


async def test_choose_within_without_a_recognized_press_reads_nothing(monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("fără chip nu se citește nimic")

    monkeypatch.setattr(planner, "get_products_by_ids", boom)
    assert await planner.resolve_choose_within(_ctx(), _deps(), policy=_Pass()) == []


def test_choose_within_seed_carries_the_value_and_only_the_set():
    ctx = _ctx("Pentru ten uscat, ce aleg dintre acestea?")
    products = [{"id": c["product_id"], **c} for c in CARDS[:3]]
    call, result = planner.choose_within_seed_messages(ctx, products, key="dry")
    args = call["tool_calls"][0]["function"]
    assert args["name"] == "search_products" and '"concerns": ["dry"]' in args["arguments"]
    assert result["tool_call_id"] == call["tool_calls"][0]["id"]
    # Determinist: același tur ⇒ aceiași octeți (prompt caching pe reluări).
    again = planner.choose_within_seed_messages(ctx, products, key="dry")
    assert again[0]["tool_calls"][0]["id"] == call["tool_calls"][0]["id"]


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("dry", "Da, fișa produsului menționează ten uscat printre recomandări."),
        ("oily", "Fișa produsului nu menționează ten gras printre recomandări."),
    ],
)
def test_fit_answer_comes_from_the_sheet(key, expected):
    ctx = _ctx()
    lead = deterministic.fit_lead(ctx, "skin_type", key, PHRASES[("skin_type", key)])
    assert lead({"attributes": {"skin_type": ["dry"]}}) == expected


def test_fit_on_an_unknown_sheet_says_nothing():
    ctx = _ctx()
    lead = deterministic.fit_lead(ctx, "skin_type", "dry", "ten uscat")
    assert lead({"attributes": {}}) is None
    assert next(e.properties for e in ctx.events if e.type == "fit_question")["verdict"] == (
        "unknown"
    )


async def test_fit_question_press_is_served_from_facts_without_the_model(monkeypatch):
    monkeypatch.setattr(get_settings(), "chip_moves_v2_enabled", True, raising=False)
    monkeypatch.setattr(deterministic, "get_products_by_ids", _catalog())

    async def fake_vocab(deps, business_id):
        return object()

    def fake_phrases(vocab, pack, facet, keys, *, locale):
        return {k: PHRASES[(facet, k)] for k in keys if (facet, k) in PHRASES}

    monkeypatch.setattr(agent_mod, "get_vocabulary", fake_vocab)
    monkeypatch.setattr("src.catalog.clarify_menu.value_phrases", fake_phrases)

    async def _cats(conn, business_id):
        return ["Creme"]

    async def _aliases(conn, business_id, **k):
        return []

    monkeypatch.setattr(agent_mod, "list_category_names", _cats)
    monkeypatch.setattr(agent_mod, "list_routing_aliases", _aliases)

    move_id = "fit_question:p4:skin_type:dry"
    fit = chip_moves.fit_question_move(
        "p4", "COSRX Oil Free Lotion", "skin_type", "dry", "ten uscat"
    )
    text = chip_moves.render_move(chip_moves.renderable([fit], PACK, "ro")[0], PACK, "ro")
    ctx = _ctx(text, offered=[move_id])

    await agent_stage(ctx, _deps())

    assert ctx.reply is not None
    assert ctx.reply.text.startswith("Fișa produsului nu menționează ten uscat")
    pressed = next(e.properties for e in ctx.events if e.type == "chip_pressed")
    assert (pressed["kind"], pressed["recognized"], pressed["handler"]) == (
        "fit_question",
        True,
        "fit_question",
    )


async def test_choose_within_press_is_counted_and_left_to_the_turn():
    ctx = _ctx()
    served = await deterministic.serve_chip_move(ctx, _deps(), _move("choose_within:skin_type:dry"))
    assert served is False
    pressed = next(e.properties for e in ctx.events if e.type == "chip_pressed")
    assert pressed["handler"] == "choose_within"


# --- flag + eșecuri --------------------------------------------------------------------------


async def test_facet_moves_are_inert_with_the_flag_off(monkeypatch):
    monkeypatch.setattr(get_settings(), "chip_moves_v2_enabled", False, raising=False)

    async def boom(*a, **k):
        raise AssertionError("flag stins ⇒ nicio citire")

    monkeypatch.setattr("src.catalog.vocabulary_cache.get_vocabulary", boom)
    assert await finalize_mod._facet_moves(_ctx(), _deps(), CARDS) == []


async def test_facet_moves_fail_open_when_the_vocabulary_is_down(monkeypatch):
    monkeypatch.setattr(get_settings(), "chip_moves_v2_enabled", True, raising=False)

    async def down(*a, **k):
        raise RuntimeError("db jos")

    monkeypatch.setattr("src.catalog.vocabulary_cache.get_vocabulary", down)
    assert await finalize_mod._facet_moves(_ctx(), _deps(), CARDS) == []


async def test_facet_moves_on_with_phrases(monkeypatch):
    monkeypatch.setattr(get_settings(), "chip_moves_v2_enabled", True, raising=False)

    async def vocab(*a, **k):
        return object()

    def phrases(vocab, pack, facet, keys, *, locale):
        return {k: PHRASES[(facet, k)] for k in keys if (facet, k) in PHRASES}

    monkeypatch.setattr("src.catalog.vocabulary_cache.get_vocabulary", vocab)
    monkeypatch.setattr("src.catalog.clarify_menu.value_phrases", phrases)
    ctx = _ctx()
    ctx.state.search_constraints = {SUBJECT_KEY: {"needs": [["skin_type", "sensitive"]]}}
    moves = await finalize_mod._facet_moves(ctx, _deps(), CARDS)
    kinds = {m.kind for m in moves}
    assert kinds == {"choose_within", "fit_question"}
    # «ten sensibil» rostit ⇒ întrebat pe primele carduri, unde fișa cunoaște fațeta.
    assert "fit_question:p1:skin_type:sensitive" in {m.move_id for m in moves}


async def test_brain_seed_puts_only_the_chosen_set_in_the_turn_evidence(monkeypatch):
    """Pe creierul unic setul intră în `run.retrieved` (altfel validatorul l-ar respinge ca
    negroundat) și în fața modelului ca seed, ca la «mai ieftin»."""
    from types import SimpleNamespace

    monkeypatch.setattr(planner, "get_products_by_ids", _catalog())
    monkeypatch.setattr(agent_mod.SafetyPolicy, "for_turn", classmethod(lambda cls, ctx: _Pass()))
    ctx = _ctx("Pentru ten gras, ce aleg dintre acestea?")
    ctx.chip_move = _move("choose_within:skin_type:oily")
    run = SimpleNamespace(retrieved=[])

    seed = await agent_mod._choose_within_seed(ctx, _deps(), run=run)

    assert [p["id"] for p in run.retrieved] == ["p2", "p4"]
    assert (
        seed is not None
        and '"concerns": ["oily"]' in seed[0]["tool_calls"][0]["function"]["arguments"]
    )
    ctx.chip_move = None
    assert await agent_mod._choose_within_seed(ctx, _deps(), run=run) is None
