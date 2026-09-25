"""NX-326 (kernel v1.0, pasul 0) — scurtăturile de link/comparație servesc produsul NUMIT.

B1: «trimite-mi linkul la Yuja Niacin» cu cinci carduri pe ecran servea linkurile tuturor celor
cinci. B2: «compară Wishtrend Vitamin cu Yuja Niacin» compara primele două carduri afișate.
Regresiile de mai jos pică pe codul de dinainte și trec cu `NAMED_SHORTCUT_TARGETS_ENABLED`.
Stub-uri DB/LLM, zero apeluri reale."""

import pytest

from src.agent import deterministic as det
from src.agent.reference_resolver import named_targets
from src.config import get_settings
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

SHOWN = [
    ProductRef("p1", "COSRX Advanced Snail 92 All In One Cream", 89.0),
    ProductRef("p2", "Beauty of Joseon Dynasty Cream", 95.0),
    ProductRef("p3", "SOME BY MI Yuja Niacin Brightening Moisture Gel Cream", 110.0),
    ProductRef("p4", "By Wishtrend Pure Vitamin C 21.5 Advanced Serum", 120.0),
    ProductRef("p5", "ROUND LAB 1025 Dokdo Cream", 99.0),
]


@pytest.fixture(autouse=True)
def _stub_prompt_inputs(monkeypatch):
    async def _cats(conn, business_id):
        return ["Creme"]

    async def _aliases(conn, business_id, **k):
        return []

    monkeypatch.setattr(agent_mod, "list_category_names", _cats)
    monkeypatch.setattr(agent_mod, "list_routing_aliases", _aliases)


@pytest.fixture
def flag(monkeypatch):
    def _set(value: bool) -> None:
        monkeypatch.setattr(get_settings(), "named_shortcut_targets_enabled", value)

    _set(True)
    return _set


class _RecordLLM:
    def __init__(self):
        self.loop_called = False

    async def embed(self, texts, *, model=None):
        return [[0.0] * 8 for _ in texts]

    async def run_tool_loop(self, *a, **k):
        self.loop_called = True
        return ""

    async def complete(self, *a, **k):
        raise AssertionError("scurtătura nu cheamă modelul")


def _ctx(body: str, shown=SHOWN) -> TurnContext:
    ctx = TurnContext(
        turn_id="t",
        business=BusinessConfig(id="b", slug="d", name="D"),
        contact=Contact(id="c", business_id="b"),
        message=InboundMessage(provider_msg_id="m", body=body),
        conversation_id="conv",
    )
    ctx.route = RouteDecision(route=Route.SALES)
    ctx.state.displayed_products = list(shown)
    return ctx


def _deps(llm=None) -> PipelineDeps:
    return PipelineDeps(conn=object(), redis=None, llm=llm or _RecordLLM())


@pytest.fixture
def catalog(monkeypatch):
    """`get_products_by_ids` fals: rândurile cerute, în ordinea cerută, înregistrate."""
    calls: list[list[str]] = []
    by_id = {r.product_id: r for r in SHOWN}

    async def fake_by_ids(conn, business_id, ids, **k):
        calls.append(list(ids))
        return [
            {
                "id": pid,
                "name": by_id[pid].name,
                "price": by_id[pid].price,
                "url": f"https://shop.ro/p/{pid}",
            }
            for pid in ids
            if pid in by_id
        ]

    monkeypatch.setattr(det, "get_products_by_ids", fake_by_ids)
    return calls


@pytest.fixture(autouse=True)
def _planner_catalog(monkeypatch):
    """Turul care pleacă la model rehidratează setul afișat în planner; nu e ce măsurăm aici."""

    async def fake_by_ids(conn, business_id, ids, **k):
        return []

    monkeypatch.setattr("src.agent.planner.get_products_by_ids", fake_by_ids)


@pytest.fixture
def compared(monkeypatch):
    """`serve_comparison` fals: înregistrează ce produse s-ar compara."""
    seen: list[list[str]] = []

    async def fake_serve(ctx, deps, ids):
        seen.append(list(ids))
        ctx.set_reply("tabel", cacheable=False)
        return True

    monkeypatch.setattr(det, "serve_comparison", fake_serve)
    return seen


# --- B1: link ------------------------------------------------------------------------------


async def test_b1_link_to_a_named_product_serves_only_that_product(flag, catalog):
    ctx = _ctx("Trimite-mi linkul la Yuja Niacin")
    await agent_stage(ctx, _deps())

    assert catalog == [["p3"]]
    assert ctx.reply.offer is not None and ctx.reply.offer.url == "https://shop.ro/p/p3"
    assert [p.get("product_id") or p.get("id") for p in ctx.reply.products] == ["p3"]


