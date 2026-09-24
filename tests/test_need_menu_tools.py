"""NX-322 — nevoile din meniu pe uneltele REALE: `hard` filtrează, `soft` doar ordonează.

Aceeași conversație (`bc7a356e`). DB stubuită, zero model: se verifică ce ajunge la SQL
(`facet_filters`) și la fuziune (`prefer`), nu un rezultat de ranking.
"""

from __future__ import annotations

import pytest

from src.config import get_settings
from src.models import BusinessConfig, Contact, InboundMessage, Message, TurnContext
from src.tools import catalog_tools as ct
from src.tools import routine_tools as rt
from src.worker.runner import PipelineDeps
from src.worker.stages import agent as agent_stage
from tests.test_need_menu import PACK, VOCAB

TEXTS = ("pai mi se usuca pielea dupa dus", "vreau o crema de fata")


def _ctx(body: str, *earlier: str) -> TurnContext:
    business = BusinessConfig(id="b", slug="d", name="D")
    business.domain_pack = PACK
    ctx = TurnContext(
        turn_id="t",
        business=business,
        contact=Contact(id="c", business_id="b"),
        message=InboundMessage(provider_msg_id="m", body=body),
        conversation_id="conv",
        language="ro",
    )
    ctx.history = [Message(direction="inbound", author="contact", body=t) for t in earlier]
    return ctx


def _deps() -> PipelineDeps:
    return PipelineDeps(conn=object(), redis=None, llm=None)


@pytest.fixture
def seen(monkeypatch):
    captured: dict = {}

    async def fake_lexical(conn, business_id, *, facet_filters, **kwargs):
        captured["facet_filters"] = facet_filters
        return [
            {"id": "p1", "name": "Crema", "price": 50.0, "attributes": {"skin_type": "dry"}},
        ]

    def fake_fuse(lex, vec, **kw):
        captured["prefer"] = kw.get("prefer")
        return list(lex)

    async def no_embeddings(conn, business_id):
        return False

    async def fake_vocab(deps, business_id):
        return VOCAB

    monkeypatch.setattr(ct, "search_products_lexical", fake_lexical)
    monkeypatch.setattr(ct, "has_embeddings", no_embeddings)
    monkeypatch.setattr(ct, "fuse_candidates", fake_fuse)
    monkeypatch.setattr(ct, "get_vocabulary", fake_vocab)
    monkeypatch.setattr(rt, "get_vocabulary", fake_vocab)
    return captured


def _events(ctx, kind):
    return [e.properties for e in ctx.events if e.type == kind]


# ── search_products ───────────────────────────────────────────────────────────────────────────


async def test_cautarea_conversatiei_reale_ordoneaza_dupa_dry_fara_filtru(seen):
    ctx = _ctx(*TEXTS)
    await ct.search_products_tool(
        ctx,
        _deps(),
        {
            "query": "crema de fata pentru piele care se usuca",
            "concerns": [
                {"key": "dry", "quote": "mi se usuca pielea"},
                {"key": "redness", "quote": "calmarea roseții"},  # a scris-o botul
            ],
        },
    )
    assert "skin_type" not in (seen["facet_filters"] or {})
    assert "concerns" not in (seen["facet_filters"] or {})
    assert seen["prefer"] == {"skin_type": ["dry"]}
    verdicts = {e["key"]: (e["strength"], e["reason"]) for e in _events(ctx, "need_resolved")}
    assert verdicts == {
        "dry": ("soft", "semantic_only"),
        "redness": ("rejected", "quote_missing"),
    }
    assert all(e["consumer"] == "search_products" for e in _events(ctx, "need_resolved"))


async def test_alias_in_citat_devine_filtru(seen):
    ctx = _ctx("am ten uscat, vreau o crema")
    await ct.search_products_tool(
        ctx, _deps(), {"query": "crema", "concerns": [{"key": "dry", "quote": "am ten uscat"}]}
    )
    assert seen["facet_filters"]["skin_type"] == ["dry"]
    assert seen["prefer"] is None


