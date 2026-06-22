"""F2-1 — tool `checkout_link` + extensia de validator (allowed_links). ZERO DB/OpenAI.

Query-urile de catalog/comerț sunt monkeypatch-uite; LLM-ul (la testul e2e) e scriptat.
Acoperă: link generat + scriere checkout_links, produse inexistente filtrate, fără base URL,
idempotență (ref_code=turn_id), validatorul acceptă linkul botului dar respinge unul inventat.
"""

import pytest

from src.models import BusinessConfig, Contact, InboundMessage, Route, RouteDecision, TurnContext
from src.tools import commerce_tools as cm
from src.tools.base import enabled_tools
from src.worker.runner import PipelineDeps
from src.worker.stages.agent import _links_ok, _valid, agent_stage

BASE = "https://shop.example/checkout"

CATALOG = [
    {
        "id": "p1",
        "name": "Crema Hidratantă",
        "brand": "BrandA",
        "price": 82.99,
        "url": "https://shop/p1",
    },
    {
        "id": "p2",
        "name": "Ser Calmant",
        "brand": "BrandB",
        "price": 120.50,
        "url": "https://shop/p2",
    },
]


def _ctx(*, settings=None, route=Route.SALES, body="vreau să comand") -> TurnContext:
    ctx = TurnContext(
        turn_id="t",
        business=BusinessConfig(id="b", slug="d", name="D", settings=settings or {}),
        contact=Contact(id="c", business_id="b"),
        message=InboundMessage(provider_msg_id="m", body=body),
        conversation_id="conv",
    )
    if route is not None:
        ctx.route = RouteDecision(route=route)
    return ctx


def _deps(llm=None) -> PipelineDeps:
    return PipelineDeps(conn=object(), redis=None, llm=llm)


def _patch_catalog(monkeypatch, catalog=CATALOG):
    async def fake_by_ids(conn, business_id, ids, *, limit=6):
        return [p for p in catalog if p["id"] in set(ids)]

    monkeypatch.setattr(cm, "get_products_by_ids", fake_by_ids)


def _patch_create(monkeypatch, sink):
    async def fake_create(
        conn, business_id, conversation_id, contact_id, ref_code, cart, url, expires_at
    ):
        sink.append(
            {
                "business_id": business_id,
                "ref_code": ref_code,
                "cart": cart,
                "url": url,
                "expires_at": expires_at,
            }
        )
        return {"id": "cl1", "ref_code": ref_code, "url": url}

    monkeypatch.setattr(cm, "create_checkout_link", fake_create)


# --- tool direct -------------------------------------------------------------


async def test_checkout_link_happy(monkeypatch):
    _patch_catalog(monkeypatch)
    writes: list[dict] = []
    _patch_create(monkeypatch, writes)

    ctx = _ctx(settings={"checkout_url": BASE})
    res = await cm.checkout_link_tool(
        ctx, _deps(), {"cart_items": [{"product_id": "p1", "variant_id": None, "quantity": 2}]}
    )

    assert res.ok is True
    assert res.links == [f"{BASE}?ref=t"]  # ref_code = turn_id
    assert "ref=t" in res.llm_view
    # checkout_links scris cu cart snapshot (ref-uri + preț) + total corect în event
    assert len(writes) == 1 and writes[0]["ref_code"] == "t"
    assert writes[0]["cart"] == [
        {
            "product_id": "p1",
            "variant_id": None,
            "name": "Crema Hidratantă",
            "price": 82.99,
            "quantity": 2,
        }
    ]
    ev = [e for e in ctx.events if e.type == "checkout_link_created"]
    assert ev and ev[0].properties == {"items": 1, "value": round(82.99 * 2, 2), "turn_id": "t"}
    assert [p["id"] for p in res.products] == ["p1"]


async def test_checkout_link_filters_unknown_product(monkeypatch):
    _patch_catalog(monkeypatch)
    _patch_create(monkeypatch, [])
    ctx = _ctx(settings={"checkout_url": BASE})
    res = await cm.checkout_link_tool(
        ctx,
        _deps(),
        {
            "cart_items": [
                {"product_id": "p1", "variant_id": None, "quantity": 1},
                {"product_id": "ghost", "variant_id": None, "quantity": 1},
            ]
        },
    )
    assert res.ok is True
    assert len(res.products) == 1 and res.products[0]["id"] == "p1"