async def test_b1_link_without_a_name_still_serves_every_anchor(flag, catalog):
    ctx = _ctx("Trimite-mi linkul, te rog")
    await agent_stage(ctx, _deps())

    assert catalog == [["p1", "p2", "p3", "p4", "p5"]]


async def test_b1_link_to_a_word_shared_by_two_serves_both_candidates(flag, catalog):
    shown = [
        ProductRef("p1", "IT'S SKIN The Fresh Blueberries Toner", 50.0),
        ProductRef("p2", "IT'S SKIN The Fresh Tomato Toner", 50.0),
        ProductRef("p3", "COSRX Low pH Cleanser", 60.0),
    ]
    ctx = _ctx("dă-mi linkul la The Fresh", shown=shown)
    await agent_stage(ctx, _deps())

    assert catalog == [["p1", "p2"]]
    assert any(
        e.type == "shortcut_targets" and e.properties["source"] == "ambiguous" for e in ctx.events
    )


async def test_b1_flag_off_is_the_old_behavior(flag, catalog):
    flag(False)
    ctx = _ctx("Trimite-mi linkul la Yuja Niacin")
    await agent_stage(ctx, _deps())

    assert catalog == [["p1", "p2", "p3", "p4", "p5"]]
    assert not any(e.type == "shortcut_targets" for e in ctx.events)


# --- B2: comparație ------------------------------------------------------------------------


async def test_b2_compare_two_named_products_in_the_order_asked(flag, compared):
    ctx = _ctx("Compară Wishtrend Vitamin cu Yuja Niacin")
    await agent_stage(ctx, _deps())

    assert compared == [["p4", "p3"]]


async def test_b2_compare_ordinals_other_than_the_first_two(flag, compared):
    ctx = _ctx("compară primul cu al treilea")
    await agent_stage(ctx, _deps())

    assert compared == [["p1", "p3"]]


async def test_b2_compare_without_names_keeps_the_first_two(flag, compared):
    ctx = _ctx("compară-le pe primele două")
    await agent_stage(ctx, _deps())

    assert compared == [["p1", "p2"]]


async def test_b2_compare_a_single_named_product_goes_to_the_model(flag, compared):
    llm = _RecordLLM()
    ctx = _ctx("compară Yuja Niacin cu celelalte")
    await agent_stage(ctx, _deps(llm))

    assert compared == []
    assert llm.loop_called
    assert any(
        e.type == "shortcut_targets" and e.properties["outcome"] == "model" for e in ctx.events
    )


async def test_b2_flag_off_is_the_old_behavior(flag, compared):
    flag(False)
    ctx = _ctx("Compară Wishtrend Vitamin cu Yuja Niacin")
    await agent_stage(ctx, _deps())

    assert compared == [["p1", "p2"]]


# --- producătorul pur ----------------------------------------------------------------------


def test_named_targets_orders_by_position_in_the_message():
    got = named_targets("vreau Dokdo, apoi snail", SHOWN, locale="ro")
    assert got.indices == (4, 0)
    assert got.candidates == ()


def test_named_targets_mixes_names_and_ordinals_without_duplicates():
    got = named_targets("primul si COSRX Snail si al doilea", SHOWN, locale="ro")
    assert got.indices == (0, 1)


def test_named_targets_ignores_a_word_every_product_shares():
    got = named_targets("linkul la crema", SHOWN[:3], locale="ro")
    # „cream" e pe toate trei, iar „crema" nici nu e același cuvânt: nimic numit
    assert got.indices == () and got.candidates == ()


def test_named_targets_ignores_stopwords_and_short_tokens():
    shown = [ProductRef("a", "Crema pentru ochi", 1.0), ProductRef("b", "Ser cu vitamina", 2.0)]
    assert named_targets("linkul pentru asta", shown, locale="ro").indices == ()


def test_named_targets_a_shared_word_is_ambiguous_not_a_pick():
    shown = [
        ProductRef("a", "IT'S SKIN The Fresh Blueberries Toner", 1.0),
        ProductRef("b", "IT'S SKIN The Fresh Tomato Toner", 1.0),
        ProductRef("c", "COSRX Low pH Cleanser", 1.0),
    ]
    got = named_targets("linkul la The Fresh", shown, locale="ro")
    assert got.indices == ()
    assert got.candidates == (0, 1)


def test_named_targets_empty_inputs():
    assert named_targets("", SHOWN, locale="ro").indices == ()
    assert named_targets("linkul la Yuja", [], locale="ro").indices == ()
