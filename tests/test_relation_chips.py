"""NX-316 felia 3 — chips din graful de relații (`routine_next`, `similar_to`) + vocea clientului.

Turul real «vreau o crema de hidratare» → «cum se folosește prima»: iZi a oferit pași de rutină
(„un gel de curățare potrivit", „o cremă cu SPF care merge cu ea"), Nativx raftul „Machiaj".
`product_relations` are 37.082 de muchii pe SOLE și nimic din chips nu le folosea.

Pure (construcție, coborârea lui `pivot_shelf`, ordinea rolurilor, recunoaștere, șabloanele v2) +
handlerele și fail-open-ul cu stub-uri de DB. ZERO OpenAI, zero DB (proba pe DB real e în
`test_relation_chips_db.py`)."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from src.agent import deterministic, planner
from src.agent import finalize as finalize_mod
from src.agent.deterministic import _COMPARE_RE, _DETAIL_RE, _LINK_RE, _REVIEW_RE, _norm_followup
from src.config import get_settings
from src.conversation import chip_moves
from src.conversation.chip_press import product_ids, recognize
from src.conversation.subject import SUBJECT_KEY, subject_is_new
from src.domain.loader import load_domain_pack
from src.models import BusinessConfig, Contact, InboundMessage, ProductRef, TurnContext
from src.worker.runner import PipelineDeps

_BIZ = BusinessConfig(id="b", slug="d", name="D")
PACK = load_domain_pack(_BIZ)

CARDS = [
    {"product_id": "p1", "name": "SOME BY MI Yuja Niacin Cream", "price": 110.0,
     "attributes": {"product_type": "crema"}},
    {"product_id": "p2", "name": "BELIF Aqua Bomb Cream", "price": 159.0,
     "attributes": {"product_type": "crema"}},
]  # fmt: skip

ROWS = [
    {"anchor_id": "p1", "kind": "complement", "product_type": "protectie_solara", "n": 6},
    {"anchor_id": "p1", "kind": "routine_next", "product_type": "gel_de_curatare", "n": 4},
    {"anchor_id": "p1", "kind": "routine_next", "product_type": "crema", "n": 9},  # propriul tip
    {"anchor_id": "p1", "kind": "substitute", "product_type": "crema", "n": 2},
    {"anchor_id": "p1", "kind": "substitute", "product_type": None, "n": 1},
    {"anchor_id": "p2", "kind": "complement", "product_type": "ser", "n": 3},
]
PHRASES = {"gel_de_curatare": "gel de curatare", "protectie_solara": "protectie solara"}


def _moves(rows=ROWS, cards=CARDS, phrases=PHRASES):
    return chip_moves.renderable(
        chip_moves.from_relations(rows, cards, phrases, locale="ro"), PACK, "ro"
    )


def _mv(kind, move_id):
    return chip_moves.ChipMove(
        kind=kind, move_id=move_id, anchor="x", slots=(("slot", "x"),), evidence=3
    )


# --- construcția, pură -----------------------------------------------------------------------


def test_routine_next_prefers_the_routine_edge_and_skips_the_own_type():
    moves = {m.move_id: m for m in _moves()}
    # `routine_next` bate `complement` (6 produse) și propriul tip (9), deși are doar 4.
    assert "routine_next:p1:gel_de_curatare" in moves
    assert moves["routine_next:p1:gel_de_curatare"].evidence == 4
    assert not [m for m in moves if m.startswith("routine_next:p2")]  # doar produsul discutat


def test_routine_next_text_names_the_type_and_the_product():
    move = next(m for m in _moves() if m.kind == "routine_next")
    text = chip_moves.render_move(move, PACK, "ro")
    assert text.startswith("Arata-mi gel de curatare care merge cu SOME BY MI")


def test_similar_to_counts_substitutes_even_without_a_type():
    moves = {m.move_id: m for m in _moves()}
    assert moves["similar_to:p1"].evidence == 3
    assert "similar_to:p2" not in moves  # p2 n-are niciun substitut servabil


def test_type_without_phrase_falls_to_the_next_type():
    moves = _moves(phrases={"protectie_solara": "protectie solara"})
    assert "routine_next:p1:protectie_solara" in {m.move_id for m in moves}


def test_no_relations_no_graph_moves():
    assert _moves(rows=[]) == []


def test_routine_types_wanted_are_only_the_discussed_product():
    assert chip_moves.relation_types_wanted(ROWS, CARDS) == ("gel_de_curatare", "protectie_solara")


# --- pivot_shelf coboară + ordinea rolurilor --------------------------------------------------


def test_pivot_shelf_drops_on_explain_when_the_graph_has_a_lateral():
    moves = [_mv("pivot_shelf", "pivot_shelf:category:machiaj"), *_moves()]
    kept, dropped = chip_moves.drop_dead(moves, obligation_kinds=["explain"])
    assert "pivot_shelf" not in {m.kind for m in kept} and dropped == 1


def test_pivot_shelf_stays_on_explain_without_a_graph_lateral():
    moves = [_mv("pivot_shelf", "pivot_shelf:category:machiaj")]
    kept, _ = chip_moves.drop_dead(moves, obligation_kinds=["explain"])
    assert [m.kind for m in kept] == ["pivot_shelf"]


def test_pivot_shelf_on_recommend_only_on_the_first_turn_of_the_subject():
    moves = [_mv("pivot_shelf", "pivot_shelf:category:machiaj")]
    first, _ = chip_moves.drop_dead(moves, obligation_kinds=["recommend"], first_subject_turn=True)
    later, _ = chip_moves.drop_dead(moves, obligation_kinds=["recommend"], first_subject_turn=False)
    assert len(first) == 1 and later == []


def test_without_obligations_pivot_shelf_rule_is_the_old_one():
    moves = [_mv("pivot_shelf", "pivot_shelf:category:machiaj"), *_moves()]
    kept, dropped = chip_moves.drop_dead(moves, first_subject_turn=False)
    assert "pivot_shelf" in {m.kind for m in kept} and dropped == 0


def test_explain_orders_lateral_before_commit_only_with_the_graph():
    assert chip_moves.roles_for(["explain"]) == ("deepen", "commit", "lateral")
    assert chip_moves.roles_for(["explain"], graph_lateral=True) == ("deepen", "lateral", "commit")


def test_graph_lateral_is_picked_before_a_shelf():
    shelf = chip_moves.ChipMove(
        kind="pivot_shelf",
        move_id="pivot_shelf:category:ten",
        anchor="Ten",
        slots=(("slot", "Ten"),),
        evidence=900,
    )
    picked = chip_moves.select(
        [shelf, *_moves()], slots=5, role_order=chip_moves.roles_for(["recommend"])
    )
    lateral = [m.kind for m in picked if m.role == "lateral"]
    assert lateral[0] in chip_moves.GRAPH_LATERAL


def test_subject_is_new():
    class S:
        search_constraints = None

    state = S()
    assert subject_is_new(state, "t2") is True
    state.search_constraints = {SUBJECT_KEY: {"type": "crema", "turn": "t1"}}
    assert subject_is_new(state, "t1") is True
    assert subject_is_new(state, "t2") is False


# --- recunoașterea ---------------------------------------------------------------------------


_SHOWN = [{k: c[k] for k in ("product_id", "name", "price")} for c in CARDS]


def test_routine_next_press_is_recognized_from_move_id_and_phrase():
    move = next(m for m in _moves() if m.kind == "routine_next")
    text = chip_moves.render_move(move, PACK, "ro")
    got = recognize(
        text,
        _SHOWN,
        [move.move_id],
        PACK,
        "ro",
        phrases={("product_type", "gel_de_curatare"): "gel de curatare"},
    )
    assert got is not None and got.move_id == move.move_id and product_ids(got) == ("p1",)
    assert chip_moves.wanted_phrases([move.move_id]) == {"product_type": ("gel_de_curatare",)}


def test_similar_to_press_needs_no_phrase():
    move = next(m for m in _moves() if m.kind == "similar_to")
    text = chip_moves.render_move(move, PACK, "ro")
    got = recognize(text, _SHOWN, ["similar_to:p1"], PACK, "ro")
    assert got is not None and product_ids(got) == ("p1",)


# --- șabloanele v2 (vocea clientului) ------------------------------------------------------------

_ROUTING = {
    "detail": _DETAIL_RE,
    "reviews": _REVIEW_RE,
    "link": _LINK_RE,
    "compare": _COMPARE_RE,
}
_PACKS = sorted(Path("src/domain/defaults").glob("*.json"))


@pytest.mark.parametrize("path", _PACKS, ids=lambda p: p.stem)
def test_every_v2_template_of_an_existing_kind_still_routes_by_its_regex(path):
    """Dacă recunoașterea apăsării cade, textul ajunge la regexurile de azi: un chip reformulat
    nu are voie să devină un mesaj pe care nimeni nu-l înțelege."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    v2 = raw.get("chip_templates_v2") or {}
    assert v2, path.stem
    for kind, pattern in _ROUTING.items():
        for locale, template in (v2.get(kind) or {}).items():
            text = template.format(slot="Produs Test", slot_b="Alt Produs")
            assert pattern.search(_norm_followup(text)), (path.stem, kind, locale)


