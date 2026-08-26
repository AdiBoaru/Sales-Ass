"""NX-263 — extragerea DRUMULUI + declanșarea deterministă după add-to-cart.

Pur (zero DB, zero OpenAI). Două straturi:

  1. `walk_chain` — funcția care transformă o traversare într-o SECVENȚĂ. Aici se apără exact
     defectul pe care l-ar avea varianta naivă: a lua „cel mai bun produs de la fiecare adâncime"
     coase o rutină din ramuri diferite. Un pas trebuie să fie succesorul celui dinainte.
  2. `_cart_followup_products` — poarta deterministă: cine decide că se propune o secvență, și ce
     se întâmplă în fiecare ramură de eșec (flag stins, pack absent, lanț scurt, pași fără stoc).
"""

from __future__ import annotations

import pytest

from src.agent import planner as planner_mod
from src.agent.planner import _cart_followup_products
from src.catalog.relation_chain import walk_chain
from src.config import get_settings
from src.domain.pack import DomainPack
from src.domain.relation_kinds import load_relation_kinds
from src.models import BusinessConfig, Contact, InboundMessage, TurnContext
from src.worker.runner import PipelineDeps


@pytest.fixture
def traversal_on(monkeypatch):
    monkeypatch.setattr(get_settings(), "relation_traversal_enabled", True)


@pytest.fixture
def traversal_off(monkeypatch):
    monkeypatch.setattr(get_settings(), "relation_traversal_enabled", False)


def _hop(id_, parent, depth, position=0):
    return {"id": id_, "parent": parent, "depth": depth, "position": position}


# --- 1. walk_chain: drum, nu frontieră ----------------------------------------------------------


def test_walks_the_successor_of_the_previous_step_not_the_best_of_each_depth():
    """Miezul. `b2` e la adâncimea 2 și ar câștiga un „cel mai bun per adâncime", dar e succesorul
    lui `x`, nu al lui `a` (pasul ales). Lanțul corect îl ignoră și merge pe `a → a2`."""
    hops = [
        _hop("a", "anchor", 1, 0),
        _hop("b2", "x", 2, 0),  # ramură străină, poziție mai bună
        _hop("a2", "a", 2, 5),
        _hop("a3", "a2", 3, 0),
    ]
    assert [h["id"] for h in walk_chain(hops, "anchor", 4)] == ["a", "a2", "a3"]


def test_chain_stops_where_the_data_stops():
    hops = [_hop("a", "anchor", 1), _hop("b", "a", 2)]
    assert [h["id"] for h in walk_chain(hops, "anchor", 6)] == ["a", "b"]


def test_chain_respects_max_steps():
    hops = [_hop("a", "anchor", 1), _hop("b", "a", 2), _hop("c", "b", 3)]
    assert [h["id"] for h in walk_chain(hops, "anchor", 2)] == ["a", "b"]


def test_anchor_without_successor_yields_nothing():
    assert walk_chain([_hop("x", "someone_else", 1)], "anchor", 4) == []


@pytest.mark.parametrize("steps", [0, -1])
def test_non_positive_max_steps_yields_nothing(steps):
    assert walk_chain([_hop("a", "anchor", 1)], "anchor", steps) == []


def test_chain_never_revisits_a_product():
    """A doua plasă după clauza `CYCLE`: „pune cremă, apoi tonic, apoi cremă" nu e un sfat."""
    hops = [_hop("a", "anchor", 1), _hop("anchor", "a", 2), _hop("a", "anchor2", 2)]
    assert [h["id"] for h in walk_chain(hops, "anchor", 5)] == ["a"]


def test_walk_is_pure_and_repeatable():
    hops = [_hop("a", "anchor", 1), _hop("b", "a", 2)]
    assert walk_chain(hops, "anchor", 3) == walk_chain(hops, "anchor", 3)


# --- 2. poarta deterministă ---------------------------------------------------------------------

_SEQ_PACK = DomainPack(
    vertical="beauty_salon",
    relation_kinds=load_relation_kinds(
        {
            "routine_next": {
                "mode": "chain",
                "max_depth": 4,
                "ordered": True,
                "labels": {"ro": "Pasi recomandati"},
            }
        }
    ),
)

_STEP_A = {"id": "s1", "name": "Tonic", "availability": "in_stock"}
_STEP_B = {"id": "s2", "name": "Tratament", "availability": "low_stock"}
_COMPLEMENT = {"id": "c1", "name": "Complement", "availability": "in_stock"}


