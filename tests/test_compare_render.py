"""IZI-compare — tabel comparativ structurat (P0: „Compară primele două" nu mai re-listează).

Pur, fără DB/Redis: `build_comparison` (din dict-uri de produs ca get_products_by_ids) →
`Comparison` (coloane + rânduri, fapte din date, ZERO proză LLM) → `render_web` (contract
frontend cu cheia `comparison`). Acoperă: construcția deterministă, anti-halucinație (celule doar
din date), anchor preț redus, floor aplatizat, roundtrip asdict (ruta async) și rutarea pe
capability (web = tabel; canale text = floor).
"""

import json
from dataclasses import asdict

from src.channels.base import Capability
from src.channels.web.render import render_web, reply_from_outbox
from src.channels.web.sender import WebSender
from src.domain.pack import FacetSpec
from src.models import Reply
from src.worker.compose import build_comparison, comparison_cards, flatten_comparison
from src.worker.dispatcher import _requested_render, choose_render


def _products() -> list[dict]:
    """Două produse ca din get_products_by_ids (ordine = cea cerută, păstrată de array_position)."""
    return [
        {
            "id": "p1",
            "name": "Crema A",
            "brand": "BrandX",
            "price": 58.99,
            "url": "https://shop/p1",
            "image": "https://cdn/p1.jpg",
            "availability": "in_stock",
            "rating": 4.8,
            "top_pros": ["hidratează intens", "fără parfum", "textură ușoară"],
            "top_cons": ["tub mic"],
        },
        {
            "id": "p2",
            "name": "Crema B",
            "brand": "BrandY",
            "price": 88.99,
            "url": "https://shop/p2",
            "image": "https://cdn/p2.jpg",
            "availability": "low_stock",
            "rating": 4.6,
            "top_pros": ["bogată", "pentru ten foarte uscat"],
            "top_cons": [],
        },
    ]


# --- build_comparison: determinist, fapte din date ---------------------------


def test_build_comparison_columns_and_rows():
    cmp = build_comparison(_products(), "ro")
    assert cmp is not None
    assert [c.product_id for c in cmp.columns] == ["p1", "p2"]  # ordine păstrată
    labels = {r.label for r in cmp.rows}
    assert {"Preț", "Rating", "Disponibilitate", "Avantaje", "Brand"} <= labels
    price_row = next(r for r in cmp.rows if r.label == "Preț")
    assert price_row.values == ["58.99 lei", "88.99 lei"]  # fapte din date, nu proză
    avail_row = next(r for r in cmp.rows if r.label == "Disponibilitate")
    assert avail_row.values == ["În stoc", "Stoc limitat"]


def test_build_comparison_drops_all_empty_row():
    # p1 are 1 con, p2 are 0 → rândul „De luat în calcul" rămâne (o celulă non-goală).
    # Dar dacă AMÂNDOUĂ ar fi goale, rândul dispare complet (vezi mai jos).
    prods = _products()
    prods[0]["top_cons"] = []  # acum AMBELE fără minusuri
    cmp = build_comparison(prods, "ro")
    assert cmp is not None
    assert all(r.label != "De luat în calcul" for r in cmp.rows)  # rând complet gol → sărit


def test_build_comparison_lead_has_data_verdict():
    cmp = build_comparison(_products(), "ro")
    # lead determinist: cel mai ieftin (Crema A) + cel mai bine cotat (Crema A) — derivat din date
    assert "Crema A" in (cmp.intro or "")
    assert "diferențele principale" in (cmp.intro or "")


def test_build_comparison_needs_two_valid():
    assert build_comparison(_products()[:1], "ro") is None  # un singur produs
    assert build_comparison([{"id": "x"}, {"id": "y"}], "ro") is None  # fără name/price


def test_build_comparison_list_price_anchor():
    prods = _products()
    prods[0]["list_price"] = 79.99  # preț de listă > curent (58.99) → anchor reducere
    cmp = build_comparison(prods, "ro")
    col = cmp.columns[0]
    # convenție unică: `price` = CURENT (58.99), `list_price` = ORIGINAL tăiat (79.99)
    assert col.price == 58.99 and col.list_price == 79.99


# --- Tier 2: fațete de DOMENIU în tabel (din products.attributes, generic DomainPack) ----------


def _concerns_facet() -> FacetSpec:
    return FacetSpec(
        key="concerns",
        labels={"ro": "Potrivit pentru", "en": "Suitable for"},
        value_labels={"oily": {"ro": "ten gras", "en": "oily skin"}, "dry": {"ro": "ten uscat"}},
    )


