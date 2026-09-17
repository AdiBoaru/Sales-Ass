"""NX-297 felia 2 — `clarify_options`: opțiunile pe care magazinul le poate onora.

Ce se verifică aici e contractul uneltei, nu meniul (ăla are testele lui, NX-295): ce ajunge la
model, ce NU ajunge, și cele trei stări în care poate fi catalogul (are opțiuni / nu are ce cere
clientul / nu știm nimic).
"""

import re

from src.catalog.clarify_menu import CATEGORY_DIMENSION, ClarifyMenu, MenuOption
from src.config import get_settings
from src.models import BusinessConfig, Contact, InboundMessage, TurnContext
from src.tools import clarify_tools
from src.tools.base import enabled_tools
from src.tools.clarify_tools import clarify_options_tool


def _ctx(body: str = "vreau ceva de par") -> TurnContext:
    return TurnContext(
        turn_id="t1",
        business=BusinessConfig(id="b1", slug="demo", name="Demo"),
        contact=Contact(id="c1", business_id="b1"),
        message=InboundMessage(provider_msg_id="m1", body=body),
        conversation_id="conv1",
    )


def _menu(**kw) -> ClarifyMenu:
    options = kw.pop(
        "options",
        (
            MenuOption(phrase="Par", dimension=CATEGORY_DIMENSION, key="par", count=233),
            MenuOption(phrase="Ten", dimension=CATEGORY_DIMENSION, key="ten", count=1461),
            MenuOption(phrase="par gras", dimension="concerns", key="oily_hair", count=42),
        ),
    )
    return ClarifyMenu(options=options, **kw)


def _stub(monkeypatch, menu: ClarifyMenu) -> None:
    async def _fake(ctx, deps):
        return menu

    monkeypatch.setattr(clarify_tools, "menu_for_turn", _fake)


async def test_offers_the_real_shelves_and_facets(monkeypatch):
    _stub(monkeypatch, _menu(reason="topic"))
    ctx = _ctx()
    res = await clarify_options_tool(ctx, None, {})
    assert res.ok
    assert "Par" in res.llm_view and "Ten" in res.llm_view and "par gras" in res.llm_view
    assert "O SINGURĂ întrebare" in res.llm_view
    assert any(e.type == "clarify_options" for e in ctx.events)


async def test_never_hands_the_model_a_number(monkeypatch):
    """Meniul ORDONEAZĂ după numărul de produse, dar cifra nu pleacă spre model: o cifră ajunsă în
    prompt e o cifră pe care o poate rosti, iar validatorul stagiului 8 verifică prețuri și
    produse, nu inventarul pe raft."""
    _stub(monkeypatch, _menu(reason="topic"))
    res = await clarify_options_tool(_ctx(), None, {})
    assert not re.search(r"\d", res.llm_view)


async def test_catalog_miss_refuses_instead_of_asking(monkeypatch):
    """«vreau un cablu usb» într-un magazin de cosmetice: răspunsul onest e „nu vindem asta", nu o
    întrebare despre cablul pe care nu-l avem. Defectul măsurat la NX-295."""
    _stub(monkeypatch, _menu(catalog_miss=True, reason="shelves"))
    ctx = _ctx("vreau un cablu usb")
    res = await clarify_options_tool(ctx, None, {})
    assert res.ok
    assert "nu vindem" in res.llm_view
    assert "NU cere detalii despre" in res.llm_view
    assert "O SINGURĂ întrebare" not in res.llm_view  # regulile de OFERTĂ nu se aplică pe refuz
    miss = [e for e in ctx.events if e.type == "clarify_options"]
    assert miss and miss[0].properties["catalog_miss"] is True


async def test_menu_unavailable_fails_open(monkeypatch):
    """DB jos / vocabular gol ⇒ agentul întreabă natural, fără să numească tipuri de produse.
    O clipeală de DB nu are voie să devină „botul nu mai știe să întrebe" (fail-OPEN, ca NX-295)."""
    _stub(monkeypatch, ClarifyMenu(reason="vocabulary_unavailable"))
    res = await clarify_options_tool(_ctx(), None, {})
    assert res.ok is False and res.error == "no_options"
    assert "FĂRĂ să" in res.llm_view


async def test_tool_is_not_offered_with_the_flag_off(monkeypatch):
    """Kill-switch REAL: unealta nu apare în schema trimisă modelului, deci nu costă tokeni și nu
    poate fi chemată. O unealtă care s-ar auto-dezactiva din interior ar rămâne în schemă."""
    monkeypatch.setattr(get_settings(), "clarify_tool_enabled", False)
    assert "clarify_options" not in enabled_tools(None)
    monkeypatch.setattr(get_settings(), "clarify_tool_enabled", True)
    assert "clarify_options" in enabled_tools(None)


def test_tool_is_classified_for_the_budget():
    """Poarta NX-241: un tool fără clasificare ar rula ca „read paralel-safe" din inerție."""
    from src.agent.tool_budget import assert_registry_complete, spec_for

    assert_registry_complete()
    assert spec_for("clarify_options").kind.value == "read"