async def test_checkout_link_no_valid_products(monkeypatch):
    _patch_catalog(monkeypatch)
    _patch_create(monkeypatch, [])
    ctx = _ctx(settings={"checkout_url": BASE})
    res = await cm.checkout_link_tool(
        ctx, _deps(), {"cart_items": [{"product_id": "ghost", "variant_id": None, "quantity": 1}]}
    )
    assert res.ok is False and res.error == "no_valid_products"
    assert res.links == []


async def test_checkout_link_no_base_url(monkeypatch):
    # business fără checkout_url + config default gol → checkout indisponibil (ok=False).
    _patch_catalog(monkeypatch)
    monkeypatch.setattr(cm.get_settings(), "checkout_base_url", "", raising=False)
    ctx = _ctx(settings={})
    res = await cm.checkout_link_tool(
        ctx, _deps(), {"cart_items": [{"product_id": "p1", "variant_id": None, "quantity": 1}]}
    )
    assert res.ok is False and res.error == "no_checkout_url"


async def test_checkout_link_business_setting_overrides(monkeypatch):
    # settings-ul businessului are prioritate față de config global.
    _patch_catalog(monkeypatch)
    _patch_create(monkeypatch, [])
    ctx = _ctx(settings={"checkout_url": "https://tenant.example/cart"})
    res = await cm.checkout_link_tool(
        ctx, _deps(), {"cart_items": [{"product_id": "p1", "variant_id": None, "quantity": 1}]}
    )
    assert res.ok is True and res.links == ["https://tenant.example/cart?ref=t"]


def test_checkout_link_registered():
    assert "checkout_link" in enabled_tools(object())


# --- validator: allowed_links ------------------------------------------------


def test_links_ok_accepts_generated_checkout_link():
    url = f"{BASE}?ref=t"
    assert _links_ok(f"Gata! Plătești aici: {url}", [], {url}) is True
    # un product_url retrievat rămâne valid
    assert _links_ok("vezi https://shop/p1", CATALOG, set()) is True


def test_links_ok_rejects_invented_link():
    url = f"{BASE}?ref=t"
    assert _links_ok("link inventat https://evil/x", [], {url}) is False
    assert _valid("Plătești aici https://evil/x", [], {url}) is False


# --- e2e prin agent_stage ----------------------------------------------------


class _FakeLLM:
    """Scriptează bucla: rulează tool_calls prin execute, întoarce `final`."""

    def __init__(self, *, tool_calls=(), final=""):
        self._tool_calls = list(tool_calls)
        self._final = final

    async def embed(self, texts, *, model=None):
        return [[0.0] * 8 for _ in texts]

    async def complete(self, system, user, *, model=None):
        return "fallback"

    async def run_tool_loop(self, system, user, tools, execute, *, max_steps=3, model=None):
        for name, args in self._tool_calls:
            await execute(name, args)
        return self._final


async def test_agent_sends_checkout_link_validated(monkeypatch):
    _patch_catalog(monkeypatch)
    _patch_create(monkeypatch, [])
    url = f"{BASE}?ref=t"
    ctx = _ctx(settings={"checkout_url": BASE})
    llm = _FakeLLM(
        tool_calls=[
            (
                "checkout_link",
                {"cart_items": [{"product_id": "p1", "variant_id": None, "quantity": 1}]},
            )
        ],
        final=f"Super alegere! Finalizezi comanda aici: {url}",
    )
    await agent_stage(ctx, _deps(llm))

    # linkul generat de bot e ACCEPTAT de validator (textul rămâne neschimbat, nu cade pe fallback)
    assert ctx.reply is not None and url in ctx.reply.text
    assert any(e.type == "checkout_link_created" for e in ctx.events)


@pytest.mark.parametrize("qty", [0, 100])
async def test_checkout_link_rejects_bad_quantity(monkeypatch, qty):
    _patch_catalog(monkeypatch)
    _patch_create(monkeypatch, [])
    ctx = _ctx(settings={"checkout_url": BASE})
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        await cm.checkout_link_tool(
            ctx,
            _deps(),
            {"cart_items": [{"product_id": "p1", "variant_id": None, "quantity": qty}]},
        )