def test_build_comparison_facet_row_from_attributes():
    prods = _products()
    prods[0]["attributes"] = {"concerns": ["oily", "dry"]}
    prods[1]["attributes"] = {"concerns": ["dry"]}
    cmp = build_comparison(prods, "ro", [_concerns_facet()])
    row = next(r for r in cmp.rows if r.label == "Potrivit pentru")
    assert row.values == ["ten gras, ten uscat", "ten uscat"]  # listă → etichete unite, per-locale
    labels = [r.label for r in cmp.rows]
    assert labels.index("Potrivit pentru") < labels.index(
        "Disponibilitate"
    )  # între Rating și Avail


def test_build_comparison_facet_raw_value_and_en_label():
    prods = _products()
    f = FacetSpec(key="finish", labels={"ro": "Finisaj", "en": "Finish"})  # fără value_labels
    prods[0]["attributes"] = {"finish": "mat"}
    prods[1]["attributes"] = {"finish": "satinat"}
    cmp = build_comparison(prods, "en", [f])
    row = next(r for r in cmp.rows if r.label == "Finish")  # eticheta EN
    assert row.values == ["mat", "satinat"]  # display-ready → valoarea ca atare


def test_build_comparison_facet_all_empty_dropped_partial_dash():
    prods = _products()
    prods[0]["attributes"] = {"concerns": ["oily"]}
    prods[1]["attributes"] = {}  # lipsă pe coloana a doua
    spf = FacetSpec(key="spf", labels={"ro": "SPF"})  # niciun produs n-are spf
    cmp = build_comparison(prods, "ro", [_concerns_facet(), spf])
    assert all(r.label != "SPF" for r in cmp.rows)  # TOT-gol → rând sărit
    row = next(r for r in cmp.rows if r.label == "Potrivit pentru")
    assert row.values == ["ten gras", None]  # parțial gol → None („—" pe frontend)


def test_build_comparison_no_facets_unchanged():
    cmp = build_comparison(_products(), "ro")  # facets implicit () → tabel ca azi
    assert all(r.label not in ("Potrivit pentru", "Finisaj", "SPF") for r in cmp.rows)


# --- flatten_comparison: floor pt canale fără tabel --------------------------


def test_flatten_comparison_floor_text():
    cmp = build_comparison(_products(), "ro")
    floor = flatten_comparison(cmp, "ro")
    assert "Crema A" in floor and "Crema B" in floor
    assert "Preț: 58.99 lei · 88.99 lei" in floor
    # celulă lipsă (p2 fără minusuri, p1 cu „tub mic") → randată „—" pe partea goală
    cons_row = next(r for r in cmp.rows if r.label == "De luat în calcul")
    assert cons_row.values == ["tub mic", None]
    assert "tub mic · —" in floor


# --- render_web: contractul frontend cu cheia `comparison` -------------------


def _comparison_reply() -> Reply:
    cmp = build_comparison(_products(), "ro")
    return Reply(
        text=flatten_comparison(cmp, "ro"),
        products=comparison_cards(cmp),
        comparison=cmp,
        suggestions=["Adaugă Crema A", "Ceva mai ieftin"],
        cacheable=False,
    )


class _FakeRedis:
    """Redis fake minimal: captează publish (SSE) + pipeline no-op pt backlog."""

    def __init__(self) -> None:
        self.published: list[tuple[str, str]] = []

    async def publish(self, channel, message):
        self.published.append((channel, message))
        return 1

    def pipeline(self, transaction: bool = True):
        return _FakePipe()


class _FakePipe:
    def rpush(self, *a):
        return self

    def ltrim(self, *a):
        return self

    def expire(self, *a):
        return self

    async def execute(self):
        return [1, 1, 1]


def test_render_web_comparison_shape():
    out = render_web(_comparison_reply(), "ro")
    assert "comparison" in out
    assert [c["product_id"] for c in out["comparison"]["columns"]] == ["p1", "p2"]
    assert any(r["label"] == "Preț" for r in out["comparison"]["rows"])
    # cardurile produselor comparate (header poză+preț) + lead în content
    assert len(out["products"]) == 2 and out["products"][0]["image_url"] == "https://cdn/p1.jpg"
    assert "diferențele principale" in out["content"]
    assert out["suggestions"] == ["Adaugă Crema A", "Ceva mai ieftin"]


def test_render_web_comparison_roundtrip_async():
    # ruta async: asdict(comparison) → outbox → reply_from_outbox → render_web = shape ca sync
    rep = _comparison_reply()
    sync = render_web(rep, "ro")
    payload = {"comparison": asdict(rep.comparison), "products": rep.products, "text": rep.text}
    rebuilt = render_web(reply_from_outbox(payload), "ro")
    assert rebuilt["comparison"] == sync["comparison"]
    assert rebuilt["products"] == sync["products"]