def _ctx(pack=None):
    ctx = TurnContext(
        turn_id="t",
        business=BusinessConfig(id="b", slug="d", name="D", domain_pack=pack),
        contact=Contact(id="c", business_id="b"),
        message=InboundMessage(provider_msg_id="m", body="adauga", channel_kind="webchat"),
        conversation_id="conv",
    )
    ctx.language = "ro"
    return ctx


def _deps():
    # `PipelineDeps(conn=...)` primește automat un provider static care yield-uiește fake-ul
    # (puntea TEST-ONLY documentată în `runner.py`). Conexiunea nu contează: tot ce o folosește
    # e monkeypatch-uit.
    return PipelineDeps(conn=object(), redis=None, llm=object())


def _patch(monkeypatch, *, hops, products, complementary=(_COMPLEMENT,)):
    async def _traverse(conn, business_id, *, anchor_id, kind, max_depth):
        return list(hops)

    async def _by_ids(conn, business_id, ids, *, limit=6, respect_content_status=False):
        by_id = {p["id"]: p for p in products}
        return [by_id[i] for i in ids if i in by_id]

    async def _complementary(conn, business_id, anchor_id, *, exclude_ids=None, limit=4):
        return list(complementary)

    monkeypatch.setattr(planner_mod, "traverse_relation_chain", _traverse)
    monkeypatch.setattr(planner_mod, "get_products_by_ids", _by_ids)
    monkeypatch.setattr(planner_mod, "get_complementary_products", _complementary)


async def test_flag_off_keeps_todays_behaviour(monkeypatch, traversal_off):
    """Flagul stins ⇒ complementarele de azi, eticheta `None`. Byte-identic."""
    _patch(
        monkeypatch, hops=[_hop("s1", "p0", 1), _hop("s2", "s1", 2)], products=[_STEP_A, _STEP_B]
    )
    products, label = await _cart_followup_products(_ctx(_SEQ_PACK), _deps(), "p0", [])
    assert [p["id"] for p in products] == ["c1"]
    assert label is None


async def test_flag_on_returns_the_steps_in_order(monkeypatch, traversal_on):
    _patch(
        monkeypatch, hops=[_hop("s1", "p0", 1), _hop("s2", "s1", 2)], products=[_STEP_A, _STEP_B]
    )
    ctx = _ctx(_SEQ_PACK)
    products, label = await _cart_followup_products(ctx, _deps(), "p0", [])
    assert [p["id"] for p in products] == ["s1", "s2"]
    assert label == "Pasi recomandati"
    assert any(e.type == "relation_chain" for e in ctx.events)


async def test_pack_without_sequence_falls_back(monkeypatch, traversal_on):
    """Un tenant care nu declară niciun tip `ordered` nu primește secvențe, oricât ar avea date."""
    _patch(
        monkeypatch, hops=[_hop("s1", "p0", 1), _hop("s2", "s1", 2)], products=[_STEP_A, _STEP_B]
    )
    pack = DomainPack(
        vertical="ecommerce",
        relation_kinds=load_relation_kinds({"complement": {"mode": "neighbors"}}),
    )
    _, label = await _cart_followup_products(_ctx(pack), _deps(), "p0", [])
    assert label is None


async def test_single_step_is_not_a_sequence(monkeypatch, traversal_on):
    """Un „lanț" de un pas e un vecin direct cu alt nume: nu-l anunțăm ca «pașii următori»."""
    _patch(monkeypatch, hops=[_hop("s1", "p0", 1)], products=[_STEP_A])
    _, label = await _cart_followup_products(_ctx(_SEQ_PACK), _deps(), "p0", [])
    assert label is None


async def test_steps_already_in_cart_are_excluded_and_can_collapse_the_sequence(
    monkeypatch, traversal_on
):
    _patch(
        monkeypatch, hops=[_hop("s1", "p0", 1), _hop("s2", "s1", 2)], products=[_STEP_A, _STEP_B]
    )
    _, label = await _cart_followup_products(_ctx(_SEQ_PACK), _deps(), "p0", ["s1"])
    assert label is None  # rămâne un singur pas ⇒ nu mai e secvență


async def test_unbuyable_steps_drop_out_and_can_collapse_the_sequence(monkeypatch, traversal_on):
    """Structura rămâne întreagă în traversare; prezentarea filtrează. Dacă după filtrare rămâne
    prea puțin, cădem pe complemente în loc să anunțăm o rutină ciuntită."""
    out_of_stock = {**_STEP_B, "availability": "out_of_stock"}
    _patch(
        monkeypatch,
        hops=[_hop("s1", "p0", 1), _hop("s2", "s1", 2)],
        products=[_STEP_A, out_of_stock],
    )
    _, label = await _cart_followup_products(_ctx(_SEQ_PACK), _deps(), "p0", [])
    assert label is None