def test_v2_templates_are_read_only_with_the_flag(monkeypatch):
    move = chip_moves.ChipMove(
        kind="reviews", move_id="reviews:p1", anchor="X Y", slots=(("slot", "X Y"),), evidence=1
    )
    monkeypatch.setattr(get_settings(), "chip_moves_v2_enabled", False, raising=False)
    assert chip_moves.render_move(move, PACK, "ro") == "Ce spun recenziile despre X Y"
    monkeypatch.setattr(get_settings(), "chip_moves_v2_enabled", True, raising=False)
    assert chip_moves.render_move(move, PACK, "ro") == "Ce parere au clientii despre X Y?"


def test_v2_falls_back_per_kind(monkeypatch):
    monkeypatch.setattr(get_settings(), "chip_moves_v2_enabled", True, raising=False)
    pack = replace(PACK, chip_templates_v2={"link": {"ro": "Trimite-mi linkul la {slot}!"}})
    move = chip_moves.ChipMove(
        kind="detail", move_id="detail:p1", anchor="X Y", slots=(("slot", "X Y"),), evidence=1
    )
    assert chip_moves.render_move(move, pack, "ro") == "Spune-mi mai multe despre X Y"


# --- emiterea și handlerele ------------------------------------------------------------------


def _ctx(body: str = "x") -> TurnContext:
    ctx = TurnContext(
        turn_id="t",
        business=replace(_BIZ, domain_pack=PACK),
        contact=Contact(id="c", business_id="b"),
        message=InboundMessage(provider_msg_id="m", body=body),
        conversation_id="conv",
    )
    ctx.state.displayed_products = [
        ProductRef(c["product_id"], c["name"], c["price"]) for c in CARDS
    ]
    return ctx


