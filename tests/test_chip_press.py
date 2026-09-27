"""NX-316 felia 1 — serverul recunoaște apăsarea unui chip, iar chips-urile moarte nu se oferă.

Pure (recunoașterea, `drop_dead`) + cap-coadă pe `agent_stage` cu stub-uri de DB și un model care
PICĂ dacă e chemat: o apăsare recunoscută se servește determinist. ZERO OpenAI, zero DB."""

import pytest

from src.config import get_settings
from src.conversation import chip_moves
from src.conversation.chip_press import product_ids, recognize
from src.conversation.subject import SUBJECT_KEY, spoken_needs
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
PACK = load_domain_pack(_BIZ)

CARDS = [
    {"product_id": "p1", "name": "IUNIK Beta Glucan Daily Moisture Cream", "price": 89.0},
    {"product_id": "p2", "name": "BELIF Age Knockdown V Cream", "price": 159.0},
    {"product_id": "p3", "name": "Dear Klairs Midnight Blue Calming Cream", "price": 99.0},
]


def _moves():
    return chip_moves.renderable(
        chip_moves.from_cards(CARDS, unique_anchor=True, locale="ro"), PACK, "ro"
    )


def _text(move_id: str) -> str:
    move = next(m for m in _moves() if m.move_id == move_id)
    return chip_moves.render_move(move, PACK, "ro")


# --- recunoașterea, pură ---------------------------------------------------------------------


def test_offered_chip_is_recognized_with_its_product():
    text = _text("link:p2")
    move = recognize(text, CARDS, ["link:p2"], PACK, "ro", unique_anchor=True)
    assert move is not None and move.kind == "link"
    assert product_ids(move) == ("p2",)


def test_diacritics_and_punctuation_do_not_matter():
    text = _text("detail:p3")
    typed = text.replace("ă", "a").replace("ț", "t").upper() + "!"
    assert recognize(typed, CARDS, ["detail:p3"], PACK, "ro", unique_anchor=True) is not None


def test_compare_pair_comes_from_the_move_id():
    move = recognize(
        _text("compare:p1:p2"), CARDS, ["compare:p1:p2"], PACK, "ro", unique_anchor=True
    )
    assert move is not None and product_ids(move) == ("p1", "p2")


@pytest.mark.parametrize(
    ("message", "offered"),
    [
        ("link:p2", []),  # text corect, dar mutarea n-a fost OFERITĂ
        ("link:p2", ["link:p1"]),  # altă mutare oferită
        ("__free__", ["link:p2"]),  # mesaj liber
    ],
)
def test_not_recognized(message, offered):
    text = "Trimite-mi linkul, te rog" if message == "__free__" else _text(message)
    assert recognize(text, CARDS, offered, PACK, "ro", unique_anchor=True) is None


def test_a_rephrased_chip_is_a_new_message_not_a_press():
    text = _text("link:p2") + " si spune-mi pretul"
    assert recognize(text, CARDS, ["link:p2"], PACK, "ro", unique_anchor=True) is None


def test_menu_moves_are_not_pressable():
    """`refine_facet`/`pivot_shelf` sunt fraze de CĂUTARE, deja rutate corect prin vocabular."""
    assert (
        recognize(
            "Caut ceva pentru hidratare", CARDS, ["refine_facet:concerns:hydration"], PACK, "ro"
        )
        is None
    )


# --- chips moarte, pur -----------------------------------------------------------------------


def _mv(kind, move_id):
    return chip_moves.ChipMove(
        kind=kind, move_id=move_id, anchor="x", slots=(("slot", "x"),), evidence=3
    )


def test_spoken_need_is_not_offered_again():
    moves = [
        _mv("refine_facet", "refine_facet:concerns:hydration"),
        _mv("refine_facet", "refine_facet:skin_type:dry"),
    ]
    kept, dropped = chip_moves.drop_dead(moves, spoken_needs=[("concerns", "hydration")])
    assert [m.move_id for m in kept] == ["refine_facet:skin_type:dry"] and dropped == 1


def test_detail_chip_is_dead_on_a_single_card_turn():
    moves = [_mv("detail", "detail:p1"), _mv("reviews", "reviews:p1")]
    kept, dropped = chip_moves.drop_dead(moves, n_cards=1)
    assert [m.kind for m in kept] == ["reviews"] and dropped == 1
    kept, dropped = chip_moves.drop_dead(moves, n_cards=2)
    assert len(kept) == 2 and dropped == 0


