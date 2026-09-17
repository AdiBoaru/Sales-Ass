"""NX-297 felia 5 — chips-urile v1 devin MUTĂRI cu dovadă, nu fraze scrise liber.

Pe v1 textul chip-ului ESTE comanda: apăsarea îl retrimite ca mesaj nou al clientului. Testele de
aici verifică cine îl produce și ce se întâmplă când producția eșuează.
"""

from types import SimpleNamespace

from src.agent import finalize
from src.catalog.clarify_menu import CATEGORY_DIMENSION, ClarifyMenu, MenuOption
from src.config import get_settings
from src.domain.loader import load_domain_pack
from src.models import BusinessConfig, Contact, InboundMessage, RichItem, RichReply, TurnContext


def _pack():
    return load_domain_pack(SimpleNamespace(vertical="ecommerce", settings={}))


def _ctx(body: str = "vreau ceva de par") -> TurnContext:
    return TurnContext(
        turn_id="t1",
        business=BusinessConfig(id="b1", slug="demo", name="Demo", domain_pack=_pack()),
        contact=Contact(id="c1", business_id="b1"),
        message=InboundMessage(provider_msg_id="m1", body=body),
        conversation_id="conv1",
        language="ro",
    )


def _rich() -> RichReply:
    return RichReply(
        intro="Uite ce am găsit.",
        items=[
            RichItem(product_id="p1", name="Sampon Delicat Zilnic", price=49.0, reason="blând"),
            RichItem(product_id="p2", name="Balsam Hidratant Intens", price=59.0, reason="hidr."),
        ],
        pick=None,
        education=None,
        chips=[],
        disclaimer="",
    )


def _stub_menu(monkeypatch, menu: ClarifyMenu) -> None:
    async def _fake(ctx, deps):
        return menu

    monkeypatch.setattr("src.catalog.clarify_menu.menu_for_turn", _fake)


def _menu() -> ClarifyMenu:
    return ClarifyMenu(
        options=(
            MenuOption(phrase="Par", dimension=CATEGORY_DIMENSION, key="par", count=233),
            MenuOption(phrase="Ten", dimension=CATEGORY_DIMENSION, key="ten", count=1461),
        ),
        reason="shelves",
    )


async def test_flag_off_leaves_the_model_chips_alone(monkeypatch):
    monkeypatch.setattr(get_settings(), "chip_moves_v1_enabled", False)
    rich = _rich()
    rich.chips = ["orice a scris modelul"]
    await finalize._apply_move_chips(_ctx(), None, rich)
    assert rich.chips == ["orice a scris modelul"]


async def test_chips_come_from_the_menu_and_the_cards(monkeypatch):
    monkeypatch.setattr(get_settings(), "chip_moves_v1_enabled", True)
    _stub_menu(monkeypatch, _menu())
    ctx, rich = _ctx(), _rich()
    await finalize._apply_move_chips(ctx, None, rich)

    labels = [c.label for c in rich.chips]
    assert labels, "turul are și raft, și carduri: trebuie să iasă continuări"
    # Fiecare chip numește ceva REAL: un raft din meniu sau un produs afișat.
    anchors = ("Par", "Ten", "Sampon Delicat", "Balsam Hidratant")
    assert all(any(a in label for a in anchors) for label in labels)
    assert any(e.type == "chip_moves" and e.properties["path"] == "v1" for e in ctx.events)


async def test_offered_moves_are_remembered_so_they_are_not_repeated(monkeypatch):
    monkeypatch.setattr(get_settings(), "chip_moves_v1_enabled", True)
    _stub_menu(monkeypatch, _menu())
    ctx, rich = _ctx(), _rich()
    await finalize._apply_move_chips(ctx, None, rich)
    # Un chip văzut și neluat nu se mai oferă la turul viitor.
    assert ctx.state_patch["offered_chips"]


async def test_a_broken_menu_keeps_the_answer(monkeypatch):
    """Chips-urile nu sunt răspunsul. Orice eșec lasă exact comportamentul de azi (P6)."""
    monkeypatch.setattr(get_settings(), "chip_moves_v1_enabled", True)

    async def _boom(ctx, deps):
        raise RuntimeError("DB jos")

    monkeypatch.setattr("src.catalog.clarify_menu.menu_for_turn", _boom)
    rich = _rich()
    rich.chips = ["ce a compus compose"]
    await finalize._apply_move_chips(_ctx(), None, rich)
    assert rich.chips == ["ce a compus compose"]


async def test_a_factual_turn_gets_depth_not_filters(monkeypatch):
    """Rolurile vin din obligațiile DETERMINISTE ale mesajului. La o întrebare punctuală, cinci
    îngustări arată ca un bot care nu te-a auzit — continuarea firească e pe produsul discutat."""
    monkeypatch.setattr(get_settings(), "chip_moves_v1_enabled", True)
    _stub_menu(monkeypatch, _menu())
    ctx, rich = _ctx("care e pretul la primul?"), _rich()
    await finalize._apply_move_chips(ctx, None, rich)

    ev = [e for e in ctx.events if e.type == "chip_moves"]
    assert ev and "forward" not in ev[0].properties["roles"]