async def test_send_rich_publishes_comparison_event():
    r = _FakeRedis()
    s = WebSender(r)
    rep = _comparison_reply()
    payload = {
        "to": "v1",
        "comparison": asdict(rep.comparison),
        "products": rep.products,
        "text": rep.text,
        "language": "ro",
    }
    await s.send_rich("tok", "v1", payload)
    evt = json.loads(r.published[0][1])
    assert evt["type"] == "rich" and "comparison" in evt
    assert len(evt["comparison"]["columns"]) == 2


# --- dispatcher: tabel pe web (COMPARISON), floor pe canale text -------------


def test_choose_render_comparison_web_vs_text():
    payload = {"comparison": {"columns": [], "rows": []}}
    web = frozenset({Capability.TEXT, Capability.RICH, Capability.CARDS, Capability.COMPARISON})
    wa = frozenset({Capability.TEXT})  # WhatsApp: fără COMPARISON → floor text
    assert choose_render(payload, "text", web) == "rich"  # web randează tabelul (send_rich)
    assert choose_render(payload, "text", wa) == "text"  # WhatsApp → floor aplatizat
    assert _requested_render(payload, "text") == "rich"  # comparația se CERE ca rich (degradare)


# --- end-to-end: agent_stage construiește comparația (fix P0 „Compară primele două") ---


async def test_agent_stage_builds_comparison_not_recommendation(monkeypatch):
    """Calea MODEL-DRIVEN: când mesajul NU declanșează gate-ul determinist de comparație (G2), dar
    modelul DECIDE să cheme compare_products (rezolvă „astea două" din displayed) → turul devine o
    COMPARAȚIE structurată, NU o re-recomandare (bug-ul iZi). reply.comparison setat. (Calea
    deterministă „compară primele două" e acoperită în test_agent.py.)"""
    from src.models import (
        BusinessConfig,
        Contact,
        ConversationState,
        InboundMessage,
        ProductRef,
        Route,
        RouteDecision,
        TurnContext,
    )
    from src.tools import catalog_tools
    from src.worker.runner import PipelineDeps
    from src.worker.stages import agent as agent_mod
    from src.worker.stages.agent import agent_stage

    async def _cats(conn, bid):
        return ["Creme"]

    async def _aliases(conn, bid, **k):
        return []

    monkeypatch.setattr(agent_mod, "list_category_names", _cats)
    monkeypatch.setattr(agent_mod, "list_routing_aliases", _aliases)

    prods = _products()

    async def _by_ids(conn, bid, ids, *, limit=6):
        order = {pid: i for i, pid in enumerate(ids)}
        return sorted([p for p in prods if p["id"] in ids], key=lambda p: order[p["id"]])[:limit]

    monkeypatch.setattr(catalog_tools, "get_products_by_ids", _by_ids)

    class FakeLLM:
        async def embed(self, texts, *, model=None):
            return [[0.0] * 8 for _ in texts]

        async def run_tool_loop(self, system, user, tools, execute, *, max_steps=3, model=None):
            await execute("compare_products", {"product_ids": ["p1", "p2"]})
            return "Crema A e mai ușoară, Crema B mai bogată."

    ctx = TurnContext(
        turn_id="t",
        business=BusinessConfig(id="b", slug="d", name="D"),
        contact=Contact(id="c", business_id="b"),
        # frazare care NU declanșează _COMPARE_RE (gate determinist) → exersează calea model-driven.
        message=InboundMessage(provider_msg_id="m", body="care din astea două mi se potrivește?"),
        conversation_id="conv",
        state=ConversationState(
            displayed_products=[
                ProductRef(product_id="p1", name="Crema A", price=58.99),
                ProductRef(product_id="p2", name="Crema B", price=88.99),
            ]
        ),
    )
    ctx.route = RouteDecision(route=Route.SALES)
    await agent_stage(ctx, PipelineDeps(conn=object(), redis=None, llm=FakeLLM()))

    assert ctx.reply is not None and ctx.reply.comparison is not None
    assert [c.product_id for c in ctx.reply.comparison.columns] == ["p1", "p2"]  # ordine păstrată
    assert ctx.reply.cacheable is False  # relativ la setul afișat
    assert any("Adaugă" in s for s in ctx.reply.suggestions)  # chips deterministe
    assert any(e.type == "agent_compared" for e in ctx.events)