async def test_contradictia_nu_filtreaza_si_nu_ordoneaza(seen):
    ctx = _ctx("am ten gras")
    await ct.search_products_tool(
        ctx, _deps(), {"query": "crema", "concerns": [{"key": "dry", "quote": "am ten gras"}]}
    )
    assert "skin_type" not in (seen["facet_filters"] or {})
    assert seen["prefer"] is None


async def test_evenimentul_nu_poarta_citatul(seen):
    ctx = _ctx("sunt insarcinata si mi se usuca pielea")
    await ct.search_products_tool(
        ctx,
        _deps(),
        {
            "query": "crema",
            "concerns": [{"key": "dry", "quote": "sunt insarcinata si mi se usuca"}],
        },
    )
    for ev in ctx.events:
        assert "insarcinata" not in str(ev.properties)


# ── routine_plan ──────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def routine(monkeypatch):
    from src.domain.routine_steps import build_spec

    captured: dict = {}

    async def fake_candidates(conn, business_id, *, values, facet_filters=None, prefer=None, **kw):
        captured["facet_filters"] = facet_filters
        captured["prefer"] = prefer
        return [{"id": f"p{i}", "step": v, "price": 30} for i, v in enumerate(values)]

    async def fake_hydrate(conn, business_id, ids, **kw):
        return [{"id": pid, "name": f"Produs {pid}", "price": 30} for pid in ids]

    async def fake_vocab(deps, business_id):
        return VOCAB

    monkeypatch.setattr(rt, "routine_candidates", fake_candidates)
    monkeypatch.setattr(rt, "get_products_by_ids", fake_hydrate)
    monkeypatch.setattr(rt, "get_vocabulary", fake_vocab)
    monkeypatch.setattr(rt, "_safety_gate", lambda ctx, products, purpose: (products, ""))
    spec = build_spec(
        {
            "families": {"fata": ["curatare", "hidratare"]},
            "by_product_type": {"gel de curatare": "fata:curatare"},
        }
    )
    return captured, spec


async def test_rutina_conversatiei_reale_ordoneaza_dupa_dry(routine):
    from dataclasses import replace

    captured, spec = routine
    ctx = _ctx("fa mi o rutina", *TEXTS)
    ctx.business.domain_pack = replace(PACK, routine_steps=spec)

    await rt.routine_plan_tool(
        ctx,
        _deps(),
        {
            "family": "fata",
            "concerns": [
                {"key": "dry", "quote": "mi se usuca pielea"},
                {"key": "redness", "quote": "calmarea roseții"},
            ],
            "budget_max": None,
            "anchor_id": None,
        },
    )

    assert not (captured["facet_filters"] or {})
    assert captured["prefer"] == {"skin_type": ["dry"]}
    assert {e["consumer"] for e in _events(ctx, "need_resolved")} == {"routine_plan"}


# ── agent_stage: meniul se construiește doar cu flagul ────────────────────────────────────────


async def test_meniul_e_stins_implicit(monkeypatch):
    monkeypatch.setattr(get_settings(), "need_menu_enabled", False)
    assert await agent_stage.need_menu_for_turn(_ctx("x"), _deps()) is None


async def test_meniul_aprins_are_optiuni(monkeypatch):
    monkeypatch.setattr(get_settings(), "need_menu_enabled", True)

    async def fake_vocab(deps, business_id):
        return VOCAB

    monkeypatch.setattr(agent_stage, "get_vocabulary", fake_vocab)
    menu = await agent_stage.need_menu_for_turn(_ctx("x"), _deps())
    assert menu is not None and "dry" in menu.keys()


async def test_vocabular_cazut_da_schema_de_azi(monkeypatch):
    monkeypatch.setattr(get_settings(), "need_menu_enabled", True)

    async def boom(deps, business_id):
        raise RuntimeError("db down")

    monkeypatch.setattr(agent_stage, "get_vocabulary", boom)
    assert await agent_stage.need_menu_for_turn(_ctx("x"), _deps()) is None