def _deps():
    return PipelineDeps(conn=object(), redis=None, llm=None)


class _Pass:
    def gate(self, ctx, products, purpose):
        return (products,)


async def test_relation_moves_are_inert_with_the_flag_off(monkeypatch):
    monkeypatch.setattr(get_settings(), "chip_moves_v2_enabled", False, raising=False)

    async def boom(*a, **k):
        raise AssertionError("flag stins ⇒ niciun query")

    monkeypatch.setattr("src.db.queries.catalog.relation_type_counts", boom)
    assert await finalize_mod._relation_moves(_ctx(), _deps(), CARDS) == ([], {})


async def test_relation_query_failure_is_fail_open_and_counted(monkeypatch):
    monkeypatch.setattr(get_settings(), "chip_moves_v2_enabled", True, raising=False)

    async def down(*a, **k):
        raise RuntimeError("db jos")

    monkeypatch.setattr("src.db.queries.catalog.relation_type_counts", down)
    assert await finalize_mod._relation_moves(_ctx(), _deps(), CARDS) == (
        [],
        {"relations_error": 1},
    )


async def test_relation_moves_one_query_on_all_anchors(monkeypatch):
    monkeypatch.setattr(get_settings(), "chip_moves_v2_enabled", True, raising=False)
    calls: list = []

    async def counts(conn, business_id, anchor_ids, kinds=()):
        calls.append((business_id, list(anchor_ids)))
        return ROWS

    async def vocab(*a, **k):
        return object()

    def phrases(vocab, pack, dimension, keys, *, locale):
        assert dimension == "product_type"
        return {k: PHRASES[k] for k in keys if k in PHRASES}

    monkeypatch.setattr("src.db.queries.catalog.relation_type_counts", counts)
    monkeypatch.setattr("src.catalog.vocabulary_cache.get_vocabulary", vocab)
    monkeypatch.setattr("src.catalog.clarify_menu.value_phrases", phrases)

    moves, stats = await finalize_mod._relation_moves(_ctx(), _deps(), CARDS)

    assert calls == [("b", ["p1", "p2"])] and stats == {}
    assert {m.kind for m in moves} == {"routine_next", "similar_to"}


@pytest.mark.parametrize(
    ("move_id", "kinds", "ptype"),
    [
        ("routine_next:p1:gel_de_curatare", ("routine_next", "complement"), "gel_de_curatare"),
        ("similar_to:p1", ("substitute",), None),
    ],
)
async def test_graph_press_serves_the_related_set(monkeypatch, move_id, kinds, ptype):
    calls: list = []

    async def related(conn, business_id, anchor_id, rel_kinds, *, product_type=None, limit=6):
        calls.append((business_id, anchor_id, rel_kinds, product_type))
        return [{"id": "r1", "name": "Gel", "price": 40.0}]

    monkeypatch.setattr(planner, "related_in_stock", related)
    ctx = _ctx()
    ctx.chip_move = next(m for m in _moves() if m.move_id == move_id)

    got = await planner.resolve_chip_set(ctx, _deps(), policy=_Pass())

    assert [p["id"] for p in got] == ["r1"]
    assert calls == [("b", "p1", kinds, ptype)]


async def test_graph_press_is_counted_and_left_to_the_turn():
    ctx = _ctx()
    move = next(m for m in _moves() if m.kind == "similar_to")
    assert await deterministic.serve_chip_move(ctx, _deps(), move) is False
    pressed = next(e.properties for e in ctx.events if e.type == "chip_pressed")
    assert pressed["handler"] == "similar_to"


async def test_no_chip_no_relation_read(monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("fără chip nu se citește nimic")

    monkeypatch.setattr(planner, "related_in_stock", boom)
    assert await planner.resolve_chip_set(_ctx(), _deps(), policy=_Pass()) == []