def test_spoken_needs_read_from_the_subject():
    ctx = _ctx("x")
    assert spoken_needs(ctx.state) == ()
    ctx.state.search_constraints = {SUBJECT_KEY: {"needs": [["concerns", "hydration"]]}}
    assert spoken_needs(ctx.state) == (("concerns", "hydration"),)


def test_drop_dead_is_inert_only_with_both_flags_off(monkeypatch):
    """Chips-urile moarte au kill-switch PROPRIU (ON): pe turul `a623c53e` V2 era stins și
    «Caut ceva pentru hidratare» a plecat sub «vreau o crema de hidratare»."""
    from src.agent.finalize import _drop_dead_moves

    monkeypatch.setattr(get_settings(), "chip_moves_v2_enabled", False, raising=False)
    monkeypatch.setattr(get_settings(), "chip_drop_dead_enabled", False, raising=False)
    ctx = _ctx("x")
    ctx.state.search_constraints = {SUBJECT_KEY: {"needs": [["concerns", "hydration"]]}}
    moves = [_mv("refine_facet", "refine_facet:concerns:hydration")]
    assert _drop_dead_moves(ctx, moves, n_cards=1) == (moves, 0)
    monkeypatch.setattr(get_settings(), "chip_drop_dead_enabled", True, raising=False)
    assert _drop_dead_moves(ctx, moves, n_cards=1) == ([], 1)
    monkeypatch.setattr(get_settings(), "chip_drop_dead_enabled", False, raising=False)
    monkeypatch.setattr(get_settings(), "chip_moves_v2_enabled", True, raising=False)
    assert _drop_dead_moves(ctx, moves, n_cards=1) == ([], 1)


def test_dead_chips_are_on_by_default():
    from src.config import Settings

    assert Settings.model_fields["chip_drop_dead_enabled"].default is True
    assert Settings.model_fields["chip_moves_v2_enabled"].default is False


def test_pivot_shelf_lowering_stays_behind_v2(monkeypatch):
    """Doar chips-urile MOARTE ies de sub V2; coborârea raftului vecin e o decizie de produs a
    feliei 3 și rămâne pe flagul ei."""
    from src.agent.finalize import _drop_dead_moves

    monkeypatch.setattr(get_settings(), "chip_moves_v2_enabled", False, raising=False)
    monkeypatch.setattr(get_settings(), "chip_drop_dead_enabled", True, raising=False)
    ctx = _ctx("x")
    moves = [_mv("pivot_shelf", "pivot_shelf:category:machiaj")]
    kept, dropped = _drop_dead_moves(ctx, moves, n_cards=2, obligation_kinds=["recommend"])
    assert kept == moves and dropped == 0


def test_refinement_already_true_of_every_card_is_dead():
    """Turul `a623c53e`: «Caut ceva pentru zi si noapte» sub două creme care sunt AMBELE de zi și
    de noapte. Îngustarea n-ar scoate nimic de pe ecran."""
    moves = [
        _mv("refine_facet", "refine_facet:routine_time:am_pm"),
        _mv("refine_facet", "refine_facet:skin_type:dry"),
    ]
    cards = [
        {"product_id": "p1", "attributes": {"routine_time": "am_pm", "skin_type": "dry"}},
        {"product_id": "p2", "attributes": {"routine_time": "am_pm", "skin_type": "sensitive"}},
    ]
    kept, dropped = chip_moves.drop_dead(moves, cards=cards)
    assert [m.move_id for m in kept] == ["refine_facet:skin_type:dry"] and dropped == 1


@pytest.mark.parametrize(
    "cards",
    [
        # un singur card: o îngustare poate aduce produse NOI, nu e moartă
        [{"product_id": "p1", "attributes": {"routine_time": "am_pm"}}],
        # un card fără valoare = „nu știm", nu „are": UNKNOWN ≠ MISMATCH
        [
            {"product_id": "p1", "attributes": {"routine_time": "am_pm"}},
            {"product_id": "p2", "attributes": {}},
        ],
        # valori diferite: îngustarea chiar alege
        [
            {"product_id": "p1", "attributes": {"routine_time": "am_pm"}},
            {"product_id": "p2", "attributes": {"routine_time": "pm"}},
        ],
    ],
)
def test_refinement_that_still_narrows_survives(cards):
    moves = [_mv("refine_facet", "refine_facet:routine_time:am_pm")]
    kept, dropped = chip_moves.drop_dead(moves, cards=cards)
    assert kept == moves and dropped == 0


def test_list_valued_attributes_count_per_element():
    moves = [_mv("refine_facet", "refine_facet:concerns:hydration")]
    cards = [
        {"attributes": {"concerns": ["hydration", "redness"]}},
        {"attributes": {"concerns": ["Hydration", "anti_aging"]}},
    ]
    kept, dropped = chip_moves.drop_dead(moves, cards=cards)
    assert kept == [] and dropped == 1


# --- cap-coadă pe agent_stage ----------------------------------------------------------------


@pytest.fixture(autouse=True)
def _stub_prompt_inputs(monkeypatch):
    async def _cats(conn, business_id):
        return ["Creme"]

    async def _aliases(conn, business_id, **k):
        return []

    monkeypatch.setattr(agent_mod, "list_category_names", _cats)
    monkeypatch.setattr(agent_mod, "list_routing_aliases", _aliases)


class _NoModel:
    async def embed(self, texts, *, model=None):
        return [[0.0] * 8 for _ in texts]

    async def run_tool_loop(self, *a, **k):
        raise AssertionError("o apăsare recunoscută NU trece prin model")

    async def complete(self, *a, **k):
        raise AssertionError("o apăsare recunoscută NU trece prin model")


def _ctx(body: str, offered=()) -> TurnContext:
    from dataclasses import replace

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


def _by_ids(served: list):
    async def fake(conn, business_id, ids, **k):
        served.append(list(ids))
        return [
            {
                "id": c["product_id"],
                "name": c["name"],
                "price": c["price"],
                "url": f"https://shop/{c['product_id']}",
            }
            for c in CARDS
            if c["product_id"] in ids
        ]

    return fake


def _deps():
    return PipelineDeps(conn=object(), redis=None, llm=_NoModel())


async def test_link_press_sends_only_the_named_product(monkeypatch):
    monkeypatch.setattr(get_settings(), "chip_moves_v2_enabled", True, raising=False)
    served: list = []
    monkeypatch.setattr("src.agent.deterministic.get_products_by_ids", _by_ids(served))
    ctx = _ctx(_text("link:p2"), offered=["link:p2", "detail:p1"])

    await agent_stage(ctx, _deps())

    assert served == [["p2"]]
    assert ctx.reply is not None and ctx.reply.offer is not None
    assert ctx.reply.offer.url == "https://shop/p2"
    pressed = [e.properties for e in ctx.events if e.type == "chip_pressed"]
    assert pressed and pressed[0]["kind"] == "link" and pressed[0]["handler"] == "link_intent"


async def test_link_press_with_the_flag_off_is_the_old_handler(monkeypatch):
    """Byte-identic cu flagul stins: handlerul de azi servește TOT setul afișat.

    NX-326 repară aceeași frază și pe calea TASTATĂ (`named_targets`), deci „handlerul de azi"
    înseamnă aici ambele flaguri stinse."""
    monkeypatch.setattr(get_settings(), "chip_moves_v2_enabled", False, raising=False)
    monkeypatch.setattr(get_settings(), "named_shortcut_targets_enabled", False, raising=False)
    served: list = []
    monkeypatch.setattr("src.agent.deterministic.get_products_by_ids", _by_ids(served))
    ctx = _ctx(_text("link:p2"), offered=["link:p2"])

    await agent_stage(ctx, _deps())

    assert served == [["p1", "p2", "p3"]]
    assert not any(e.type == "chip_pressed" for e in ctx.events)


async def test_compare_press_compares_the_pair_in_the_move(monkeypatch):
    """Perechea vine din `move_id`, chiar dacă nu sunt primele două afișate."""
    from src.agent import deterministic

    compared: list = []

    async def fake_compare(ctx, deps, ids):
        compared.append(ids)
        ctx.set_reply("tabel")
        return True

    monkeypatch.setattr(deterministic, "serve_comparison", fake_compare)
    ctx = _ctx("x")
    move = chip_moves.ChipMove(
        kind="compare",
        move_id="compare:p3:p1",
        anchor="a",
        slots=(("slot", "a"), ("slot_b", "b")),
        evidence=2,
    )

    assert await deterministic.serve_chip_move(ctx, _deps(), move) is True
    assert compared == [["p3", "p1"]]


async def test_refused_compare_falls_through_to_the_normal_path(monkeypatch):
    from src.agent import deterministic

    async def refuse(ctx, deps, ids):
        return False

    monkeypatch.setattr(deterministic, "serve_comparison", refuse)
    ctx = _ctx("x")
    move = chip_moves.ChipMove(
        kind="compare",
        move_id="compare:p1:p2",
        anchor="a",
        slots=(("slot", "a"), ("slot_b", "b")),
        evidence=2,
    )

    assert await deterministic.serve_chip_move(ctx, _deps(), move) is False
    pressed = [e.properties for e in ctx.events if e.type == "chip_pressed"]
    assert pressed[0]["handler"] == "agent"
